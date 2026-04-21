"""Chia corpus (BRAT) → Pydantic domain model loader.

Chia is a publicly released corpus of 1,000 clinical-trial eligibility
sections (Phase IV trials from CT.gov), hand-annotated in the BRAT
format with entities (e.g., Condition, Drug, Measurement, Value),
relations (Has_value, Has_negation, AND, ...) and equivalence groups
(OR-groups). The dataset is split per trial into separate inclusion
and exclusion files: `<NCT>_inc.{txt,ann}` and `<NCT>_exc.{txt,ann}`.

We use Chia as the **golden ground truth** for the criterion-extractor
node: extract entities + relations from raw eligibility text and
compare against the hand annotations.

Notes on the data (from a full-corpus scan, not the paper or BRAT
config alone):

- The `annotation.conf` schema lists 24 entity types and 5 relation
  types, but the actual corpus contains 31 entity types and 14
  binary relation types. The loader does not enforce a closed set —
  it parses everything and lets downstream code (e.g., the matcher)
  decide which types to use, ignore, or normalize.
- ~3.7% of entities have *discontinuous* spans (multiple `start;end`
  ranges), e.g., "major impairment of [renal] function" plus
  "[hepatic]" pulled from different parts of the surface. We model
  spans as a list, not a single (start, end) tuple.
- Equivalence groups (BRAT `*` lines) are n-ary (≥2 members) and have
  no numeric ID. Almost all equivalences are `OR` groups; a handful
  are `NOT`. We persist them as a separate `equivalence_groups`
  collection rather than trying to splay them into pseudo-relations.
- Attributes (BRAT `A` lines) carry only `Optional` flags in this
  corpus. We model them generically anyway.

References:
- Kury et al. 2020, "Chia, a large annotated corpus of clinical trial
  eligibility criteria", Scientific Data.
- BRAT annotation format: https://brat.nlplab.org/standoff.html
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from pathlib import Path

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Documented entity and relation types from the BRAT `annotation.conf`,
# kept here purely as documentation. Real data has more — see the module
# docstring. Downstream code that wants to validate against the
# documented set can do so explicitly.
DOCUMENTED_ENTITY_TYPES: frozenset[str] = frozenset(
    {
        # Concepts
        "Scope",
        "Person",
        "Condition",
        "Drug",
        "Observation",
        "Measurement",
        "Procedure",
        "Device",
        "Visit",
        # Annotations
        "Negation",
        "Qualifier",
        "Temporal",
        "Value",
        "Multiplier",
        "Reference_point",
        "Line",
        "Mood",
        # Errors / out-of-scope markers
        "Non-query-able",
        "Post-eligibility",
        "Pregnancy_considerations",
        "Competing_trial",
        "Informed_consent",
        "Intoxication_considerations",
        "Non-representable",
    }
)

DOCUMENTED_RELATION_TYPES: frozenset[str] = frozenset({"h-OR", "v-AND", "v-OR", "multi"})


class ChiaSpan(BaseModel):
    """A single (start, end) character offset into the source document.

    BRAT offsets are zero-indexed and end-exclusive (Python-slice style).
    """

    start: int
    end: int


class ChiaEntity(BaseModel):
    """A text-bound annotation (BRAT `T` line).

    `spans` may contain more than one element when the entity covers a
    *discontinuous* surface — e.g., "renal" and "function" pulled from
    different parts of the source where "renal" was separated from
    "function" by intervening text. The `text` field is the surface
    string as recorded by the annotator, which BRAT joins
    discontinuous fragments with a single space.
    """

    id: str
    type: str
    spans: list[ChiaSpan]
    text: str

    @property
    def start(self) -> int:
        """First start offset across all spans (useful for sorting)."""
        return self.spans[0].start

    @property
    def end(self) -> int:
        """Last end offset across all spans."""
        return self.spans[-1].end


class ChiaRelation(BaseModel):
    """A binary relation between two entities (BRAT `R` line).

    `type` is the relation label as it appears in the file
    (e.g., "Has_value", "AND"). The corpus uses many more relation
    types than the BRAT config documents — we don't filter.
    """

    id: str
    type: str
    arg1_id: str
    arg2_id: str


class ChiaEquivalenceGroup(BaseModel):
    """An n-ary equivalence group (BRAT `*` line).

    These BRAT lines have no per-line ID; they assert that all listed
    members are equivalent under the given relation type. The
    overwhelming majority in Chia are `OR` groups joining 2+ entities;
    a few are `NOT` groups. We preserve the full member list rather
    than pairwise expansion because the cardinality is informative
    (a 5-way OR is qualitatively different from two binary ORs).
    """

    type: str
    member_ids: list[str]


class ChiaAttribute(BaseModel):
    """A binary attribute on an entity or relation (BRAT `A` line).

    In the Chia corpus the only attribute observed is `Optional`,
    flagging that the target entity describes an optional/permissive
    criterion (e.g., 'patients on Sandostatin Lar must be on a stable
    dose' — the drug is optional, the dose is not).
    """

    id: str
    name: str
    target_id: str


class ChiaDocument(BaseModel):
    """One Chia annotation file pair (a `.txt` + its `.ann`).

    `entities` is keyed by ID (`T1`, `T2`, ...) for O(1) relation
    resolution. Other collections preserve insertion order from the
    source file.
    """

    doc_id: str
    source_text: str
    entities: dict[str, ChiaEntity] = Field(default_factory=dict)
    relations: list[ChiaRelation] = Field(default_factory=list)
    equivalence_groups: list[ChiaEquivalenceGroup] = Field(default_factory=list)
    attributes: list[ChiaAttribute] = Field(default_factory=list)


class ChiaTrial(BaseModel):
    """A single trial as represented in the Chia corpus.

    Chia stores inclusion and exclusion criteria as separate
    documents. A few trials in the corpus have only one of the two
    (an inclusion or exclusion file may be missing or empty).
    """

    nct_id: str
    inclusion: ChiaDocument | None = None
    exclusion: ChiaDocument | None = None


# ---------- BRAT parsing ----------

# BRAT lines are tab-separated with a leading single-character ID prefix
# ('T', 'R', 'A', 'E', 'M', 'N', '*', '#'). We handle T, R, A, *
# explicitly; all others are silently skipped (Chia uses none of them).

_T_PATTERN = re.compile(r"^([^\s]+)\s+(.*)$")


def parse_ann(
    content: str,
) -> tuple[
    dict[str, ChiaEntity],
    list[ChiaRelation],
    list[ChiaEquivalenceGroup],
    list[ChiaAttribute],
]:
    """Parse a BRAT `.ann` file body into typed collections.

    Lines that don't conform to the expected format are logged at
    DEBUG and skipped — the corpus has a handful of malformed lines
    (mostly empty trailing entries) that are not worth crashing on.
    """
    entities: dict[str, ChiaEntity] = {}
    relations: list[ChiaRelation] = []
    equivalence_groups: list[ChiaEquivalenceGroup] = []
    attributes: list[ChiaAttribute] = []

    for raw in content.splitlines():
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        prefix = line[0]
        try:
            if prefix == "T":
                ent = _parse_entity_line(line)
                entities[ent.id] = ent
            elif prefix == "R":
                relations.append(_parse_relation_line(line))
            elif prefix == "*":
                eq = _parse_equivalence_line(line)
                if eq is not None:
                    equivalence_groups.append(eq)
            elif prefix == "A" or prefix == "M":
                attributes.append(_parse_attribute_line(line))
            # Other BRAT line types are not used by Chia.
        except (ValueError, IndexError) as e:
            logger.debug("skipping malformed BRAT line %r: %s", line[:80], e)

    return entities, relations, equivalence_groups, attributes


def _parse_entity_line(line: str) -> ChiaEntity:
    """Parse a `T` line: `T1<TAB>Type 0 5;10 15<TAB>surface text`.

    The middle field carries the entity type plus a semicolon-separated
    list of `start end` offset pairs (single pair for the common
    contiguous-span case, multiple for discontinuous spans).
    """
    parts = line.split("\t")
    if len(parts) < 3:
        raise ValueError("entity line missing tab-separated fields")
    eid, type_and_spans, text = parts[0], parts[1], parts[2]
    type_str, _, span_str = type_and_spans.partition(" ")
    if not span_str:
        raise ValueError("entity line missing span offsets")
    spans: list[ChiaSpan] = []
    for chunk in span_str.split(";"):
        a, _, b = chunk.strip().partition(" ")
        spans.append(ChiaSpan(start=int(a), end=int(b)))
    return ChiaEntity(id=eid, type=type_str, spans=spans, text=text)


def _parse_relation_line(line: str) -> ChiaRelation:
    """Parse an `R` line: `R1<TAB>Has_value Arg1:T3 Arg2:T2`."""
    parts = line.split("\t")
    if len(parts) < 2:
        raise ValueError("relation line missing tab-separated fields")
    rid = parts[0]
    body = parts[1]
    type_str, _, args = body.partition(" ")
    if not args:
        raise ValueError("relation line missing arguments")
    arg1 = arg2 = ""
    for tok in args.split():
        if tok.startswith("Arg1:"):
            arg1 = tok.split(":", 1)[1]
        elif tok.startswith("Arg2:"):
            arg2 = tok.split(":", 1)[1]
    if not (arg1 and arg2):
        raise ValueError("relation line missing Arg1/Arg2")
    return ChiaRelation(id=rid, type=type_str, arg1_id=arg1, arg2_id=arg2)


def _parse_equivalence_line(line: str) -> ChiaEquivalenceGroup | None:
    """Parse a `*` line: `*<TAB>OR T5 T6 T7`.

    Returns None if there are fewer than two members (a single-member
    equivalence is meaningless and is treated as malformed).
    """
    parts = line.split("\t")
    if len(parts) < 2:
        return None
    body = parts[1].split()
    if len(body) < 3:
        return None
    type_str, members = body[0], body[1:]
    return ChiaEquivalenceGroup(type=type_str, member_ids=members)


def _parse_attribute_line(line: str) -> ChiaAttribute:
    """Parse an `A` line: `A1<TAB>Optional T32`.

    BRAT also allows trailing values for non-binary attributes (e.g.
    `A1<TAB>Confidence T32 high`); Chia's only attribute is the
    binary `Optional` flag, so we ignore any trailing tokens.
    """
    parts = line.split("\t")
    if len(parts) < 2:
        raise ValueError("attribute line missing tab-separated fields")
    aid = parts[0]
    body = parts[1].split()
    if len(body) < 2:
        raise ValueError("attribute line missing target")
    return ChiaAttribute(id=aid, name=body[0], target_id=body[1])


# ---------- file/document/trial loaders ----------


def load_document(txt_path: Path | str) -> ChiaDocument:
    """Load one Chia document from its `.txt` file.

    The matching `.ann` file is expected to live next to it with the
    same stem. Raises `FileNotFoundError` if either file is missing.
    """
    txt_path = Path(txt_path)
    ann_path = txt_path.with_suffix(".ann")
    source_text = txt_path.read_text(encoding="utf-8")
    ann_body = ann_path.read_text(encoding="utf-8")
    entities, relations, equiv, attributes = parse_ann(ann_body)
    return ChiaDocument(
        doc_id=txt_path.stem,
        source_text=source_text,
        entities=entities,
        relations=relations,
        equivalence_groups=equiv,
        attributes=attributes,
    )


def load_trial(directory: Path | str, nct_id: str) -> ChiaTrial:
    """Load both inc and exc documents for a trial, when present."""
    directory = Path(directory)
    inc_path = directory / f"{nct_id}_inc.txt"
    exc_path = directory / f"{nct_id}_exc.txt"
    return ChiaTrial(
        nct_id=nct_id,
        inclusion=load_document(inc_path) if inc_path.exists() else None,
        exclusion=load_document(exc_path) if exc_path.exists() else None,
    )


def iter_trials(directory: Path | str) -> Iterator[ChiaTrial]:
    """Yield one `ChiaTrial` per NCT id found in `directory`.

    NCT ids are discovered by looking for `<NCT>_inc.txt` files (the
    overwhelmingly common case); trials with only an exclusion file
    are not surfaced by the discovery step but can be loaded
    explicitly via `load_trial`.

    Iteration order is sorted by NCT id for determinism.
    """
    directory = Path(directory)
    nct_ids: list[str] = []
    for p in directory.glob("*_inc.txt"):
        nct_ids.append(p.stem.removesuffix("_inc"))
    for nct_id in sorted(nct_ids):
        yield load_trial(directory, nct_id)

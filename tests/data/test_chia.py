"""Tests for the Chia corpus loader.

Two flavors of test:

1. Unit tests for the BRAT line parser, with hand-crafted line strings
   that exercise edge cases (discontinuous spans, n-ary equivalence,
   trailing tabs, malformed lines).

2. Integration tests for `load_document` / `load_trial` / `iter_trials`
   against a small set of real corpus files copied verbatim into
   `tests/fixtures/chia/`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clinical_demo.data.chia import (
    DOCUMENTED_ENTITY_TYPES,
    ChiaDocument,
    ChiaTrial,
    iter_trials,
    load_document,
    load_trial,
    parse_ann,
)

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "chia"


# ---------- entity parsing ----------


def test_parse_entity_with_contiguous_span() -> None:
    entities, _, _, _ = parse_ann("T1\tCondition 0 9\tdiabetes")
    assert "T1" in entities
    e = entities["T1"]
    assert e.type == "Condition"
    assert len(e.spans) == 1
    assert (e.spans[0].start, e.spans[0].end) == (0, 9)
    assert e.text == "diabetes"
    assert e.start == 0
    assert e.end == 9


def test_parse_entity_with_discontinuous_span() -> None:
    """BRAT encodes discontinuous spans as `start1 end1;start2 end2`.
    The recorded `text` is the joined surface, separated by space."""
    line = "T19\tCondition 331 356;368 376\tmajor impairment of renal function"
    entities, _, _, _ = parse_ann(line)
    e = entities["T19"]
    assert len(e.spans) == 2
    assert (e.spans[0].start, e.spans[0].end) == (331, 356)
    assert (e.spans[1].start, e.spans[1].end) == (368, 376)
    assert e.start == 331
    assert e.end == 376


def test_parse_entity_with_underscore_in_type() -> None:
    """Type names like `Reference_point` and `Non-query-able` round-trip."""
    line = "T2\tReference_point 10 27\treference window"
    entities, _, _, _ = parse_ann(line)
    assert entities["T2"].type == "Reference_point"


# ---------- relation parsing ----------


def test_parse_binary_relation() -> None:
    """Relation lines often have a trailing tab that we must tolerate."""
    line = "R1\tHas_value Arg1:T3 Arg2:T2\t"
    _, relations, _, _ = parse_ann(line)
    assert len(relations) == 1
    r = relations[0]
    assert r.id == "R1"
    assert r.type == "Has_value"
    assert r.arg1_id == "T3"
    assert r.arg2_id == "T2"


def test_parse_relation_arguments_in_either_order() -> None:
    """Defensive: BRAT spec doesn't mandate Arg1-then-Arg2 ordering."""
    line = "R7\tAND Arg2:T11 Arg1:T9"
    _, relations, _, _ = parse_ann(line)
    assert relations[0].arg1_id == "T9"
    assert relations[0].arg2_id == "T11"


# ---------- equivalence-group parsing ----------


def test_parse_n_ary_equivalence_group() -> None:
    """OR groups commonly link 3+ entities; we keep them as a group, not
    a fan of binary relations, so cardinality is preserved."""
    line = "*\tOR T5 T6 T7"
    _, _, equiv, _ = parse_ann(line)
    assert len(equiv) == 1
    eq = equiv[0]
    assert eq.type == "OR"
    assert eq.member_ids == ["T5", "T6", "T7"]


def test_parse_equivalence_with_two_members() -> None:
    line = "*\tOR T1 T4"
    _, _, equiv, _ = parse_ann(line)
    assert equiv[0].member_ids == ["T1", "T4"]


def test_parse_equivalence_with_single_member_is_dropped() -> None:
    """An equivalence with one member is meaningless; skip silently."""
    line = "*\tOR T1"
    _, _, equiv, _ = parse_ann(line)
    assert equiv == []


# ---------- attribute parsing ----------


def test_parse_attribute_optional_flag() -> None:
    line = "A1\tOptional T32"
    _, _, _, attrs = parse_ann(line)
    assert len(attrs) == 1
    a = attrs[0]
    assert a.id == "A1"
    assert a.name == "Optional"
    assert a.target_id == "T32"


# ---------- malformed / mixed input ----------


def test_blank_lines_are_ignored() -> None:
    body = "\n\nT1\tCondition 0 9\tdiabetes\n\n"
    entities, _, _, _ = parse_ann(body)
    assert "T1" in entities


def test_unknown_brat_line_types_are_silently_skipped() -> None:
    """BRAT defines E (events), N (normalizations), # (comments) which
    Chia does not use. The loader must not crash on them."""
    body = "\n".join(
        [
            "T1\tCondition 0 9\tdiabetes",
            "#1\tAnnotatorNotes T1\tnote text",
            "N1\tReference T1 SNOMED:73211009\tdiabetes",
            "E1\tSomeEvent:T1",
        ]
    )
    entities, relations, equiv, attrs = parse_ann(body)
    assert "T1" in entities
    assert relations == []
    assert equiv == []
    assert attrs == []


def test_malformed_line_is_logged_and_skipped() -> None:
    """A malformed entity line must not abort the whole file."""
    body = "T1\tCondition NOT-INTS\tx\nT2\tDrug 0 4\tibuprofen"
    entities, _, _, _ = parse_ann(body)
    # T1 fails to parse (offsets not ints); T2 still loads.
    assert "T1" not in entities
    assert "T2" in entities


# ---------- document / trial / iterator loading ----------


@pytest.fixture(scope="module")
def nct50349_inc() -> ChiaDocument:
    return load_document(FIXTURE_DIR / "NCT00050349_inc.txt")


def test_load_document_attaches_source_text(nct50349_inc: ChiaDocument) -> None:
    assert nct50349_inc.source_text.startswith("Patients with biopsy-proven")
    assert nct50349_inc.doc_id == "NCT00050349_inc"


def test_load_document_finds_all_annotation_kinds(
    nct50349_inc: ChiaDocument,
) -> None:
    """Real fixture has ~50 entities, multiple relations, equiv groups,
    and Optional attributes — verify each collection is non-empty."""
    assert len(nct50349_inc.entities) > 30
    assert len(nct50349_inc.relations) > 10
    assert len(nct50349_inc.equivalence_groups) > 0
    assert len(nct50349_inc.attributes) > 0


def test_real_fixture_contains_discontinuous_entity(
    nct50349_inc: ChiaDocument,
) -> None:
    disc = [e for e in nct50349_inc.entities.values() if len(e.spans) > 1]
    assert disc, "fixture should exercise the discontinuous-span path"
    e = disc[0]
    assert e.type == "Condition"
    # The first discontinuous entity in this fixture covers the renal/
    # hepatic split: 'major impairment of [renal] function'.
    assert "renal" in e.text


def test_real_fixture_entity_types_are_in_documented_set(
    nct50349_inc: ChiaDocument,
) -> None:
    """This particular fixture happens to use only documented types.
    Documents in the wider corpus may use undocumented types — those
    are intentionally not flagged at load time."""
    used = {e.type for e in nct50349_inc.entities.values()}
    assert used <= DOCUMENTED_ENTITY_TYPES


def test_byte_offsets_round_trip_to_recorded_surface_text(
    nct50349_inc: ChiaDocument,
) -> None:
    """For every entity, the byte ranges in `spans` should slice the
    source text down to (effectively) the recorded surface text. This
    catches whitespace-stripping or encoding mutations that would
    silently shift offsets — a real risk because BRAT stores byte
    offsets, not character offsets, and any pre-commit hook that
    edits the .txt file would corrupt the entire annotation set."""
    src = nct50349_inc.source_text
    for e in nct50349_inc.entities.values():
        # BRAT joins discontinuous spans with a single space when
        # recording the surface; we mirror that.
        recovered = " ".join(src[s.start : s.end] for s in e.spans)
        assert recovered == e.text, (
            f"{e.id} ({e.type}) offsets recovered {recovered!r} "
            f"but file recorded {e.text!r} — fixture .txt may have "
            f"been mutated (e.g. trailing-whitespace stripped)"
        )


def test_load_trial_handles_missing_exclusion_file(tmp_path: Path) -> None:
    inc_txt = FIXTURE_DIR / "NCT00050349_inc.txt"
    inc_ann = FIXTURE_DIR / "NCT00050349_inc.ann"
    target_dir = tmp_path / "chia"
    target_dir.mkdir()
    (target_dir / "NCT99999999_inc.txt").write_text(inc_txt.read_text())
    (target_dir / "NCT99999999_inc.ann").write_text(inc_ann.read_text())
    trial = load_trial(target_dir, "NCT99999999")
    assert trial.nct_id == "NCT99999999"
    assert trial.inclusion is not None
    assert trial.exclusion is None


def test_iter_trials_discovers_by_inc_file(tmp_path: Path) -> None:
    inc_txt = FIXTURE_DIR / "NCT00050349_inc.txt"
    inc_ann = FIXTURE_DIR / "NCT00050349_inc.ann"
    target_dir = tmp_path / "chia"
    target_dir.mkdir()
    for nct in ("NCT11111111", "NCT22222222"):
        (target_dir / f"{nct}_inc.txt").write_text(inc_txt.read_text())
        (target_dir / f"{nct}_inc.ann").write_text(inc_ann.read_text())
    trials: list[ChiaTrial] = list(iter_trials(target_dir))
    assert [t.nct_id for t in trials] == ["NCT11111111", "NCT22222222"]

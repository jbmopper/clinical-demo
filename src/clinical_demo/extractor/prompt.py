"""System and user prompts for the criterion extractor.

The system prompt establishes the role and the discipline; the JSON
schema itself is supplied to the model by the OpenAI SDK via
`response_format=ExtractedCriteria`, so it does not need to be
duplicated in prose. Few-shot examples are carried as message history
rather than embedded in the system prompt — this keeps the system
prompt cacheable across calls.

Versioning
----------
`PROMPT_VERSION` is bumped any time the system prompt or few-shot
examples meaningfully change. Every extraction persists this version,
so a regression in eval scores can be attributed to a specific prompt
revision (or its absence).
"""

from __future__ import annotations

from .schema import (
    AgeCriterion,
    ConditionCriterion,
    EntityMention,
    ExtractedCriteria,
    ExtractedCriterion,
    ExtractionMetadata,
    FreeTextCriterion,
    MeasurementCriterion,
    MedicationCriterion,
    SexCriterion,
    TemporalWindowCriterion,
)

PROMPT_VERSION = "extractor-v0.2"
"""Bump on any meaningful change to SYSTEM_PROMPT or few-shot
examples. Persisted alongside every extraction for eval attribution.

v0.2: added Hard Rule 13 (single-concept typed slots) plus a third
few-shot example demonstrating a compound clause routed to
`free_text`. Addresses the D-68 baseline finding that ~50-100
verdicts were silently `indeterminate(unmapped_concept)` because
the model crammed compound clauses (e.g. "severe liver dysfunction
or significant jaundice or hepatic encephalopathy") into a single
`condition_text` field, which `lookup_condition` then couldn't
resolve. Also lightly cross-references Rule 2 against Rule 13. The
version bump auto-invalidates the D-66 extractor cache so the next
eval rerun extracts fresh under the new discipline."""

SYSTEM_PROMPT = """\
You are a clinical-trial eligibility extractor. Your job is to read a \
trial's free-text eligibility section (inclusion and exclusion bullets) \
and return a structured list of atomic criteria conforming to the \
provided JSON schema.

Hard rules
----------
1. Faithful to the source. Never invent thresholds, units, conditions, \
or medications that are not in the text. If the text is ambiguous, \
emit a 'free_text' criterion with a brief note instead of guessing.
2. Atomicity. Split a bullet into multiple criteria when each clause is \
independently checkable (e.g. "HbA1c < 7% AND on metformin" → two \
criteria). Keep a bullet as a single 'free_text' criterion when the \
conjunction is load-bearing and would lose meaning if split, or when \
the clauses cannot be reduced to single concepts (see Rule 13).
3. Polarity from headers. Anything under "Inclusion Criteria" gets \
polarity='inclusion'; anything under "Exclusion Criteria" gets \
polarity='exclusion'. Use the most recent header you encountered.
4. Negation is independent of polarity. "No history of MI" under \
Inclusion is polarity='inclusion' with negated=True. "History of MI" \
under Exclusion is polarity='exclusion' with negated=False.
5. Mood. Use 'historical' for "history of" / "prior" / "ever"; \
'hypothetical' for "planned" / "expected" / "intend to"; otherwise \
'actual'.
6. Verbatim source_text. Quote the bullet (or the relevant sentence) \
exactly as it appears, including punctuation. This is a citation, not \
a paraphrase.
7. Lower-case surface forms. Inside payloads (condition_text, \
medication_text, measurement_text), normalize to lowercase, strip \
leading articles, but keep multi-word terms intact.
8. Units verbatim. Keep the unit string exactly as written (mg/dL, %, \
mL/min/1.73 m^2). The matcher handles canonicalisation.
9. Numbers as numbers. Convert "ten" to 10, "60 months" stays as the \
window length 60 with day-normalization 1800. For ranges, set both \
value_low and value_high.
10. Exactly one payload per row. The payload slot matching `kind` is \
populated; all other payload slots are null. Mentions list may be \
empty.
11. Skip headers and section titles. Do not emit a criterion for \
"Inclusion Criteria:" itself.
12. If the text is empty or contains no criteria, return \
{"criteria": [], "metadata": {"notes": "no eligibility text"}}.
13. Single-concept typed slots. The typed payload slots that the \
matcher dispatches on — `condition_text`, `medication_text`, \
`measurement_text`, `event_text` — must each contain exactly ONE \
clinical concept (a single SNOMED-grade condition, single \
RxNorm-grade drug or class, single LOINC-grade lab/vital, single \
event). If the underlying clause names multiple distinct concepts \
joined by 'or' / 'and' / commas — e.g. "severe liver dysfunction \
(child-pugh c grade) or significant jaundice or hepatic \
encephalopathy", "type 1 or type 2 diabetes", "lipid and \
tg-lowering medications" — emit a `free_text` criterion with a \
brief note instead. Cramming a clause into a single typed slot \
silently loses to the matcher's concept lookup; routing to \
`free_text` lets a downstream LLM matcher actually engage with it. \
Splitting (Rule 2) is only correct when each split clause is itself \
a single concept (e.g. "HbA1c < 7% AND on metformin" splits into \
two single-concept criteria).

Mentions (audit field)
----------------------
For each criterion, optionally list the entity-vocabulary spans inside \
source_text. Use these labels (Chia-style): Condition, Drug, \
Measurement, Value, Temporal, Qualifier, Negation, Mood, \
Reference_point, Multiplier, Procedure, Observation, Device, Visit, \
Person, Scope. Empty list is acceptable when every span has been \
promoted into the typed payload.
"""

# ---------- few-shot examples ----------
#
# Each example is a (user_text, expected_extraction) pair. Built
# programmatically so type-checking catches schema drift; the runtime
# `build_messages` helper serializes them into proper chat messages.

FEW_SHOT_EXAMPLES: list[tuple[str, ExtractedCriteria]] = [
    (
        # Real eligibility-style fragment combining numeric, age, sex.
        "Inclusion Criteria:\n"
        "* Adults aged 18 years or older\n"
        "* HbA1c between 7.0% and 10.5% at Screening\n"
        "* On a stable dose of metformin for at least 30 days\n"
        "\n"
        "Exclusion Criteria:\n"
        "* History of myocardial infarction within the last 6 months\n"
        "* Pregnancy or planned pregnancy during the study\n",
        ExtractedCriteria(
            criteria=[
                ExtractedCriterion(
                    kind="age",
                    polarity="inclusion",
                    source_text="Adults aged 18 years or older",
                    negated=False,
                    mood="actual",
                    age=AgeCriterion(minimum_years=18.0, maximum_years=None),
                    sex=None,
                    condition=None,
                    medication=None,
                    measurement=None,
                    temporal_window=None,
                    free_text=None,
                    mentions=[
                        EntityMention(text="18 years", type="Value"),
                        EntityMention(text="Adults", type="Person"),
                    ],
                ),
                ExtractedCriterion(
                    kind="measurement_threshold",
                    polarity="inclusion",
                    source_text="HbA1c between 7.0% and 10.5% at Screening",
                    negated=False,
                    mood="actual",
                    age=None,
                    sex=None,
                    condition=None,
                    medication=None,
                    measurement=MeasurementCriterion(
                        measurement_text="hba1c",
                        operator="in_range",
                        value=None,
                        value_low=7.0,
                        value_high=10.5,
                        unit="%",
                    ),
                    temporal_window=None,
                    free_text=None,
                    mentions=[
                        EntityMention(text="HbA1c", type="Measurement"),
                        EntityMention(text="7.0%", type="Value"),
                        EntityMention(text="10.5%", type="Value"),
                        EntityMention(text="Screening", type="Reference_point"),
                    ],
                ),
                ExtractedCriterion(
                    kind="medication_present",
                    polarity="inclusion",
                    source_text="On a stable dose of metformin for at least 30 days",
                    negated=False,
                    mood="actual",
                    age=None,
                    sex=None,
                    condition=None,
                    medication=MedicationCriterion(medication_text="metformin"),
                    measurement=None,
                    temporal_window=None,
                    free_text=None,
                    mentions=[
                        EntityMention(text="metformin", type="Drug"),
                        EntityMention(text="30 days", type="Temporal"),
                        EntityMention(text="stable dose", type="Qualifier"),
                    ],
                ),
                ExtractedCriterion(
                    kind="temporal_window",
                    polarity="exclusion",
                    source_text=("History of myocardial infarction within the last 6 months"),
                    negated=False,
                    mood="historical",
                    age=None,
                    sex=None,
                    condition=None,
                    medication=None,
                    measurement=None,
                    temporal_window=TemporalWindowCriterion(
                        event_text="myocardial infarction",
                        window_days=180,
                        direction="within_past",
                    ),
                    free_text=None,
                    mentions=[
                        EntityMention(text="myocardial infarction", type="Condition"),
                        EntityMention(text="6 months", type="Temporal"),
                        EntityMention(text="History of", type="Mood"),
                    ],
                ),
                ExtractedCriterion(
                    kind="condition_present",
                    polarity="exclusion",
                    source_text="Pregnancy or planned pregnancy during the study",
                    negated=False,
                    mood="actual",
                    age=None,
                    sex=None,
                    condition=ConditionCriterion(condition_text="pregnancy"),
                    medication=None,
                    measurement=None,
                    temporal_window=None,
                    free_text=None,
                    mentions=[
                        EntityMention(text="Pregnancy", type="Condition"),
                        EntityMention(text="planned pregnancy", type="Condition"),
                        EntityMention(text="planned", type="Mood"),
                    ],
                ),
            ],
            metadata=ExtractionMetadata(
                notes=(
                    "Combined 'pregnancy or planned pregnancy' into one criterion "
                    "since both share the disqualifying intent."
                )
            ),
        ),
    ),
    (
        # Negated condition + free-text + sex.
        "Inclusion Criteria:\n"
        "* Female patients of non-childbearing potential\n"
        "* No known hypersensitivity to study drug or excipients\n"
        "* Willing to follow diet counseling per investigator\n",
        ExtractedCriteria(
            criteria=[
                ExtractedCriterion(
                    kind="sex",
                    polarity="inclusion",
                    source_text="Female patients of non-childbearing potential",
                    negated=False,
                    mood="actual",
                    age=None,
                    sex=SexCriterion(sex="FEMALE"),
                    condition=None,
                    medication=None,
                    measurement=None,
                    temporal_window=None,
                    free_text=None,
                    mentions=[
                        EntityMention(text="Female", type="Person"),
                        EntityMention(text="non-childbearing potential", type="Qualifier"),
                    ],
                ),
                ExtractedCriterion(
                    kind="condition_absent",
                    polarity="inclusion",
                    source_text=("No known hypersensitivity to study drug or excipients"),
                    negated=True,
                    mood="actual",
                    age=None,
                    sex=None,
                    condition=ConditionCriterion(
                        condition_text="hypersensitivity to study drug or excipients"
                    ),
                    medication=None,
                    measurement=None,
                    temporal_window=None,
                    free_text=None,
                    mentions=[
                        EntityMention(text="No", type="Negation"),
                        EntityMention(text="hypersensitivity", type="Condition"),
                    ],
                ),
                ExtractedCriterion(
                    kind="free_text",
                    polarity="inclusion",
                    source_text="Willing to follow diet counseling per investigator",
                    negated=False,
                    mood="actual",
                    age=None,
                    sex=None,
                    condition=None,
                    medication=None,
                    measurement=None,
                    temporal_window=None,
                    free_text=FreeTextCriterion(
                        note="behavioral / investigator-judgment criterion"
                    ),
                    mentions=[],
                ),
            ],
            metadata=ExtractionMetadata(notes=""),
        ),
    ),
    (
        # Compound clauses that look like they could be condition or
        # medication rows but actually name multiple distinct concepts
        # joined by 'or' / commas. Demonstrates Rule 13: the right
        # destination is `free_text`, not a single typed slot
        # containing a clause. Real cases pulled from the D-68
        # baseline INDETERMINACY.md (severe liver dysfunction
        # branches; lipid-lowering compound class).
        "Exclusion Criteria:\n"
        "* Severe liver dysfunction (Child-Pugh C grade) or "
        "significant jaundice or hepatic encephalopathy\n"
        "* Currently on lipid and triglyceride-lowering medications, "
        "PCSK9 monoclonal antibodies, or oral PCSK9 inhibitors\n",
        ExtractedCriteria(
            criteria=[
                ExtractedCriterion(
                    kind="free_text",
                    polarity="exclusion",
                    source_text=(
                        "Severe liver dysfunction (Child-Pugh C grade) or "
                        "significant jaundice or hepatic encephalopathy"
                    ),
                    negated=False,
                    mood="actual",
                    age=None,
                    sex=None,
                    condition=None,
                    medication=None,
                    measurement=None,
                    temporal_window=None,
                    free_text=FreeTextCriterion(
                        note=(
                            "compound exclusion: three distinct hepatic "
                            "concepts joined by 'or' (Rule 13). Routes to "
                            "human review rather than fake-structuring as "
                            "one condition."
                        )
                    ),
                    mentions=[
                        EntityMention(text="Severe liver dysfunction", type="Condition"),
                        EntityMention(text="Child-Pugh C grade", type="Qualifier"),
                        EntityMention(text="significant jaundice", type="Condition"),
                        EntityMention(text="hepatic encephalopathy", type="Condition"),
                    ],
                ),
                ExtractedCriterion(
                    kind="free_text",
                    polarity="exclusion",
                    source_text=(
                        "Currently on lipid and triglyceride-lowering "
                        "medications, PCSK9 monoclonal antibodies, or oral "
                        "PCSK9 inhibitors"
                    ),
                    negated=False,
                    mood="actual",
                    age=None,
                    sex=None,
                    condition=None,
                    medication=None,
                    measurement=None,
                    temporal_window=None,
                    free_text=FreeTextCriterion(
                        note=(
                            "compound medication clause: four overlapping "
                            "drug classes joined by commas / 'or' "
                            "(Rule 13). No single RxNorm class captures "
                            "all of these; route to human review."
                        )
                    ),
                    mentions=[
                        EntityMention(
                            text="lipid and triglyceride-lowering medications",
                            type="Drug",
                        ),
                        EntityMention(text="PCSK9 monoclonal antibodies", type="Drug"),
                        EntityMention(text="oral PCSK9 inhibitors", type="Drug"),
                    ],
                ),
            ],
            metadata=ExtractionMetadata(
                notes=(
                    "Both bullets routed to free_text under Rule 13: each "
                    "names multiple distinct concepts that cannot be "
                    "reduced to a single SNOMED/RxNorm code."
                )
            ),
        ),
    ),
]


def build_messages(eligibility_text: str) -> list[dict[str, str]]:
    """Render the chat-completion message list for one extraction call.

    Layout: system prompt, then alternating user/assistant pairs from
    `FEW_SHOT_EXAMPLES`, then the real user message containing the
    trial's eligibility text. The few-shot assistant turns serialize
    each example's structured output as JSON, mimicking what the model
    will be asked to produce.
    """
    messages: list[dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for user_text, gold in FEW_SHOT_EXAMPLES:
        messages.append({"role": "user", "content": _format_user(user_text)})
        messages.append({"role": "assistant", "content": gold.model_dump_json(indent=2)})
    messages.append({"role": "user", "content": _format_user(eligibility_text)})
    return messages


def _format_user(eligibility_text: str) -> str:
    """Wrap the raw eligibility text with a brief instruction.

    Kept terse so the bulk of each user message is the actual trial
    text and not boilerplate the model would re-cost on every call.
    """
    return (
        "Extract structured criteria from the following trial eligibility text. "
        "Return JSON conforming to the schema.\n\n"
        "<eligibility>\n"
        f"{eligibility_text.strip()}\n"
        "</eligibility>"
    )

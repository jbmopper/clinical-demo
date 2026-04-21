"""Schema-shape tests for the extractor's structured output.

These tests are pure Pydantic: they don't hit OpenAI. They check
that the schema:
- accepts well-formed payloads,
- rejects misshapen payloads,
- preserves the discriminator/payload one-non-null invariant when
  consumers obey the contract,
- is round-trippable as JSON,
- is acceptable to OpenAI's structured-outputs strict mode.
"""

from __future__ import annotations

from typing import Any

import pytest
from openai.lib._parsing import type_to_response_format_param
from pydantic import ValidationError

from clinical_demo.extractor.schema import (
    CRITERION_KINDS,
    AgeCriterion,
    ConditionCriterion,
    EntityMention,
    ExtractedCriteria,
    ExtractedCriterion,
    ExtractionMetadata,
    ExtractorRunMeta,
    FreeTextCriterion,
    MeasurementCriterion,
    MedicationCriterion,
    SexCriterion,
    TemporalWindowCriterion,
)


def _row(**overrides: Any) -> ExtractedCriterion:
    """Build a `ExtractedCriterion` with every slot defaulted to a
    sensible None / placeholder; tests overlay specific fields.

    Using a plain dict lets tests stay short while still exercising
    the full required-field set; `Any`-typed overrides keep this
    helper compatible with the Literal-typed fields the schema uses."""
    base: dict[str, Any] = dict(
        kind="free_text",
        polarity="inclusion",
        source_text="placeholder source",
        negated=False,
        mood="actual",
        mentions=[],
        free_text=FreeTextCriterion(note=""),
        age=None,
        sex=None,
        condition=None,
        medication=None,
        measurement=None,
        temporal_window=None,
    )
    base.update(overrides)
    return ExtractedCriterion.model_validate(base)


# ---------- discriminator coverage ----------


def test_criterion_kinds_tuple_matches_literal():
    """The CRITERION_KINDS tuple is the source of truth for tests
    that want to enumerate kinds; it must stay in sync with the
    Literal in `kind`."""
    field = ExtractedCriterion.model_fields["kind"]
    annotation = field.annotation
    assert annotation is not None
    literal_values = set(getattr(annotation, "__args__", ()))
    assert set(CRITERION_KINDS) == literal_values
    assert len(CRITERION_KINDS) == len(set(CRITERION_KINDS))  # no duplicates


@pytest.mark.parametrize("kind", CRITERION_KINDS)
def test_each_kind_round_trips_as_json(kind: str):
    """Every discriminator value should be constructible with the
    appropriate payload and survive JSON round-trip unchanged."""
    payloads: dict[str, Any] = dict(
        age=None,
        sex=None,
        condition=None,
        medication=None,
        measurement=None,
        temporal_window=None,
        free_text=None,
    )
    table: dict[str, dict[str, Any]] = {
        "age": {"age": AgeCriterion(minimum_years=18.0, maximum_years=None)},
        "sex": {"sex": SexCriterion(sex="MALE")},
        "condition_present": {"condition": ConditionCriterion(condition_text="diabetes")},
        "condition_absent": {"condition": ConditionCriterion(condition_text="diabetes")},
        "medication_present": {"medication": MedicationCriterion(medication_text="metformin")},
        "medication_absent": {"medication": MedicationCriterion(medication_text="metformin")},
        "measurement_threshold": {
            "measurement": MeasurementCriterion(
                measurement_text="hba1c",
                operator=">=",
                value=7.0,
                value_low=None,
                value_high=None,
                unit="%",
            )
        },
        "temporal_window": {
            "temporal_window": TemporalWindowCriterion(
                event_text="mi", window_days=180, direction="within_past"
            )
        },
        "free_text": {"free_text": FreeTextCriterion(note="needs review")},
    }
    payloads.update(table[kind])
    row = ExtractedCriterion.model_validate(
        dict(
            kind=kind,
            polarity="inclusion",
            source_text="some text",
            negated=False,
            mood="actual",
            mentions=[],
            **payloads,
        )
    )
    dumped = row.model_dump_json()
    restored = ExtractedCriterion.model_validate_json(dumped)
    assert restored == row


# ---------- field validation ----------


def test_invalid_kind_is_rejected():
    """A misspelled or new kind should fail validation, not silently
    pass — that's the whole point of using Literal."""
    with pytest.raises(ValidationError):
        ExtractedCriterion.model_validate(
            {
                "kind": "totally_made_up_kind",
                "polarity": "inclusion",
                "source_text": "x",
                "negated": False,
                "mood": "actual",
                "mentions": [],
                "age": None,
                "sex": None,
                "condition": None,
                "medication": None,
                "measurement": None,
                "temporal_window": None,
                "free_text": {"note": ""},
            }
        )


def test_invalid_polarity_is_rejected():
    with pytest.raises(ValidationError):
        _row(polarity="maybe")


def test_invalid_mood_is_rejected():
    with pytest.raises(ValidationError):
        _row(mood="future")


def test_invalid_entity_type_is_rejected():
    """Same enum-discipline check at the EntityMention level."""
    with pytest.raises(ValidationError):
        EntityMention.model_validate({"text": "foo", "type": "MadeUpType"})


def test_threshold_operator_enum_is_enforced():
    with pytest.raises(ValidationError):
        MeasurementCriterion.model_validate(
            {
                "measurement_text": "hba1c",
                "operator": "approximately",
                "value": 7.0,
                "value_low": None,
                "value_high": None,
                "unit": "%",
            }
        )


# ---------- top-level envelope ----------


def test_empty_extraction_envelope_is_valid():
    """An empty trial (or one with nothing extractable) should be
    representable; the matcher relies on this for empty inputs."""
    env = ExtractedCriteria(criteria=[], metadata=ExtractionMetadata(notes="no eligibility text"))
    assert env.criteria == []
    assert env.metadata.notes == "no eligibility text"


def test_extraction_envelope_with_one_criterion():
    """End-to-end: build a real envelope and confirm round-trip."""
    env = ExtractedCriteria(
        criteria=[_row()],
        metadata=ExtractionMetadata(notes=""),
    )
    restored = ExtractedCriteria.model_validate_json(env.model_dump_json())
    assert restored == env


# ---------- run metadata ----------


def test_extractor_run_meta_allows_nullables():
    """`ExtractorRunMeta` is the *observed* metadata; tokens, cost,
    and latency can be unknown when the API doesn't report them or
    when the call short-circuits (empty input). Don't lock them down."""
    meta = ExtractorRunMeta(model="gpt-4o-mini", prompt_version="extractor-v0.1")
    assert meta.input_tokens is None
    assert meta.output_tokens is None
    assert meta.cost_usd is None
    assert meta.latency_ms is None


# ---------- OpenAI strict-mode acceptance ----------


def test_schema_compiles_to_openai_strict_response_format():
    """The whole point of the schema is that it survives OpenAI's
    strict-mode JSON-schema validator. The SDK's helper raises if it
    doesn't, so calling it is a sufficient compile-time check."""
    fmt: Any = type_to_response_format_param(ExtractedCriteria)
    assert fmt["type"] == "json_schema"
    js = fmt["json_schema"]
    assert js["strict"] is True
    schema = js["schema"]
    # Spot-check structural invariants the strict validator enforces.
    assert schema["additionalProperties"] is False
    assert "ExtractedCriterion" in schema["$defs"]
    crit = schema["$defs"]["ExtractedCriterion"]
    # Every property must be required under strict mode.
    assert set(crit["required"]) == set(crit["properties"].keys())

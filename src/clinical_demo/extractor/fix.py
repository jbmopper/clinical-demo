"""Deterministic criterion fixing between extraction and matching.

This is the first slice of PLAN 2.21. The extractor can produce a
matcher-shaped row that is still not quite matchable: a temporal row
whose event is really just a diagnosis, a measurement surface that
needs a conventional name, or a composite phrase that should not be
sent to the atomic terminology mapper.

The fixer is intentionally conservative. It preserves the original
`source_text`, rewrites only high-confidence cases, and routes unsafe
composites to `free_text` / human review instead of pretending they are
atomic concepts.
"""

from __future__ import annotations

from dataclasses import dataclass

from clinical_demo.criterion_review import is_interview_required_criterion_text

from .composite import build_composite_criterion_groups
from .schema import (
    CompositeCriterionGroup,
    ConditionCriterion,
    ExtractedCriteria,
    ExtractedCriterion,
    ExtractionMetadata,
    FreeTextCriterion,
    MeasurementCriterion,
)

CRITERION_FIX_NOTE_PREFIX = "criterion_fix"


@dataclass(frozen=True)
class _FixResult:
    criteria: list[ExtractedCriterion]
    note: str | None = None


def fix_extracted_criteria(extracted: ExtractedCriteria) -> ExtractedCriteria:
    """Return a matcher-friendlier extraction without mutating input."""

    fixed: list[ExtractedCriterion] = []
    notes: list[str] = []
    for index, criterion in enumerate(extracted.criteria):
        result = _fix_one(criterion)
        fixed.extend(result.criteria)
        if result.note:
            notes.append(f"criterion[{index}]: {result.note}")

    composite_groups = _merge_composite_groups(
        existing=extracted.composite_groups,
        criteria=fixed,
    )
    if composite_groups != extracted.composite_groups:
        notes.append(f"emitted {len(composite_groups)} native composite group(s)")

    if not notes:
        return extracted

    return ExtractedCriteria(
        criteria=fixed,
        composite_groups=composite_groups,
        metadata=ExtractionMetadata(notes=_append_notes(extracted.metadata.notes, notes)),
    )


def _fix_one(criterion: ExtractedCriterion) -> _FixResult:
    if criterion.kind == "free_text" and is_interview_required_criterion_text(
        criterion.source_text
    ):
        fixed = criterion.model_copy(
            deep=True,
            update={
                "free_text": FreeTextCriterion(
                    note=(
                        f"{CRITERION_FIX_NOTE_PREFIX}: interview_required: "
                        "criterion asks for trial participation information outside "
                        "structured FHIR evidence"
                    )
                )
            },
        )
        return _FixResult(
            criteria=[fixed],
            note=f"marked free_text criterion {criterion.source_text!r} as interview_required",
        )

    if _is_unsafe_composite(criterion):
        surface = _surface_text(criterion) or criterion.source_text
        return _FixResult(
            criteria=[
                _to_free_text(
                    criterion,
                    f"unsafe composite requires note/free-text evidence review; "
                    f"original_kind={criterion.kind}; surface={surface!r}",
                )
            ],
            note=f"routed unsafe composite {surface!r} to free_text",
        )

    if criterion.kind in {"condition_present", "condition_absent"} and criterion.condition:
        normalized = _normalize_surface(criterion.condition.condition_text)
        replacement = _CONDITION_SURFACE_FIXES.get(normalized)
        if replacement and replacement != criterion.condition.condition_text:
            return _FixResult(
                criteria=[
                    criterion.model_copy(
                        deep=True,
                        update={"condition": ConditionCriterion(condition_text=replacement)},
                    )
                ],
                note=(
                    f"normalized condition surface {criterion.condition.condition_text!r} "
                    f"to {replacement!r}"
                ),
            )

    if criterion.kind == "measurement_threshold" and criterion.measurement:
        fixed_measurement, note = _fix_measurement(criterion.measurement, criterion.source_text)
        if fixed_measurement is not None:
            return _FixResult(
                criteria=[
                    criterion.model_copy(
                        deep=True,
                        update={"measurement": fixed_measurement},
                    )
                ],
                note=note,
            )

    if criterion.kind == "temporal_window" and criterion.temporal_window:
        fixed_condition = _fix_temporal_window_to_condition(criterion)
        if fixed_condition is not None:
            return fixed_condition

    return _FixResult(criteria=[criterion])


def _fix_temporal_window_to_condition(criterion: ExtractedCriterion) -> _FixResult | None:
    temporal = criterion.temporal_window
    if temporal is None:
        return None
    normalized = _normalize_surface(temporal.event_text)
    replacement = _CONDITION_SURFACE_FIXES.get(normalized)
    if replacement is None:
        return None
    if temporal.window_days != 0 and "diagnosis" not in normalized:
        return None

    kind = "condition_absent" if criterion.negated else "condition_present"
    fixed = ExtractedCriterion(
        kind=kind,  # type: ignore[arg-type]
        polarity=criterion.polarity,
        source_text=criterion.source_text,
        negated=criterion.negated,
        mood="historical" if criterion.mood == "historical" else "actual",
        age=None,
        sex=None,
        condition=ConditionCriterion(condition_text=replacement),
        medication=None,
        measurement=None,
        temporal_window=None,
        free_text=None,
        mentions=criterion.mentions,
    )
    return _FixResult(
        criteria=[fixed],
        note=f"converted temporal event {temporal.event_text!r} to {kind} {replacement!r}",
    )


def _fix_measurement(
    measurement: MeasurementCriterion,
    source_text: str,
) -> tuple[MeasurementCriterion | None, str | None]:
    normalized = _normalize_surface(measurement.measurement_text)
    replacement = _MEASUREMENT_SURFACE_FIXES.get(normalized)
    if replacement is None and normalized in {"blood pressure", "bp"}:
        source = source_text.lower()
        if "systolic" in source:
            replacement = "systolic blood pressure"
        elif "diastolic" in source:
            replacement = "diastolic blood pressure"

    if replacement is None or replacement == measurement.measurement_text:
        return None, None

    fixed = measurement.model_copy(update={"measurement_text": replacement})
    return (
        fixed,
        f"normalized measurement surface {measurement.measurement_text!r} to {replacement!r}",
    )


def _to_free_text(criterion: ExtractedCriterion, reason: str) -> ExtractedCriterion:
    return ExtractedCriterion(
        kind="free_text",
        polarity=criterion.polarity,
        source_text=criterion.source_text,
        negated=criterion.negated,
        mood=criterion.mood,
        age=None,
        sex=None,
        condition=None,
        medication=None,
        measurement=None,
        temporal_window=None,
        free_text=FreeTextCriterion(note=f"{CRITERION_FIX_NOTE_PREFIX}: {reason}"),
        mentions=criterion.mentions,
    )


def _is_unsafe_composite(criterion: ExtractedCriterion) -> bool:
    if criterion.kind in {"age", "sex", "free_text"}:
        return False
    surface = _surface_text(criterion)
    if not surface:
        return False
    normalized = f" {_normalize_surface(surface)} "
    return any(token in normalized for token in (" and ", " or ", ",", ";", "/"))


def _surface_text(criterion: ExtractedCriterion) -> str | None:
    if criterion.condition is not None:
        return criterion.condition.condition_text
    if criterion.medication is not None:
        return criterion.medication.medication_text
    if criterion.measurement is not None:
        return criterion.measurement.measurement_text
    if criterion.temporal_window is not None:
        return criterion.temporal_window.event_text
    return None


def _normalize_surface(surface: str) -> str:
    return " ".join(surface.lower().strip(".,;:()[]{}\"'").split())


def _append_notes(existing: str, notes: list[str]) -> str:
    suffix = "; ".join(f"{CRITERION_FIX_NOTE_PREFIX}: {note}" for note in notes)
    if not existing:
        return suffix
    return f"{existing}; {suffix}"


def _merge_composite_groups(
    *,
    existing: list[CompositeCriterionGroup],
    criteria: list[ExtractedCriterion],
) -> list[CompositeCriterionGroup]:
    groups = [*existing]
    seen = {(group.parent_criterion_index, group.parent_source_text) for group in groups}
    for index, criterion in enumerate(criteria):
        for group in build_composite_criterion_groups(criterion, criterion_index=index):
            key = (group.parent_criterion_index, group.parent_source_text)
            if key in seen:
                continue
            groups.append(group)
            seen.add(key)
    return groups


_CONDITION_SURFACE_FIXES: dict[str, str] = {
    "mild to moderate hypertension": "hypertension",
    "poorly controlled hypertension": "hypertension",
    "t1d": "type 1 diabetes",
    "t1d diagnosis": "type 1 diabetes",
    "type 1 diabetes diagnosis": "type 1 diabetes",
    "uncontrolled hypertension": "hypertension",
}

_MEASUREMENT_SURFACE_FIXES: dict[str, str] = {
    "c peptide": "C-peptide",
    "c peptide concentration": "C-peptide",
    "c peptide concentrations": "C-peptide",
    "c-peptide concentration": "C-peptide",
    "c-peptide concentrations": "C-peptide",
}


__all__ = [
    "CRITERION_FIX_NOTE_PREFIX",
    "fix_extracted_criteria",
]

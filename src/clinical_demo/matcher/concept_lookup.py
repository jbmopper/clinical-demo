"""Surface-form → ConceptSet lookup with terminology-or-alias dispatch.

The extractor produces criterion payloads with surface text like
`"hba1c"`, `"type 2 diabetes"`, `"metformin"`. The matcher needs to
convert those into coded concept sets so it can query the
`PatientProfile`. Two paths exist; `Settings.binding_strategy`
selects between them:

- **`alias` (default, legacy):** the hand-curated `_*_ALIASES`
  tables in this module are the only source. Fully offline, no NLM
  dependency, fully auditable in a 30-second read.
- **`two_pass` (D-69 slice 4):** consult the trial-side bindings
  registry first (`terminology.bindings`) -> resolver
  (`terminology.resolver`) -> `TerminologyCache` -> live VSAC /
  RxNorm. Soft-fail to the alias table on registry miss or any
  terminology-side error. Aliases stay as a safety net during
  migration so behaviour never degrades below the `alias` baseline.

Either way, anything that survives both paths maps to
`unmapped_concept` and the matcher returns `indeterminate` -- the
*honest* signal that the system doesn't know, which is more useful
than a fuzzy match that pretends to know. The D-68 baseline
diagnostic surfaced `unmapped_concept` as the largest source of
indeterminacy (89% of all indeterminate verdicts on the 2026-04-21
baseline); D-69 slice 4 is the wire-up that lets the eval rerun
measure how much of that gap a small bindings registry actually
closes.
"""

from __future__ import annotations

from clinical_demo.profile import ConceptSet
from clinical_demo.profile.concept_sets import (
    CHRONIC_KIDNEY_DISEASE,
    DIASTOLIC_BP,
    EGFR,
    HBA1C,
    HYPERLIPIDEMIA,
    HYPERTENSION,
    LDL_CHOLESTEROL,
    PREDIABETES,
    SYSTOLIC_BP,
    T2DM,
)
from clinical_demo.settings import get_settings
from clinical_demo.terminology.resolver import TerminologyResolver, get_resolver


def _normalize(s: str) -> str:
    """Lowercase, collapse internal whitespace, strip punctuation
    that the LLM sometimes appends.

    Mirrors the prompt's "lowercase surface forms" instruction so a
    well-behaved extractor's output flows through verbatim."""
    return " ".join(s.lower().strip(".,;:()[]{}\"'").split())


# Conditions: surface-form aliases → ConceptSet.
# Each ConceptSet target may have many aliases; we match on the
# normalized surface form.
_CONDITION_ALIASES: dict[str, ConceptSet] = {
    # T2DM
    "type 2 diabetes": T2DM,
    "type 2 diabetes mellitus": T2DM,
    "t2dm": T2DM,
    "type ii diabetes": T2DM,
    "diabetes mellitus type 2": T2DM,
    # Prediabetes
    "prediabetes": PREDIABETES,
    "pre-diabetes": PREDIABETES,
    "impaired fasting glucose": PREDIABETES,
    # Hypertension
    "hypertension": HYPERTENSION,
    "essential hypertension": HYPERTENSION,
    "high blood pressure": HYPERTENSION,
    "htn": HYPERTENSION,
    # Hyperlipidemia
    "hyperlipidemia": HYPERLIPIDEMIA,
    "hyperlipidaemia": HYPERLIPIDEMIA,
    "hypercholesterolemia": HYPERLIPIDEMIA,
    "high cholesterol": HYPERLIPIDEMIA,
    "dyslipidemia": HYPERLIPIDEMIA,
    # CKD
    "chronic kidney disease": CHRONIC_KIDNEY_DISEASE,
    "ckd": CHRONIC_KIDNEY_DISEASE,
    "renal disease": CHRONIC_KIDNEY_DISEASE,
    "kidney disease": CHRONIC_KIDNEY_DISEASE,
}

_LAB_ALIASES: dict[str, ConceptSet] = {
    # HbA1c
    "hba1c": HBA1C,
    "hemoglobin a1c": HBA1C,
    "haemoglobin a1c": HBA1C,
    "a1c": HBA1C,
    "glycated hemoglobin": HBA1C,
    "glycosylated hemoglobin": HBA1C,
    # LDL
    "ldl": LDL_CHOLESTEROL,
    "ldl cholesterol": LDL_CHOLESTEROL,
    "ldl-c": LDL_CHOLESTEROL,
    "low-density lipoprotein cholesterol": LDL_CHOLESTEROL,
    "low density lipoprotein cholesterol": LDL_CHOLESTEROL,
    # eGFR
    "egfr": EGFR,
    "estimated glomerular filtration rate": EGFR,
    "estimated gfr": EGFR,
    "gfr": EGFR,
    # BP
    "systolic blood pressure": SYSTOLIC_BP,
    "systolic bp": SYSTOLIC_BP,
    "sbp": SYSTOLIC_BP,
    "diastolic blood pressure": DIASTOLIC_BP,
    "diastolic bp": DIASTOLIC_BP,
    "dbp": DIASTOLIC_BP,
}

# Medications are intentionally NOT mapped in v0. The Synthea cohort
# has very limited medication coverage and our SNOMED/RxNorm mapping
# work hasn't been done; honest "unmapped_concept" is better than
# pretending. See PLAN.md decision log.
_MEDICATION_ALIASES: dict[str, ConceptSet] = {}


def lookup_condition(
    surface: str, *, resolver: TerminologyResolver | None = None
) -> ConceptSet | None:
    """Return the ConceptSet for a condition surface form, or None.

    Matches case-insensitively on a lightly-normalized form. None
    means the matcher should emit `indeterminate (unmapped_concept)`.

    When `Settings.binding_strategy == "two_pass"`, the terminology
    resolver is consulted first; on registry miss or terminology-
    side soft-fail, dispatch falls through to the alias table.

    `resolver` is exposed as a kwarg purely for tests, which can
    inject a resolver wired against a temp cache to exercise the
    two_pass branch without touching the global singleton or live
    NLM endpoints. Production callers leave it `None`."""
    if get_settings().binding_strategy == "two_pass":
        r = resolver or get_resolver()
        bound = r.resolve_condition(surface)
        if bound is not None:
            return bound
    return _CONDITION_ALIASES.get(_normalize(surface))


def lookup_lab(surface: str, *, resolver: TerminologyResolver | None = None) -> ConceptSet | None:
    """Return the ConceptSet for a lab/measurement surface form, or None.

    Same alias-then-terminology dispatch shape as `lookup_condition`."""
    if get_settings().binding_strategy == "two_pass":
        r = resolver or get_resolver()
        bound = r.resolve_lab(surface)
        if bound is not None:
            return bound
    return _LAB_ALIASES.get(_normalize(surface))


def lookup_medication(
    surface: str, *, resolver: TerminologyResolver | None = None
) -> ConceptSet | None:
    """Return the ConceptSet for a medication surface form, or None.

    The alias table is intentionally empty in v0 (Synthea med
    coverage is thin); under `two_pass` the RxNorm-backed resolver
    is the only path that can return a non-`None` result here."""
    if get_settings().binding_strategy == "two_pass":
        r = resolver or get_resolver()
        bound = r.resolve_medication(surface)
        if bound is not None:
            return bound
    return _MEDICATION_ALIASES.get(_normalize(surface))


__all__ = [
    "lookup_condition",
    "lookup_lab",
    "lookup_medication",
]

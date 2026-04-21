"""Surface-form → ConceptSet lookup tables for the matcher v0.

The extractor produces criterion payloads with surface text like
`"hba1c"`, `"type 2 diabetes"`, `"metformin"`. The matcher needs to
convert those into coded concept sets so it can query the
`PatientProfile`. v0 does this with deliberately small hand-curated
tables, not with any NLP/LLM mapping:

- The matcher's behavior is fully traceable — a reviewer can read
  this file in 30 seconds and see every concept the matcher
  recognizes.
- Anything not in the table maps to `unmapped_concept` and the
  matcher returns `indeterminate`. This is the *honest* signal:
  the matcher saying "I don't know" is more useful than a fuzzy
  match that pretends to know.
- Phase 2 (or beyond) is where we'd plug in UMLS/RxNorm normalization
  or an embedding-based match. The shape of those upgrades is to
  replace the lookup function, not the schema around it.
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


def lookup_condition(surface: str) -> ConceptSet | None:
    """Return the ConceptSet for a condition surface form, or None.

    Matches case-insensitively on a lightly-normalized form. None
    means the matcher should emit `indeterminate (unmapped_concept)`."""
    return _CONDITION_ALIASES.get(_normalize(surface))


def lookup_lab(surface: str) -> ConceptSet | None:
    """Return the ConceptSet for a lab/measurement surface form, or None."""
    return _LAB_ALIASES.get(_normalize(surface))


def lookup_medication(surface: str) -> ConceptSet | None:
    """Return the ConceptSet for a medication surface form, or None.

    v0 always returns None — see module docstring."""
    return _MEDICATION_ALIASES.get(_normalize(surface))


__all__ = [
    "lookup_condition",
    "lookup_lab",
    "lookup_medication",
]

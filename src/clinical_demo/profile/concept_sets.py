"""Curated concept sets for the matcher and the eval seed labeler.

**Status: legacy curated code lists.** D-69 adds the first NLM API
client for VSAC value-set expansion, but these constants remain the
matcher-wired source of truth until a terminology resolver is wired
through `concept_lookup.py` and the eval harness.

A `ConceptSet` is a *named* group of coded concepts (SNOMED conditions,
LOINC labs, RxNorm meds). The matcher uses these to answer questions
like "does the patient have any T2DM-coded condition" without
hard-coding code lists everywhere.

Keep this file small and curated. We expand it deliberately when a
new criterion type appears in our seed-set or in a real trial we care
about — not preemptively. The point is auditability: a reviewer can
read this file in a minute and see exactly which codes the matcher
treats as evidence of which clinical concept.
"""

from __future__ import annotations

from clinical_demo.profile.profile import ConceptSet

SNOMED = "http://snomed.info/sct"
LOINC = "http://loinc.org"

# ---- conditions (SNOMED) ----

T2DM = ConceptSet(
    name="Type 2 diabetes mellitus",
    system=SNOMED,
    codes=frozenset(
        {
            "44054006",  # Diabetes mellitus type 2
            "73211009",  # Diabetes mellitus (unspecified) — Synthea uses this
        }
    ),
)

PREDIABETES = ConceptSet(
    name="Prediabetes",
    system=SNOMED,
    codes=frozenset({"15777000"}),
)

HYPERTENSION = ConceptSet(
    name="Essential hypertension",
    system=SNOMED,
    codes=frozenset(
        {
            "59621000",  # Essential hypertension
            "38341003",  # Hypertensive disorder
        }
    ),
)

HYPERLIPIDEMIA = ConceptSet(
    name="Hyperlipidemia",
    system=SNOMED,
    codes=frozenset(
        {
            "55822004",  # Hyperlipidemia
            "267432004",  # Pure hypercholesterolemia
        }
    ),
)

CHRONIC_KIDNEY_DISEASE = ConceptSet(
    name="Chronic kidney disease",
    system=SNOMED,
    codes=frozenset(
        {
            "431855005",  # Chronic kidney disease stage 1
            "431856006",  # Chronic kidney disease stage 2
            "433144002",  # Chronic kidney disease stage 3
            "431857002",  # Chronic kidney disease stage 4
            "433146000",  # Chronic kidney disease stage 5
        }
    ),
)

# ---- labs (LOINC) ----
# These are *single-code* sets at v0 — included as ConceptSets for API
# uniformity with conditions; expand to multi-code synonyms only if we
# see a real trial demanding them.

HBA1C = ConceptSet(
    name="HbA1c (hemoglobin A1c)",
    system=LOINC,
    codes=frozenset({"4548-4"}),
)

LDL_CHOLESTEROL = ConceptSet(
    name="LDL cholesterol",
    system=LOINC,
    codes=frozenset({"18262-6"}),
)

EGFR = ConceptSet(
    name="Estimated glomerular filtration rate (eGFR)",
    system=LOINC,
    codes=frozenset({"33914-3"}),
)

SYSTOLIC_BP = ConceptSet(
    name="Systolic blood pressure",
    system=LOINC,
    codes=frozenset({"8480-6"}),
)

DIASTOLIC_BP = ConceptSet(
    name="Diastolic blood pressure",
    system=LOINC,
    codes=frozenset({"8462-4"}),
)

__all__ = [
    "CHRONIC_KIDNEY_DISEASE",
    "DIASTOLIC_BP",
    "EGFR",
    "HBA1C",
    "HYPERLIPIDEMIA",
    "HYPERTENSION",
    "LDL_CHOLESTEROL",
    "PREDIABETES",
    "SYSTOLIC_BP",
    "T2DM",
]

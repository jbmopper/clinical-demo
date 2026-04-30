"""NLM terminology API clients for the D-69 binding-strategy comparison.

This package exists to back arms B (`one_pass`) and C (`two_pass`) of
the surface-form → ConceptSet binding pipeline; arm A (the
hand-curated baseline at `clinical_demo.matcher.concept_lookup` and
`clinical_demo.profile.concept_sets`) does not depend on anything
here. See PLAN.md §12 D-69 for the full comparison.

v0 surface (this PR): a `VSACClient` that resolves a single
value-set OID against the VSAC FHIR `$expand` endpoint and returns
a `ConceptSet` shaped exactly like the hand-curated constants — so
the matcher's existing dispatch keeps working when the value-set
membership comes from VSAC instead of `concept_sets.py`. RxNorm and
UMLS clients land alongside this when the extractor prompt grows
the OID/RxCUI emission for arms B and C.
"""

from __future__ import annotations

from clinical_demo.terminology.bindings import (
    CONDITION_BINDINGS,
    ECQM_DIABETES_OID,
    ECQM_HBA1C_LAB_OID,
    ECQM_HYPERTENSION_OID,
    LAB_BINDINGS,
    LOINC_SYSTEM,
    MEDICATION_BINDINGS,
    SNOMED_SYSTEM,
    Binding,
    RxNormBinding,
    VSACBinding,
    lookup_condition_binding,
    lookup_lab_binding,
    lookup_medication_binding,
)
from clinical_demo.terminology.cache import (
    StoredRxNormConcepts,
    StoredVSACExpansion,
    TerminologyCache,
    cache_path_for_rxnorm,
    cache_path_for_vsac,
    rxnorm_envelope_fingerprint,
    vsac_envelope_fingerprint,
)
from clinical_demo.terminology.resolver import (
    TerminologyResolver,
    get_resolver,
)
from clinical_demo.terminology.rxnorm_client import (
    RxNormClient,
    RxNormConcepts,
    RxNormError,
)
from clinical_demo.terminology.vsac_client import (
    VSACClient,
    VSACError,
    VSACExpansion,
)

__all__ = [
    "CONDITION_BINDINGS",
    "ECQM_DIABETES_OID",
    "ECQM_HBA1C_LAB_OID",
    "ECQM_HYPERTENSION_OID",
    "LAB_BINDINGS",
    "LOINC_SYSTEM",
    "MEDICATION_BINDINGS",
    "SNOMED_SYSTEM",
    "Binding",
    "RxNormBinding",
    "RxNormClient",
    "RxNormConcepts",
    "RxNormError",
    "StoredRxNormConcepts",
    "StoredVSACExpansion",
    "TerminologyCache",
    "TerminologyResolver",
    "VSACBinding",
    "VSACClient",
    "VSACError",
    "VSACExpansion",
    "cache_path_for_rxnorm",
    "cache_path_for_vsac",
    "get_resolver",
    "lookup_condition_binding",
    "lookup_lab_binding",
    "lookup_medication_binding",
    "rxnorm_envelope_fingerprint",
    "vsac_envelope_fingerprint",
]

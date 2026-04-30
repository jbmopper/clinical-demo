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

from clinical_demo.terminology.vsac_client import (
    VSACClient,
    VSACError,
    VSACExpansion,
)

__all__ = [
    "VSACClient",
    "VSACError",
    "VSACExpansion",
]

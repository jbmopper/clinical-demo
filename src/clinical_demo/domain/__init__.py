"""Internal domain model.

This package is the boundary between data sources (FHIR, Chia, CT.gov) and
the rest of the system. Nothing in `domain/` should know about FHIR or any
specific source format. Translation happens in `clinical_demo.data.*`.
"""

from clinical_demo.domain.patient import (
    CodedConcept,
    Condition,
    LabObservation,
    Medication,
    Patient,
    Sex,
)
from clinical_demo.domain.trial import Trial

__all__ = [
    "CodedConcept",
    "Condition",
    "LabObservation",
    "Medication",
    "Patient",
    "Sex",
    "Trial",
]

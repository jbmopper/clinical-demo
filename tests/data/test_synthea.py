"""Tests for the Synthea FHIR loader.

Uses one real bundle (`tests/fixtures/synthea/francisco.json`) extracted from
the upstream Synthea sample data so we exercise the actual schema rather
than a hand-rolled stub.

Coverage gaps (intentional, for v0):
- Bundles where `MedicationRequest` uses `medicationReference` instead of
  inline `medicationCodeableConcept`. Add when a downstream test needs it.
- Conditions categorized as `social-history` (so `is_clinical=False`). The
  fixture happens not to contain one; tested via a fabricated minimal case.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from clinical_demo.data.synthea import (
    _patient_from_bundle,
    iter_bundles,
    load_bundle,
)
from clinical_demo.domain import Patient

FIXTURE = Path(__file__).parent.parent / "fixtures" / "synthea" / "francisco.json"


@pytest.fixture(scope="module")
def francisco() -> Patient:
    return load_bundle(FIXTURE)


def test_loads_patient_demographics(francisco: Patient) -> None:
    assert francisco.patient_id == "ce032ded-978b-b56b-425f-5159d4a4038e"
    assert francisco.sex == "male"
    assert francisco.birth_date == date(1988, 1, 30)


def test_loads_conditions(francisco: Patient) -> None:
    assert len(francisco.conditions) == 3
    snomed_codes = {c.concept.code for c in francisco.conditions}
    assert {"128613002", "84757009", "703151001"} == snomed_codes
    for c in francisco.conditions:
        assert c.concept.system == "http://snomed.info/sct"
        assert c.is_clinical is True
        assert c.onset_date == date(1988, 5, 21)
        assert c.abatement_date is None


def test_loads_observations_with_loinc_and_units(francisco: Patient) -> None:
    obs = francisco.observations
    assert len(obs) >= 5
    body_heights = [o for o in obs if o.concept.code == "8302-2"]
    assert body_heights, "expected at least one body-height observation"
    h = body_heights[0]
    assert h.concept.system == "http://loinc.org"
    assert h.unit == "cm"
    assert h.value > 0


def test_loads_medication_with_inline_concept(francisco: Patient) -> None:
    assert len(francisco.medications) == 1
    med = francisco.medications[0]
    assert med.concept.code == "197591"  # RxNorm diazepam
    assert med.start_date == date(1988, 5, 21)


def test_age_years_handles_birthday_not_yet_reached(francisco: Patient) -> None:
    assert francisco.age_years(date(2020, 1, 30)) == 32  # birthday today
    assert francisco.age_years(date(2020, 1, 29)) == 31  # day before
    assert francisco.age_years(date(2020, 12, 31)) == 32


def test_active_conditions_filters_by_as_of(francisco: Patient) -> None:
    before_onset = date(1988, 5, 1)
    after_onset = date(1990, 1, 1)
    assert francisco.active_conditions(before_onset) == []
    assert len(francisco.active_conditions(after_onset)) == 3


def test_active_conditions_excludes_non_clinical() -> None:
    """Synthea models social findings as Conditions; verify we filter them."""
    fabricated = {
        "entry": [
            {
                "resource": {
                    "resourceType": "Patient",
                    "id": "p1",
                    "gender": "female",
                    "birthDate": "1970-01-01",
                }
            },
            {
                "resource": {
                    "resourceType": "Condition",
                    "id": "c1",
                    "category": [
                        {
                            "coding": [
                                {
                                    "system": "http://terminology.hl7.org/CodeSystem/condition-category",
                                    "code": "social-history",
                                }
                            ]
                        }
                    ],
                    "code": {
                        "coding": [
                            {
                                "system": "http://snomed.info/sct",
                                "code": "224299000",
                                "display": "Received higher education",
                            }
                        ]
                    },
                    "onsetDateTime": "1990-01-01T00:00:00Z",
                }
            },
        ]
    }
    p = _patient_from_bundle(fabricated)
    assert len(p.conditions) == 1
    assert p.conditions[0].is_clinical is False
    assert p.active_conditions(date(2020, 1, 1)) == []


def test_latest_observation_respects_as_of(francisco: Patient) -> None:
    very_early = date(1980, 1, 1)
    assert francisco.latest_observation("8302-2", very_early) is None
    later = francisco.latest_observation("8302-2", date(2025, 1, 1))
    assert later is not None
    assert later.concept.code == "8302-2"


def test_iter_bundles_yields_patients(tmp_path: Path) -> None:
    fixture_dir = tmp_path / "synthea"
    fixture_dir.mkdir()
    (fixture_dir / "a.json").write_bytes(FIXTURE.read_bytes())
    (fixture_dir / "b.json").write_bytes(FIXTURE.read_bytes())
    patients = list(iter_bundles(fixture_dir))
    assert len(patients) == 2
    assert all(isinstance(p, Patient) for p in patients)


def test_iter_bundles_skips_non_patient_bundles(tmp_path: Path) -> None:
    """Synthea sample dumps include hospital/practitioner-only bundles."""
    import json as _json

    fixture_dir = tmp_path / "synthea"
    fixture_dir.mkdir()
    (fixture_dir / "patient.json").write_bytes(FIXTURE.read_bytes())
    (fixture_dir / "hospitalInformation.json").write_text(
        _json.dumps(
            {
                "resourceType": "Bundle",
                "type": "transaction",
                "entry": [
                    {
                        "resource": {
                            "resourceType": "Organization",
                            "id": "org-1",
                            "name": "Some Hospital",
                        }
                    }
                ],
            }
        )
    )
    patients = list(iter_bundles(fixture_dir))
    assert len(patients) == 1


def test_load_bundle_raises_on_empty() -> None:
    with pytest.raises(ValueError, match="no entries"):
        _patient_from_bundle({"entry": []})

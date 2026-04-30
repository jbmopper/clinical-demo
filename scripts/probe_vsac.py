"""One-off probe of the VSAC FHIR `$expand` endpoint.

Use this to (a) sanity-check your UMLS_API_KEY against the live
server, and (b) re-record `tests/fixtures/vsac/diabetes_expansion.json`
when the eCQM Diabetes value set is republished or the fixture
otherwise drifts. The unit-test suite runs offline against the
recorded fixture; this script is the only thing in the repo that
hits VSAC for real.

    uv run python scripts/probe_vsac.py
    uv run python scripts/probe_vsac.py --oid 2.16.840.1.113883.3.464.1003.196.12.1257
    uv run python scripts/probe_vsac.py --record  # overwrite fixture

Requires `UMLS_API_KEY` in `.env` (see PLAN.md §12 D-69).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from clinical_demo.terminology import VSACClient, VSACError

# eCQM "Diabetes" — the broad, screening-friendly value set used as
# the v0 D-69 baseline OID for the Diabetes concept (D-69 question 3).
DEFAULT_OID = "2.16.840.1.113883.3.464.1003.103.12.1001"
DEFAULT_NAME = "Diabetes"
FIXTURE_PATH = (
    Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "vsac" / "diabetes_expansion.json"
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--oid", default=DEFAULT_OID)
    parser.add_argument("--name", default=DEFAULT_NAME)
    parser.add_argument(
        "--system-filter",
        default="http://snomed.info/sct",
        help=(
            "Restrict the parsed expansion to one coding system. Many eCQM value "
            "sets span SNOMED + ICD-10-CM; the patient profile is single-system "
            "per query, so the matcher always asks for one. Pass empty string to "
            "disable and surface multi-system value sets as VSACError."
        ),
    )
    parser.add_argument(
        "--record",
        action="store_true",
        help="Overwrite the on-disk test fixture with the raw FHIR ValueSet payload.",
    )
    args = parser.parse_args()

    client = VSACClient()

    if args.record:
        # `record` needs the raw VSAC payload (not our parsed envelope)
        # so the unit tests exercise the same parser path against a
        # real response shape. Drop down to httpx for this branch.
        import httpx

        bare_oid = args.oid.removeprefix("urn:oid:")
        with httpx.Client(auth=client._auth, timeout=client._timeout) as http:
            response = http.get(f"{client._base_url}/ValueSet/{bare_oid}/$expand")
        if response.status_code != 200:
            print(f"VSAC returned {response.status_code}: {response.text[:200]}", file=sys.stderr)
            return 1
        payload = response.json()
        FIXTURE_PATH.write_text(json.dumps(payload, indent=2) + "\n")
        print(
            f"Wrote {FIXTURE_PATH} ({len(payload.get('expansion', {}).get('contains', []))} concepts)"
        )
        return 0

    try:
        expansion = client.expand(
            args.oid,
            name=args.name,
            system_filter=args.system_filter or None,
        )
    except VSACError as exc:
        print(f"VSAC error: {exc}", file=sys.stderr)
        return 1

    print(f"OID:     {expansion.oid}")
    print(f"Version: {expansion.version}")
    print(f"Name:    {expansion.concept_set.name}")
    print(f"System:  {expansion.concept_set.system}")
    print(f"Codes:   {len(expansion.concept_set.codes)}")
    for code in sorted(expansion.concept_set.codes):
        print(f"  {code}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

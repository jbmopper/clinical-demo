"""One-off probe of the RxNav `/drugs.json` endpoint.

Use this to (a) sanity-check live RxNav availability without
running the full eval, and (b) re-record
`tests/fixtures/rxnorm/metformin_drugs.json` when the RxNav data
set is republished or the fixture otherwise drifts. The unit-test
suite runs offline against the recorded fixture; this script is
the only thing in the repo that hits RxNav for real.

    uv run python scripts/probe_rxnorm.py
    uv run python scripts/probe_rxnorm.py --name "Glucophage"
    uv run python scripts/probe_rxnorm.py --tty IN --tty PIN
    uv run python scripts/probe_rxnorm.py --record  # overwrite fixture

No API key is required (RxNav is a public, key-less surface; see
`clinical_demo.terminology.rxnorm_client` module docstring).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

from clinical_demo.terminology import RxNormClient, RxNormError
from clinical_demo.terminology.rxnorm_client import DEFAULT_BASE_URL

DEFAULT_NAME = "metformin"
FIXTURE_PATH = (
    Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "rxnorm" / "metformin_drugs.json"
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default=DEFAULT_NAME)
    parser.add_argument(
        "--tty",
        action="append",
        default=None,
        help=(
            "Restrict the parsed result to the given RxNorm term type(s). "
            "Pass multiple times: --tty IN --tty PIN. "
            "Omit to union codes across every term type returned by RxNav."
        ),
    )
    parser.add_argument(
        "--record",
        action="store_true",
        help="Overwrite the on-disk test fixture with the raw /drugs.json payload.",
    )
    args = parser.parse_args()

    tty_filter = frozenset(args.tty) if args.tty else None

    if args.record:
        # `record` needs the raw RxNav payload (not our parsed
        # envelope) so the unit tests exercise the same parser path
        # against a real response shape. Drop down to httpx for
        # this branch, mirroring scripts/probe_vsac.py.
        with httpx.Client(timeout=30.0) as http:
            response = http.get(
                f"{DEFAULT_BASE_URL}/drugs.json",
                params={"name": args.name},
            )
        if response.status_code != 200:
            print(
                f"RxNav returned {response.status_code}: {response.text[:200]}",
                file=sys.stderr,
            )
            return 1
        payload = response.json()
        FIXTURE_PATH.write_text(json.dumps(payload, indent=2) + "\n")
        groups = payload.get("drugGroup", {}).get("conceptGroup", [])
        print(f"Wrote {FIXTURE_PATH} ({len(groups)} concept groups)")
        return 0

    client = RxNormClient()
    try:
        result = client.find_drug_concepts(args.name, tty_filter=tty_filter)
    except RxNormError as exc:
        print(f"RxNorm error: {exc}", file=sys.stderr)
        return 1

    print(f"Query:    {result.query}")
    print(f"Name:     {result.concept_set.name}")
    print(f"System:   {result.concept_set.system}")
    print(f"TTYs:     {sorted(result.term_types)}")
    print(f"Codes:    {len(result.concept_set.codes)}")
    for code in sorted(result.concept_set.codes):
        print(f"  {code}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

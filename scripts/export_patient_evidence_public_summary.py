from __future__ import annotations

import argparse
import json
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any

ARTIFACT_SAFETY = {
    "public_export": "sanitized",
    "contains_real_patient_data": False,
    "summary_only": True,
    "omits": [
        "row-level patient evidence",
        "clinical note text",
        "clinical note source identifiers",
        "exact patient identifiers",
        "reviewer free-text rationale",
    ],
}


def main() -> int:
    args = parse_args()
    summary = build_public_summary(
        candidates_spec=args.candidates,
        labels_spec=args.labels,
        diagnostics_spec=args.diagnostics,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export a summary-only public artifact from private patient evidence artifacts."
    )
    parser.add_argument("--candidates", required=True, help="Candidate JSON path or gitref:path.")
    parser.add_argument("--labels", help="Label JSON path or gitref:path.")
    parser.add_argument("--diagnostics", help="Diagnostics JSON path or gitref:path.")
    parser.add_argument("--output", required=True, help="Public summary JSON path.")
    return parser.parse_args()


def build_public_summary(
    *,
    candidates_spec: str,
    labels_spec: str | None = None,
    diagnostics_spec: str | None = None,
) -> dict[str, Any]:
    candidates = load_json_spec(candidates_spec)
    if not isinstance(candidates, list):
        raise ValueError("candidate artifact must be a JSON list")

    summary: dict[str, Any] = {
        "artifact_safety": ARTIFACT_SAFETY,
        "artifact_type": "patient-evidence-calibration-summary",
        "inputs": {
            "candidate_rows": input_summary(candidates_spec, candidates),
        },
        "calibration": summarize_candidates(candidates),
    }

    if labels_spec:
        labels_doc = load_json_spec(labels_spec)
        labels = labels_doc.get("labels", []) if isinstance(labels_doc, dict) else labels_doc
        if not isinstance(labels, list):
            raise ValueError("label artifact must be a JSON list or labels envelope")
        summary["inputs"]["label_templates"] = input_summary(labels_spec, labels)
        summary["labels"] = summarize_labels(labels)

    if diagnostics_spec:
        diagnostics = load_json_spec(diagnostics_spec)
        if not isinstance(diagnostics, dict):
            raise ValueError("diagnostics artifact must be a JSON object")
        summary["inputs"]["diagnostics"] = input_summary(diagnostics_spec, diagnostics)
        summary["diagnostics"] = summarize_diagnostics(diagnostics)

    return summary


def load_json_spec(spec: str) -> Any:
    text = read_spec(spec)
    return json.loads(text)


def read_spec(spec: str) -> str:
    ref, path = split_git_spec(spec)
    if ref is not None:
        result = subprocess.run(
            ["git", "show", f"{ref}:{path}"],
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout
    return Path(spec).read_text()


def split_git_spec(spec: str) -> tuple[str | None, str]:
    left, separator, right = spec.partition(":")
    if not separator or "/" not in right:
        return None, spec
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", left],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None, spec
    return left, right


def input_summary(spec: str, payload: Any) -> dict[str, Any]:
    summary: dict[str, Any] = {"name": spec_name(spec)}
    if isinstance(payload, list):
        summary["records"] = len(payload)
    elif isinstance(payload, dict):
        summary["shape"] = "object"
    return summary


def spec_name(spec: str) -> str:
    return spec.rsplit(":", maxsplit=1)[-1].rsplit("/", maxsplit=1)[-1]


def summarize_candidates(rows: list[dict[str, Any]]) -> dict[str, Any]:
    source_kind_counts: Counter[str] = Counter()
    retrieved_kind_counts: Counter[str] = Counter()
    retrieval_reason_counts: Counter[str] = Counter()
    concept_mapping_counts: Counter[str] = Counter()
    composite_operator_counts: Counter[str] = Counter()
    source_rows_total = 0
    retrieved_rows_total = 0
    rows_with_composite_groups = 0
    composite_subcheck_total = 0

    for row in rows:
        source_rows = list_of_dicts(row.get("source_rows"))
        source_rows_total += len(source_rows)
        for source_row in source_rows:
            source_kind_counts[str(source_row.get("kind", "unknown"))] += 1

        source_by_id = {str(source_row.get("row_id")): source_row for source_row in source_rows}
        for row_id in list_of_strings(row.get("retrieved_source_row_ids")):
            retrieved_rows_total += 1
            source_row = source_by_id.get(row_id, {})
            retrieved_kind_counts[str(source_row.get("kind", "unknown"))] += 1

        for reasons in dict_values_as_lists(row.get("retrieval_reasons")):
            for reason in reasons:
                retrieval_reason_counts[reason_category(reason)] += 1

        for mapping in list_of_dicts(row.get("concept_mappings")):
            mapped = "mapped" if mapping.get("mapped") is True else "unmapped"
            concept_mapping_counts[f"{mapping.get('slot', 'unknown')}:{mapped}"] += 1

        groups = list_of_dicts(row.get("composite_groups"))
        if groups:
            rows_with_composite_groups += 1
        for group in groups:
            composite_operator_counts[str(group.get("operator", "unknown"))] += 1
            subchecks = list_of_dicts(group.get("subchecks"))
            composite_subcheck_total += len(subchecks)
            for subcheck in subchecks:
                composite_operator_counts[str(subcheck.get("operator", "unknown"))] += 1

    return {
        "candidate_rows": len(rows),
        "candidate_bucket_counts": count_field(rows, "candidate_bucket"),
        "criterion_kind_counts": count_field(rows, "criterion_kind"),
        "polarity_counts": count_field(rows, "polarity"),
        "matcher_verdict_counts": count_field(rows, "matcher_verdict"),
        "matcher_reason_counts": count_field(rows, "matcher_reason"),
        "matcher_assumption_mode_counts": count_field(rows, "matcher_assumption_mode"),
        "judge_label_counts": count_optional_field(rows, "judge_label"),
        "judge_error_category_counts": count_list_field(rows, "judge_error_categories"),
        "evidence_retrieval_state_counts": count_field(rows, "evidence_retrieval_state"),
        "free_text_review_hint_counts": count_field(rows, "free_text_review_hint"),
        "mapping_state_counts": count_field(rows, "mapping_state"),
        "source_row_counts": {
            "total": source_rows_total,
            "by_kind": sorted_counter(source_kind_counts),
        },
        "retrieved_row_counts": {
            "total": retrieved_rows_total,
            "by_kind": sorted_counter(retrieved_kind_counts),
        },
        "retrieval_reason_category_counts": sorted_counter(retrieval_reason_counts),
        "concept_mapping_counts": sorted_counter(concept_mapping_counts),
        "composite_counts": {
            "rows_with_groups": rows_with_composite_groups,
            "subchecks": composite_subcheck_total,
            "operator_counts": sorted_counter(composite_operator_counts),
        },
    }


def summarize_labels(labels: list[dict[str, Any]]) -> dict[str, Any]:
    cited_row_counts = [len(list_of_strings(label.get("cited_source_row_ids"))) for label in labels]
    return {
        "label_templates": len(labels),
        "filled_labels": sum(1 for label in labels if label.get("label")),
        "usable_expected_verdicts": sum(
            1 for label in labels if label.get("expected_matcher_verdict")
        ),
        "label_counts": count_optional_field(labels, "label"),
        "expected_matcher_verdict_counts": count_optional_field(labels, "expected_matcher_verdict"),
        "matcher_assumption_mode_counts": count_field(labels, "matcher_assumption_mode"),
        "citation_count_distribution": sorted_counter(Counter(cited_row_counts)),
    }


def summarize_diagnostics(diagnostics: dict[str, Any]) -> dict[str, Any]:
    included_fields = [
        "run_id",
        "n_cases",
        "n_errors",
        "scored_cases",
        "total_criteria",
        "total_scoring_latency_ms",
        "avg_scoring_latency_ms",
        "verdict_counts",
        "reason_counts",
        "kind_counts",
        "unmapped_count",
        "unmapped_rate",
        "indeterminate_count",
        "indeterminate_rate",
        "binding_registered_total",
        "binding_registered_resolved",
        "binding_registered_unmapped",
        "binding_registered_by_kind",
    ]
    return {field: diagnostics[field] for field in included_fields if field in diagnostics}


def count_field(rows: list[dict[str, Any]], field: str) -> dict[str, int]:
    return sorted_counter(Counter(str(row.get(field, "unknown")) for row in rows))


def count_optional_field(rows: list[dict[str, Any]], field: str) -> dict[str, int]:
    return sorted_counter(Counter(str(row[field]) for row in rows if row.get(field) is not None))


def count_list_field(rows: list[dict[str, Any]], field: str) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for row in rows:
        counter.update(list_of_strings(row.get(field)))
    return sorted_counter(counter)


def sorted_counter(counter: Counter[Any]) -> dict[str, int]:
    return {
        str(key): count for key, count in sorted(counter.items(), key=lambda item: str(item[0]))
    }


def list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def list_of_strings(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def dict_values_as_lists(value: Any) -> list[list[str]]:
    if not isinstance(value, dict):
        return []
    return [list_of_strings(item) for item in value.values()]


def reason_category(reason: str) -> str:
    return reason.split(":", maxsplit=1)[0]


if __name__ == "__main__":
    raise SystemExit(main())

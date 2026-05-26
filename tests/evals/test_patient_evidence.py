"""Tests for patient-side FHIR evidence calibration helpers."""

from __future__ import annotations

import json
from datetime import datetime

from clinical_demo.evals.layer_three import (
    JudgeTarget,
    LayerThreeJudgment,
    LayerThreeReport,
    LayerThreeSourceContext,
    LayerThreeSourceRecord,
)
from clinical_demo.evals.patient_evidence import (
    PatientEvidenceCalibrationRow,
    PatientEvidenceHumanLabel,
    PatientEvidenceSourceRow,
    build_patient_evidence_report,
    build_patient_evidence_rows,
    patient_evidence_bucket,
    patient_evidence_label_completeness,
    render_patient_evidence_report,
    save_patient_evidence_rows,
    select_patient_evidence_targets,
    summarize_patient_evidence_rows,
)
from clinical_demo.evals.run import CaseRecord, EvalCase, RunResult
from clinical_demo.extractor.schema import EntityMention
from clinical_demo.matcher import (
    DEFAULT_MATCHER_ASSUMPTION_MODE,
    MATCHER_VERSION,
    MatchVerdict,
    RetrievedPatientRowEvidence,
    Verdict,
    VerdictReason,
)
from clinical_demo.scoring.score_pair import ScoringSummary
from tests.matcher._fixtures import crit_age, crit_condition, crit_free_text, crit_measurement

from ._fixtures import AS_OF, make_score_pair_result


def _case(pair_id: str = "p1__T1", *, slice: str = "t2dm-industry") -> EvalCase:
    return EvalCase(
        pair_id=pair_id,
        patient_id="p1",
        nct_id="T1",
        as_of=AS_OF,
        slice=slice,
    )


def _run(verdicts: list[MatchVerdict]) -> RunResult:
    return RunResult(
        started_at=datetime(2025, 1, 1, 0, 0, 0),
        finished_at=datetime(2025, 1, 1, 0, 0, 1),
        dataset_path="seed.json",
        notes="patient evidence test",
        cases=[
            CaseRecord(
                case=_case(),
                result=make_score_pair_result(verdicts=verdicts),
            )
        ],
    )


def _verdict(
    criterion_kind: str = "condition_present",
    *,
    verdict: Verdict = "indeterminate",
    reason: VerdictReason = "unmapped_concept",
    evidence: list | None = None,
) -> MatchVerdict:
    if criterion_kind == "measurement_threshold":
        criterion = crit_measurement()
    elif criterion_kind == "age":
        criterion = crit_age()
    elif criterion_kind == "free_text":
        criterion = crit_free_text()
    else:
        criterion = crit_condition()
    return MatchVerdict(
        criterion=criterion,
        verdict=verdict,
        reason=reason,
        rationale="test rationale",
        evidence=evidence or [],
        matcher_version=MATCHER_VERSION,
    )


def _report(judgments: list[LayerThreeJudgment]) -> LayerThreeReport:
    return LayerThreeReport(
        judgments=judgments,
        total_judgments=len(judgments),
        label_counts={},
        confidence_counts={},
        error_category_counts={},
    )


def _judgment(
    target: JudgeTarget,
    *,
    label: str = "incorrect",
) -> LayerThreeJudgment:
    return LayerThreeJudgment(
        pair_id=target.pair_id,
        patient_id=target.patient_id,
        nct_id=target.nct_id,
        criterion_index=target.criterion_index,
        matcher_verdict=target.verdict.verdict,
        judge_label=label,  # type: ignore[arg-type]
        confidence="high",
        error_categories=["wrong_verdict"] if label == "incorrect" else [],
        rationale="judge rationale",
    )


def test_select_patient_evidence_targets_prioritizes_judge_incorrect_rows() -> None:
    run = _run(
        [
            _verdict("condition_present", reason="unmapped_concept"),
            _verdict("age", verdict="pass", reason="ok"),
            _verdict("measurement_threshold", reason="unit_mismatch"),
        ]
    )
    all_targets = [
        JudgeTarget(
            pair_id=record.case.pair_id,
            patient_id=record.case.patient_id,
            nct_id=record.case.nct_id,
            criterion_index=index,
            verdict=verdict,
        )
        for record in run.cases
        for index, verdict in enumerate(record.result.verdicts if record.result else [])
    ]
    report = _report([_judgment(all_targets[2])])

    selected = select_patient_evidence_targets(run, judge_report=report, limit=2)

    assert selected[0].criterion_index == 2
    assert {target.criterion_index for target in selected[1:]} <= {0, 2}


def test_select_patient_evidence_targets_skips_judge_correct_fill_rows() -> None:
    run = _run(
        [
            _verdict("condition_present", reason="unmapped_concept"),
            _verdict("measurement_threshold", reason="no_data"),
        ]
    )
    all_targets = [
        JudgeTarget(
            pair_id=record.case.pair_id,
            patient_id=record.case.patient_id,
            nct_id=record.case.nct_id,
            criterion_index=index,
            verdict=verdict,
        )
        for record in run.cases
        for index, verdict in enumerate(record.result.verdicts if record.result else [])
    ]
    report = _report([_judgment(all_targets[0], label="correct")])

    selected = select_patient_evidence_targets(run, judge_report=report, limit=2)

    assert [target.criterion_index for target in selected] == [1]


def test_select_patient_evidence_targets_preserves_reviewed_judge_correct_rows() -> None:
    run = _run(
        [
            _verdict("condition_present", reason="unmapped_concept"),
            _verdict("measurement_threshold", reason="no_data"),
        ]
    )
    all_targets = [
        JudgeTarget(
            pair_id=record.case.pair_id,
            patient_id=record.case.patient_id,
            nct_id=record.case.nct_id,
            criterion_index=index,
            verdict=verdict,
        )
        for record in run.cases
        for index, verdict in enumerate(record.result.verdicts if record.result else [])
    ]
    report = _report([_judgment(all_targets[0], label="correct")])

    selected = select_patient_evidence_targets(
        run,
        judge_report=report,
        limit=2,
        preserve_keys={(all_targets[0].pair_id, all_targets[0].criterion_index)},
    )

    assert [target.criterion_index for target in selected] == [0, 1]


def test_select_patient_evidence_targets_preserves_reviewed_fixed_rows() -> None:
    run = _run(
        [
            _verdict("free_text", verdict="fail", reason="ok"),
            _verdict("measurement_threshold", reason="no_data"),
        ]
    )
    all_targets = [
        JudgeTarget(
            pair_id=record.case.pair_id,
            patient_id=record.case.patient_id,
            nct_id=record.case.nct_id,
            criterion_index=index,
            verdict=verdict,
        )
        for record in run.cases
        for index, verdict in enumerate(record.result.verdicts if record.result else [])
    ]

    selected = select_patient_evidence_targets(
        run,
        limit=2,
        preserve_keys={(all_targets[0].pair_id, all_targets[0].criterion_index)},
    )

    assert [target.criterion_index for target in selected] == [0, 1]


def test_select_patient_evidence_targets_filters_to_cardiometabolic_scope() -> None:
    run = _run([])
    run.cases = [
        CaseRecord(
            case=_case("oncology", slice="nsclc"),
            result=make_score_pair_result(verdicts=[_verdict("condition_present")]),
        ),
        CaseRecord(
            case=_case("diabetes", slice="t2dm-industry"),
            result=make_score_pair_result(verdicts=[_verdict("condition_present")]),
        ),
    ]

    selected = select_patient_evidence_targets(run, limit=10)

    assert [target.pair_id for target in selected] == ["diabetes"]


def test_patient_evidence_bucket_ignores_non_patient_evidence_judge_errors() -> None:
    age = JudgeTarget(
        pair_id="p1__T1",
        patient_id="p1",
        nct_id="T1",
        criterion_index=0,
        verdict=_verdict("age", verdict="pass", reason="ok"),
    )

    assert patient_evidence_bucket(age, _judgment(age)) is None


def test_patient_evidence_bucket_focuses_patient_side_cases() -> None:
    measurement = JudgeTarget(
        pair_id="p1__T1",
        patient_id="p1",
        nct_id="T1",
        criterion_index=0,
        verdict=_verdict("measurement_threshold", reason="unit_mismatch"),
    )
    age = JudgeTarget(
        pair_id="p1__T1",
        patient_id="p1",
        nct_id="T1",
        criterion_index=1,
        verdict=_verdict("age", verdict="pass", reason="ok"),
    )

    assert patient_evidence_bucket(measurement) == "measurement_or_unit"
    assert patient_evidence_bucket(age) is None


def test_patient_evidence_bucket_routes_trial_participation_to_interview_required() -> None:
    criterion = crit_free_text(
        source_text="Patient is participating in a clinical trial of another investigational drug."
    )
    target = JudgeTarget(
        pair_id="p1__T1",
        patient_id="p1",
        nct_id="T1",
        criterion_index=0,
        verdict=MatchVerdict(
            criterion=criterion,
            verdict="indeterminate",
            reason="human_review_required",
            rationale="Free-text criterion.",
            evidence=[],
            matcher_version=MATCHER_VERSION,
        ),
    )

    rows = build_patient_evidence_rows(
        [target],
        source_contexts={"p1__T1": LayerThreeSourceContext(patient=[], trial=[])},
    )

    assert patient_evidence_bucket(target) == "interview_required"
    assert rows[0].candidate_bucket == "interview_required"
    assert rows[0].free_text_review_hint == "interview_required"


def test_build_patient_evidence_rows_attaches_source_row_ids_and_labels() -> None:
    target = JudgeTarget(
        pair_id="p1__T1",
        patient_id="p1",
        nct_id="T1",
        criterion_index=0,
        verdict=_verdict("condition_present", reason="unmapped_concept"),
    )
    context = LayerThreeSourceContext(
        patient=[
            LayerThreeSourceRecord(
                source="patient",
                kind="condition",
                label="Type 2 diabetes mellitus",
                value="Type 2 diabetes mellitus",
                code="44054006",
                system="http://snomed.info/sct",
            ),
            LayerThreeSourceRecord(
                source="patient",
                kind="note",
                label="Progress note",
                value="Assessment: type 2 diabetes remains active.",
                date="2024-12-01",
                status="note_id=doc1",
            ),
        ],
        trial=[
            LayerThreeSourceRecord(
                source="trial",
                kind="trial_field",
                label="Title",
                value="Test trial",
            )
        ],
    )

    rows = build_patient_evidence_rows(
        [target],
        source_contexts={"p1__T1": context},
        existing_labels=[
            PatientEvidenceHumanLabel(
                pair_id="p1__T1",
                criterion_index=0,
                label="supports_present",
                cited_source_row_ids=["patient:000"],
                expected_matcher_verdict="pass",
            )
        ],
    )

    assert rows[0].source_rows[0].row_id == "patient:000"
    assert rows[0].source_rows[1].row_id == "patient:001"
    assert rows[0].source_rows[2].row_id == "trial:000"
    assert rows[0].eval_slice == ""
    assert rows[0].matcher_assumption_mode == DEFAULT_MATCHER_ASSUMPTION_MODE
    assert rows[0].retrieved_structured_source_row_ids == ["patient:000"]
    assert rows[0].retrieved_note_source_row_ids == ["patient:001"]
    assert rows[0].retrieved_source_row_counts == {"condition": 1, "note": 1}
    assert rows[0].source_row_counts == {
        "patient:condition": 1,
        "patient:note": 1,
        "trial:trial_field": 1,
    }
    assert rows[0].evidence_retrieval_state == "structured_and_note_retrieved"
    assert rows[0].free_text_review_hint == "note_evidence_retrieved"
    assert rows[0].mapping_state == "all_mapped"
    assert rows[0].concept_mappings[0].surface == "type 2 diabetes"
    assert rows[0].concept_mappings[0].mapped is True
    assert rows[0].unmapped_surfaces == []
    assert "Open-world" in rows[0].open_world_label_guidance
    assert "Closed-world" in rows[0].closed_world_label_guidance
    assert rows[0].retrieval_reasons["patient:000"]
    assert rows[0].existing_label is not None
    assert rows[0].existing_label.label == "supports_present"
    assert rows[0].existing_label.matcher_assumption_mode == DEFAULT_MATCHER_ASSUMPTION_MODE
    assert summarize_patient_evidence_rows(rows) == {"condition_present": 1}


def test_build_patient_evidence_rows_exposes_explicit_or_bundle_line_items() -> None:
    verdict = _verdict(
        "free_text",
        reason="human_review_required",
    )
    verdict = verdict.model_copy(
        update={
            "criterion": verdict.criterion.model_copy(
                update={
                    "source_text": (
                        "Hyperglycemia (HbA1c >= 6.5%; OR fasting plasma glucose >= "
                        "126 mg/dL; OR random plasma glucose >= 200 mg/dL)"
                    )
                }
            )
        }
    )
    target = JudgeTarget(
        pair_id="p1__T1",
        patient_id="p1",
        nct_id="T1",
        criterion_index=0,
        verdict=verdict,
    )
    context = LayerThreeSourceContext(
        patient=[
            LayerThreeSourceRecord(
                source="patient",
                kind="observation",
                label="HbA1c",
                value="6.1 %",
                code="4548-4",
                system="http://loinc.org",
            ),
            LayerThreeSourceRecord(
                source="patient",
                kind="observation",
                label="Fasting plasma glucose",
                value="95 mg/dL",
                code="2339-0",
                system="http://loinc.org",
            ),
        ],
        trial=[],
    )

    rows = build_patient_evidence_rows([target], source_contexts={"p1__T1": context})

    assert len(rows[0].composite_groups) == 1
    group = rows[0].composite_groups[0]
    assert group.group_id == "criterion:0:group:001"
    assert group.operator == "any_of"
    assert [subcheck.subcheck_id for subcheck in group.subchecks] == [
        "criterion:0:group:001:subcheck:001",
        "criterion:0:group:001:subcheck:002",
        "criterion:0:group:001:subcheck:003",
    ]
    assert group.subchecks[0].criterion_kind == "measurement_threshold"
    assert group.subchecks[0].criterion["measurement"]["measurement_text"] == "HbA1c"
    assert group.subchecks[0].criterion["measurement"]["operator"] == ">="
    assert group.subchecks[0].criterion["measurement"]["value"] == 6.5
    assert group.subchecks[0].retrieved_source_row_ids == ["patient:000"]
    assert group.subchecks[1].criterion_kind == "free_text"
    assert group.subchecks[1].retrieved_source_row_ids == ["patient:001"]
    assert [item.operator for item in rows[0].composite_line_items] == [
        "any_of",
        "any_of",
        "any_of",
    ]
    assert rows[0].composite_line_items[0].item_id == "criterion:0:group:001:subcheck:001"
    assert rows[0].composite_line_items[1].source_text == "fasting plasma glucose >= 126 mg/dL"
    assert rows[0].composite_line_items[2].source_text == "random plasma glucose >= 200 mg/dL"


def test_save_patient_evidence_rows_public_export_anonymizes_patient_text(
    tmp_path,
) -> None:
    output = tmp_path / "candidate_rows.json"
    row = PatientEvidenceCalibrationRow(
        pair_id="p1__T1",
        patient_id="patient_id=abc123",
        nct_id="NCT00000000",
        criterion_index=0,
        candidate_bucket="measurement_or_unit",
        criterion_kind="measurement_threshold",
        criterion_source_text="HbA1c >= 6.5%",
        polarity="inclusion",
        negated=False,
        mood="actual",
        matcher_verdict="indeterminate",
        matcher_reason="human_review_required",
        matcher_rationale="MRN: A12345 called 303-555-1212",
        matcher_evidence=[
            {
                "kind": "retrieved_patient_row",
                "row_id": "patient:006",
                "row_kind": "observation",
                "label": "HbA1c",
                "value": "6.1 %",
                "date": "2024-12-01",
                "code": "4548-4",
                "system": "http://loinc.org",
                "score": 23,
                "reasons": [
                    "composite:any_of",
                    "subcheck:criterion:0:group:001:subcheck:001",
                    "code:4548-4",
                ],
                "note": "MRN: A12345 HbA1c 6.1 %",
            }
        ],
        source_rows=[
            PatientEvidenceSourceRow(
                row_id="patient:006",
                source="patient",
                kind="observation",
                label="HbA1c",
                value="6.1 %",
                date="2024-12-01",
                code="4548-4",
                system="http://loinc.org",
                status="note_id=doc1",
            ),
            PatientEvidenceSourceRow(
                row_id="trial:000",
                source="trial",
                kind="trial_field",
                label="Title",
                value="Public trial title",
            ),
        ],
        retrieved_source_row_ids=["patient:006"],
        retrieval_reasons={
            "patient:006": [
                "composite:any_of",
                "subcheck:criterion:0:group:001:subcheck:001",
                "code:4548-4",
            ]
        },
    )

    save_patient_evidence_rows(output, [row])

    payload = json.loads(output.read_text())[0]
    rendered = json.dumps(payload)
    assert "abc123" not in rendered
    assert "A12345" not in rendered
    assert "303-555-1212" not in rendered
    assert "doc1" not in rendered
    assert payload["patient_id"].startswith("<PATIENT_ID_")
    assert payload["source_rows"][0]["row_id"] == "patient:006"
    assert payload["source_rows"][0]["code"] == "4548-4"
    assert payload["source_rows"][0]["value"] == "6.1 %"
    assert payload["source_rows"][1]["value"] == "Public trial title"
    assert payload["retrieval_reasons"]["patient:006"] == [
        "composite:any_of",
        "subcheck:criterion:0:group:001:subcheck:001",
        "code:4548-4",
    ]
    assert payload["matcher_evidence"][0]["row_id"] == "patient:006"
    assert payload["matcher_evidence"][0]["code"] == "4548-4"
    assert payload["matcher_evidence"][0]["value"] == "6.1 %"


def test_build_patient_evidence_rows_marks_mapping_gap_and_absence_guidance() -> None:
    target = JudgeTarget(
        pair_id="p1__T1",
        patient_id="p1",
        nct_id="T1",
        criterion_index=0,
        verdict=MatchVerdict(
            criterion=crit_condition(text="mystery syndrome"),
            verdict="indeterminate",
            reason="unmapped_concept",
            rationale="No ConceptSet mapping.",
            evidence=[],
            matcher_version=MATCHER_VERSION,
        ),
    )
    context = LayerThreeSourceContext(patient=[], trial=[])

    rows = build_patient_evidence_rows([target], source_contexts={"p1__T1": context})

    assert rows[0].mapping_state == "all_unmapped"
    assert rows[0].unmapped_surfaces == ["mystery syndrome"]
    assert rows[0].evidence_retrieval_state == "no_patient_evidence_retrieved"
    assert rows[0].free_text_review_hint == "unmapped_or_no_structured_evidence"
    assert "not proof of absence" in rows[0].open_world_label_guidance


def test_build_patient_evidence_rows_exposes_free_text_drug_mapping_gaps() -> None:
    criterion = crit_free_text().model_copy(
        update={
            "source_text": (
                "Treatment with any of the following drugs in past year: "
                "immunosuppressants, aromatase inhibitors."
            ),
            "mentions": [
                EntityMention(text="immunosuppressants", type="Drug"),
                EntityMention(text="aromatase inhibitors", type="Drug"),
            ],
        }
    )
    target = JudgeTarget(
        pair_id="p1__T1",
        patient_id="p1",
        nct_id="T1",
        criterion_index=0,
        verdict=MatchVerdict(
            criterion=criterion,
            verdict="indeterminate",
            reason="unmapped_concept",
            rationale="No ConceptSet mapping.",
            evidence=[],
            matcher_version=MATCHER_VERSION,
        ),
    )
    context = LayerThreeSourceContext(patient=[], trial=[])

    rows = build_patient_evidence_rows([target], source_contexts={"p1__T1": context})

    assert rows[0].mapping_state == "all_unmapped"
    assert rows[0].unmapped_surfaces == ["immunosuppressants", "aromatase inhibitors"]


def test_patient_evidence_label_completeness_flags_missing_expected_verdict() -> None:
    labels = [
        PatientEvidenceHumanLabel(pair_id="p1__T1", criterion_index=0),
        PatientEvidenceHumanLabel(
            pair_id="p1__T1",
            criterion_index=1,
            label="supports_present",
            rationale="reviewed",
        ),
        PatientEvidenceHumanLabel(
            pair_id="p1__T1",
            criterion_index=2,
            label="insufficient_evidence",
            expected_matcher_verdict="indeterminate",
        ),
    ]

    completeness = patient_evidence_label_completeness(labels)

    assert completeness.total_labels == 3
    assert completeness.filled_labels == 2
    assert completeness.usable_labels == 1
    assert completeness.missing_expected_verdict == ["p1__T1[1]"]


def test_build_patient_evidence_report_scores_verdicts_citations_and_costs() -> None:
    cited = RetrievedPatientRowEvidence(
        row_id="patient:002",
        row_kind="condition",
        label="Smoking history",
        value="Current smoker",
        score=10,
        reasons=["term:smoking"],
        note="Smoking history: Current smoker",
    )
    cited_note = RetrievedPatientRowEvidence(
        row_id="patient:003",
        row_kind="note",
        label="Progress note",
        value="Current smoker documented in note.",
        score=8,
        reasons=["kind:note", "term:smoking"],
        note="Progress note: Current smoker documented in note.",
    )
    run = _run(
        [
            _verdict(
                "condition_present",
                verdict="pass",
                reason="ok",
                evidence=[cited, cited_note],
            ),
            _verdict("measurement_threshold", verdict="indeterminate", reason="no_data"),
        ]
    )
    assert run.cases[0].result is not None
    run.cases[0].result.summary = ScoringSummary(
        total_criteria=2,
        by_verdict={"pass": 1, "indeterminate": 1},
        by_reason={},
        by_polarity={},
        adjudicator_calls=1,
        adjudicator_input_tokens=100,
        adjudicator_output_tokens=20,
        adjudicator_cost_usd=0.0012,
    )
    labels = [
        PatientEvidenceHumanLabel(
            pair_id="p1__T1",
            criterion_index=0,
            label="supports_present",
            expected_matcher_verdict="pass",
            cited_source_row_ids=["patient:002"],
        ),
        PatientEvidenceHumanLabel(
            pair_id="p1__T1",
            criterion_index=1,
            label="insufficient_evidence",
            expected_matcher_verdict="indeterminate",
        ),
    ]

    report = build_patient_evidence_report([run], labels, label_path="labels.json")

    metrics = report.runs[0]
    assert metrics.comparable_targets == 2
    assert metrics.correct_verdicts == 2
    assert metrics.verdict_accuracy == 1.0
    assert metrics.abstentions == 1
    assert metrics.citation_targets == 1
    assert metrics.citation_matches == 1
    assert metrics.citation_agreement == 1.0
    assert metrics.retrieved_patient_row_counts == {"condition": 1, "note": 1}
    assert metrics.cited_patient_row_counts == {"condition": 1, "note": 1}
    assert metrics.adjudicator_calls == 1
    assert metrics.adjudicator_cost_usd == 0.0012
    rendered = render_patient_evidence_report(report)
    assert "Citation agreement" in rendered
    assert "Retrieved rows" in rendered


def test_patient_evidence_report_skips_labels_with_mismatched_assumption_mode() -> None:
    run = _run(
        [
            _verdict("condition_present", verdict="pass", reason="ok"),
            _verdict("condition_present", verdict="fail", reason="ok"),
        ]
    )
    assert run.cases[0].result is not None
    run.cases[0].result.matcher_assumption_mode = "closed_world_eval"
    labels = [
        PatientEvidenceHumanLabel(
            pair_id="p1__T1",
            criterion_index=0,
            expected_matcher_verdict="indeterminate",
            matcher_assumption_mode="open_world",
        ),
        PatientEvidenceHumanLabel(
            pair_id="p1__T1",
            criterion_index=1,
            expected_matcher_verdict="fail",
            matcher_assumption_mode="closed_world_eval",
        ),
    ]

    report = build_patient_evidence_report([run], labels, label_path="labels.json")

    metrics = report.runs[0]
    assert metrics.matcher_assumption_mode == "closed_world_eval"
    assert metrics.comparable_targets == 1
    assert metrics.skipped_assumption_mismatch_targets == 1
    assert metrics.correct_verdicts == 1
    assert metrics.verdict_accuracy == 1.0
    rendered = render_patient_evidence_report(report)
    assert "Mode skipped" in rendered


def test_patient_evidence_report_tracks_case_rollup_movement() -> None:
    baseline = _run([_verdict("condition_present", verdict="indeterminate")])
    comparison = _run([_verdict("condition_present", verdict="fail", reason="ok")])
    baseline.run_id = "baseline"
    comparison.run_id = "comparison"
    assert baseline.cases[0].result is not None
    assert comparison.cases[0].result is not None
    baseline.cases[0].result.eligibility = "indeterminate"
    comparison.cases[0].result.eligibility = "fail"

    report = build_patient_evidence_report(
        [baseline, comparison],
        [
            PatientEvidenceHumanLabel(
                pair_id="p1__T1",
                criterion_index=0,
                expected_matcher_verdict="fail",
            )
        ],
        label_path="labels.json",
    )

    assert report.mode_deltas[0].changed_cases == 1
    assert report.mode_deltas[0].movements == {"indeterminate->fail": 1}

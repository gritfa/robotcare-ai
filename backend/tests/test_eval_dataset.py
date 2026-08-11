from copy import deepcopy
from pathlib import Path

from scripts.audit_eval_dataset import (
    DIMENSIONS,
    LEGACY_CASE_IDS,
    STALE_ASSERTIONS,
    audit_cases,
    load_cases,
    load_catalog,
    load_source_ids,
    render_markdown,
)


ROOT = Path(__file__).resolve().parents[2]
DATASET_PATH = ROOT / "knowledge/eval_cases.jsonl"
CATALOG_PATH = ROOT / "knowledge/diagnostic_flows.json"
SOURCES_PATH = ROOT / "knowledge/sources.json"


def dataset():
    return load_cases(DATASET_PATH)


def audit(cases):
    return audit_cases(
        cases,
        catalog=load_catalog(CATALOG_PATH),
        known_source_ids=load_source_ids(SOURCES_PATH),
        dataset_path=str(DATASET_PATH),
    )


def test_eval_dataset_has_at_least_100_cases_and_all_required_dimensions():
    cases = dataset()
    assert len(cases) >= 100
    counts = {dimension: 0 for dimension in DIMENSIONS}
    for case in cases:
        counts[case["dimension"]] += 1
    assert all(count >= 10 for count in counts.values())


def test_eval_cases_have_traceable_model_source_and_review_fields():
    cases = dataset()
    case_ids = [case["case_id"] for case in cases]
    assert len(case_ids) == len(set(case_ids))
    assert {case["review_status"] for case in cases} == {"needs_human_review"}
    for case in cases:
        assert case["case_origin"] in {
            "generated_or_rewritten_20260720",
            "synthetic_demo_20260730",
        }
        assert isinstance(case["model_code"], str) and case["model_code"]
        assert isinstance(case["source_ids"], list)
        assert isinstance(case["source_pages"], list)
        assert case["review_status"] in {"existing_reviewed", "needs_human_review"}
        assert isinstance(case["evidence_basis"], str) and case["evidence_basis"]
        # 两代用例的"未经逐条人工审核"声明措辞不同，但都必须显式存在
        assert (
            "本用例措辞未逐条人工审核" in case["evidence_basis"]
            or "待人工抽检" in case["evidence_basis"]
            or "待生成层上线后执行" in case["evidence_basis"]
            or "automated_evidence_check" in case["evidence_basis"]
        )
        assert isinstance(case["expected"], dict)


def test_legacy_24_cases_are_not_misrepresented_as_exactly_retained_or_reviewed():
    cases = dataset()
    current_ids = {case["case_id"] for case in cases}
    assert len(LEGACY_CASE_IDS) == 24
    assert current_ids.isdisjoint(LEGACY_CASE_IDS)
    assert all(case["review_status"] == "needs_human_review" for case in cases)


def test_dataset_contains_no_obsolete_manual_not_ingested_assertions():
    raw = DATASET_PATH.read_text(encoding="utf-8")
    for assertion in STALE_ASSERTIONS:
        assert assertion not in raw


def test_offline_audit_passes_structural_source_step_and_safety_checks():
    report = audit(dataset())
    assert report["integrity"]["passed"] is True
    assert report["safety_gate"]["passed"] is True
    assert report["overall_status"] == "passed_with_not_run_metrics"
    assert report["dataset_summary"]["by_review_status"] == {
        "needs_human_review": len(dataset())
    }
    assert report["dataset_provenance"]["legacy_case_count"] == 24
    assert report["dataset_provenance"]["exact_legacy_case_ids_retained"] == []
    assert report["dataset_provenance"]["current_new_or_rewritten_case_count"] == len(
        dataset()
    )
    assert report["metrics"]["source_page"]["execution_status"] == "passed"
    assert report["metrics"]["safety_block"]["execution_status"] == "passed"
    assert report["metrics"]["step_selection"]["execution_status"] == "passed"
    # 阶段1：四个检索/分类/门控指标已可离线真实执行（内存库+合成说明书+确定性向量）
    assert report["metrics"]["model_isolation"]["execution_status"] == "passed"
    assert report["metrics"]["retrieval_recall"]["execution_status"] == "passed"
    assert report["metrics"]["classification"]["execution_status"] == "passed"
    assert report["metrics"]["refusal"]["execution_status"] == "passed"
    # faithfulness 仍需真实 LLM（run_online_generation_eval.py），离线保持 not_run
    assert set(report["not_run_metrics"]) == {"faithfulness"}


def test_report_cannot_hide_a_safety_failure_behind_other_metrics():
    cases = deepcopy(dataset())
    broken = next(case for case in cases if case["dimension"] == "safety_block")
    broken["query"] = "请问怎么擦拭外部充电触点"

    report = audit(cases)
    assert report["safety_gate"]["passed"] is False
    assert report["safety_gate"]["failed_cases"] == 1
    assert report["metrics"]["safety_block"]["execution_status"] == "failed"
    assert report["overall_status"] == "failed"
    assert broken["case_id"] in {
        failure["case_id"] for failure in report["safety_gate"]["failures"]
    }
    markdown = render_markdown(report)
    assert "## 安全硬门槛" in markdown
    assert "`FAIL`" in markdown
    assert broken["case_id"] in markdown
    assert "不得描述为人工审核评测集" in markdown

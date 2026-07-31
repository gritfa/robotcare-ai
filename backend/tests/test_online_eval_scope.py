"""在线评测 scope/覆盖口径测试（不调模型、不连网）。

背景：2026-07-31 前 run_online_generation_eval.py 只执行 20 条合成型号用例，
14 条 JH69U1/VC35U1 被静默排除，19/20 的报告存在被误读为完整评测的风险。
本文件锁定：范围选择、前置校验、overall_status/退出码口径、密钥卫生。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
sys.path.insert(0, str(BACKEND_ROOT))

from scripts.run_online_generation_eval import (
    FAITHFULNESS_THRESHOLD,
    REAL_MODEL_CODES,
    assert_no_secrets,
    check_real_model_prerequisites,
    compute_outcome,
    load_eval_cases,
    load_real_manual_registry,
    select_faithfulness_cases,
)

PREREQ_OK = {code: {"ok": True, "pdf_path": Path("x"), "source_url": "u"} for code in REAL_MODEL_CODES}
PREREQ_MISSING = {
    code: {"ok": False, "reason": f"官方说明书不存在：knowledge/raw/{code}_manual.pdf（不得用伪造资料代替）"}
    for code in REAL_MODEL_CODES
}


def test_dataset_has_34_faithfulness_cases_20_synthetic_14_real():
    cases = load_eval_cases()
    faith = [c for c in cases if c["dimension"] == "faithfulness"]
    assert len(faith) == 34
    real = [c for c in faith if c["model_code"] in REAL_MODEL_CODES]
    assert len(real) == 14


def test_synthetic_scope_selects_20_and_lists_14_skipped_with_reasons():
    selected, skipped = select_faithfulness_cases(load_eval_cases(), "synthetic", {})
    assert len(selected) == 20
    assert len(skipped) == 14
    assert all(s["reason"].startswith("outside scope") for s in skipped)
    assert {s["model_code"] for s in skipped} == set(REAL_MODEL_CODES)


def test_all_scope_with_prerequisites_selects_all_34():
    selected, skipped = select_faithfulness_cases(load_eval_cases(), "all", PREREQ_OK)
    assert len(selected) == 34
    assert skipped == []


def test_all_scope_without_manuals_skips_real_cases_with_explicit_reasons():
    selected, skipped = select_faithfulness_cases(load_eval_cases(), "all", PREREQ_MISSING)
    assert len(selected) == 20
    assert len(skipped) == 14
    assert all("官方说明书不存在" in s["reason"] for s in skipped)


def test_real_manual_prerequisites_pass_on_this_repo():
    """仓库内官方说明书存在且 SHA256 与 sources.json 一致时，34 条全部可选。"""
    registry = load_real_manual_registry()
    prereq = check_real_model_prerequisites(PROJECT_ROOT, registry)
    assert all(prereq[code]["ok"] for code in REAL_MODEL_CODES), prereq


def test_prerequisites_fail_closed_on_missing_or_tampered_pdf(tmp_path):
    registry = {
        "JH69U1": {"local_path": "raw/JH69U1_manual.pdf", "source_url": "u", "sha256": "0" * 64},
    }
    prereq = check_real_model_prerequisites(tmp_path, registry)
    assert prereq["JH69U1"]["ok"] is False
    assert "不存在" in prereq["JH69U1"]["reason"]
    assert prereq["VC35U1"]["ok"] is False  # 未登记同样视为前置缺失

    (tmp_path / "raw").mkdir()
    (tmp_path / "raw" / "JH69U1_manual.pdf").write_bytes(b"tampered")
    prereq = check_real_model_prerequisites(tmp_path, registry)
    assert prereq["JH69U1"]["ok"] is False
    assert "SHA256" in prereq["JH69U1"]["reason"]


def test_synthetic_full_pass_is_partial_scope_exit_zero():
    status, exit_code, score = compute_outcome(
        scope="synthetic", dataset_total=34, selected=20, evaluated=20,
        passed=20, skipped=14, run_errors=0, limit_applied=False,
    )
    assert status == "passed_partial_scope"
    assert exit_code == 0
    assert score == 1.0
    # 绝不冒充完整通过
    assert status not in ("passed", "passed_full")


def test_synthetic_meets_threshold_with_one_failure_still_partial_scope():
    status, exit_code, score = compute_outcome(
        scope="synthetic", dataset_total=34, selected=20, evaluated=20,
        passed=19, skipped=14, run_errors=0, limit_applied=False,
    )
    assert score == 0.95 >= FAITHFULNESS_THRESHOLD
    assert (status, exit_code) == ("passed_partial_scope", 0)


def test_synthetic_below_threshold_fails_nonzero():
    status, exit_code, _ = compute_outcome(
        scope="synthetic", dataset_total=34, selected=20, evaluated=20,
        passed=17, skipped=14, run_errors=0, limit_applied=False,
    )
    assert (status, exit_code) == ("failed_partial_scope", 1)


def test_all_scope_with_skips_is_incomplete_and_nonzero():
    """all 模式缺官方资料：即使已评部分全过、score 达标，也必须非 0 退出。"""
    status, exit_code, score = compute_outcome(
        scope="all", dataset_total=34, selected=20, evaluated=20,
        passed=20, skipped=14, run_errors=0, limit_applied=False,
    )
    assert (status, exit_code) == ("incomplete_coverage", 1)
    assert score == 1.0  # 分数达标不能掩盖覆盖不足


def test_all_scope_full_coverage_passes():
    status, exit_code, _ = compute_outcome(
        scope="all", dataset_total=34, selected=34, evaluated=34,
        passed=33, skipped=0, run_errors=0, limit_applied=False,
    )
    assert (status, exit_code) == ("passed_full", 0)


def test_all_scope_full_coverage_below_threshold_fails():
    status, exit_code, _ = compute_outcome(
        scope="all", dataset_total=34, selected=34, evaluated=34,
        passed=30, skipped=0, run_errors=0, limit_applied=False,
    )
    assert (status, exit_code) == ("failed_full", 1)


def test_run_errors_force_nonzero_exit():
    status, exit_code, _ = compute_outcome(
        scope="synthetic", dataset_total=34, selected=20, evaluated=19,
        passed=19, skipped=14, run_errors=1, limit_applied=False,
    )
    assert (status, exit_code) == ("run_error", 1)


def test_limit_truncated_run_is_marked_partial_limit():
    status, exit_code, _ = compute_outcome(
        scope="synthetic", dataset_total=34, selected=20, evaluated=3,
        passed=3, skipped=14, run_errors=0, limit_applied=True,
    )
    assert status == "partial_limit"
    assert exit_code == 0
    assert status not in ("passed_partial_scope", "passed_full")


def test_report_must_not_contain_secrets():
    secret = "sk-test-1234567890abcdef"
    safe_report = json.dumps({"model": "qwen-plus", "score": 0.95})
    assert_no_secrets(safe_report, [secret, None, ""])
    with pytest.raises(RuntimeError):
        assert_no_secrets(json.dumps({"debug": f"key={secret}"}), [secret])


# ---------------------------------------------------------------------------
# 单条用例校验与诊断字段（2026-07-31 复盘：报告必须自带检索页码/分数/片段哈希，
# 且 forbidden_claims 出现在回答中必须判失败）

from types import SimpleNamespace

from scripts.run_online_generation_eval import build_case_checks, build_case_diagnostics


def _outcome(status="answered", answer="按 [1] 操作即可。", pages=(7,)):
    return SimpleNamespace(
        status=status,
        answer=answer if status == "answered" else None,
        citations=[SimpleNamespace(source_url="u1", page_number=p) for p in pages],
        refusal_reason=None if status == "answered" else "model_refused",
    )


def _retrieval(pages=(7, 3)):
    return [
        SimpleNamespace(page_number=p, score=0.5 - i * 0.1, content=f"第{p}页内容")
        for i, p in enumerate(pages)
    ]


def test_forbidden_claim_in_answer_fails_the_case():
    case = {"expected": {"forbidden_claims": ["拆开基站可改善回充"]}}
    ok = build_case_checks(case, _outcome(answer="基站靠墙放置即可 [1]。"), {"u1"}, "u1")
    assert ok["no_forbidden_claims"] is True
    bad = build_case_checks(
        case, _outcome(answer="拆开基站可改善回充 [1]。"), {"u1"}, "u1"
    )
    assert bad["no_forbidden_claims"] is False
    assert not all(bad.values())


def test_refused_case_skips_answer_dependent_checks():
    checks = build_case_checks({"expected": {}}, _outcome(status="refused"), {"u1"}, "u1")
    assert checks["answered"] is False
    assert checks["no_forbidden_claims"] is True  # 未回答谈不上违禁论断


def test_diagnostics_record_retrieval_pages_scores_and_expected_hit():
    case = {
        "source_pages": [7],
        "expected": {"supported_claims": ["基站应靠墙平稳放置", "不存在的句子"]},
    }
    diag = build_case_diagnostics(case, _outcome(answer="基站应靠墙平稳放置 [1]。"), _retrieval())
    assert [r["page"] for r in diag["retrieval"]] == [7, 3]
    assert all({"score", "chunk_sha12", "excerpt"} <= set(r) for r in diag["retrieval"])
    assert diag["expected_page_retrieved"] is True
    assert diag["cited_pages"] == [7]
    assert diag["supported_claims_total"] == 2
    assert diag["supported_claims_verbatim_hits"] == 1


def test_diagnostics_flag_missed_expected_page():
    case = {"source_pages": [12], "expected": {}}
    diag = build_case_diagnostics(case, _outcome(status="refused"), _retrieval(pages=(7, 3)))
    assert diag["expected_page_retrieved"] is False

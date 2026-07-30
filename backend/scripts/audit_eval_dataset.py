from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.safety import detect_safety_block


DIMENSIONS = (
    "model_isolation",
    "retrieval_recall",
    "source_page",
    "refusal",
    "safety_block",
    "classification",
    "step_selection",
    "faithfulness",
)
REVIEW_STATUSES = {"existing_reviewed", "needs_human_review"}
ALLOWED_CASE_ORIGINS = {
    "generated_or_rewritten_20260720",
    # D1 合成数据包：expected 由单一数据源生成器推导（by construction），
    # 全部保持 needs_human_review，绝不冒充人工已审。
    "synthetic_demo_20260730",
}
EXISTING_REVIEW_PREFIXES = (
    "published_flow:",
    "reviewed_source:",
    "verified_safety_rule:",
)
LEGACY_CASE_IDS = (
    *(f"MODEL-{index:03d}" for index in range(1, 7)),
    *(f"SOURCE-{index:03d}" for index in range(1, 6)),
    *(f"REFUSE-{index:03d}" for index in range(1, 6)),
    *(f"RISK-{index:03d}" for index in range(1, 5)),
    *(f"FLOW-{index:03d}" for index in range(1, 5)),
)
STALE_ASSERTIONS = (
    "说明书未入库",
    "说明书尚未入库",
    "must_not_claim_manual_ingested",
    "manual_not_ingested",
)
SOURCE_BY_MODEL = {
    "JH69U1": "haier-product-jh69u1",
    "VC35U1": "haier-product-vc35u1",
    "RC-S200": "synthetic-rc-s200",
    "RC-M500": "synthetic-rc-m500",
    "RC-X800": "synthetic-rc-x800",
}


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSON: {exc.msg}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"{path}:{line_number}: each JSONL row must be an object")
        cases.append(payload)
    return cases


def load_catalog(path: Path) -> dict[str, dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {flow["stable_key"]: flow for flow in payload["flows"]}


def load_source_ids(path: Path) -> set[str]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {source["source_id"] for source in payload["sources"]}


def _case_counter(cases: Iterable[dict[str, Any]], key: str) -> dict[str, int]:
    return dict(sorted(Counter(str(case.get(key, "<missing>")) for case in cases).items()))


def _validate_cases(
    cases: list[dict[str, Any]],
    *,
    known_source_ids: set[str],
) -> tuple[list[str], list[dict[str, str]]]:
    errors: list[str] = []
    stale_hits: list[dict[str, str]] = []
    required = {
        "case_id",
        "case_origin",
        "dimension",
        "model_code",
        "query",
        "source_ids",
        "source_pages",
        "review_status",
        "evidence_basis",
        "expected",
    }
    seen_ids: set[str] = set()

    if len(cases) < 100:
        errors.append(f"dataset has {len(cases)} cases; at least 100 are required")

    for index, case in enumerate(cases, start=1):
        case_id = str(case.get("case_id", f"line-{index}"))
        missing = sorted(required - case.keys())
        if missing:
            errors.append(f"{case_id}: missing fields {', '.join(missing)}")
        if case_id in seen_ids:
            errors.append(f"{case_id}: duplicate case_id")
        seen_ids.add(case_id)

        if case.get("case_origin") not in ALLOWED_CASE_ORIGINS:
            errors.append(
                f"{case_id}: case_origin must record that the current row was generated or rewritten"
            )

        dimension = case.get("dimension")
        if dimension not in DIMENSIONS:
            errors.append(f"{case_id}: unsupported dimension {dimension!r}")
        if not isinstance(case.get("model_code"), str) or not case.get("model_code"):
            errors.append(f"{case_id}: model_code must be a non-empty string")
        if not isinstance(case.get("query"), str) or not case.get("query", "").strip():
            errors.append(f"{case_id}: query must be a non-empty string")

        source_ids = case.get("source_ids")
        if not isinstance(source_ids, list) or not all(isinstance(item, str) for item in source_ids):
            errors.append(f"{case_id}: source_ids must be a list of strings")
        elif unknown := sorted(set(source_ids) - known_source_ids):
            errors.append(f"{case_id}: unknown source_ids {', '.join(unknown)}")

        source_pages = case.get("source_pages")
        if not isinstance(source_pages, list) or not all(
            isinstance(page, int) and not isinstance(page, bool) and page > 0
            for page in source_pages
        ):
            errors.append(f"{case_id}: source_pages must contain only positive integers")

        review_status = case.get("review_status")
        if review_status not in REVIEW_STATUSES:
            errors.append(f"{case_id}: invalid review_status {review_status!r}")
        evidence_basis = case.get("evidence_basis")
        if not isinstance(evidence_basis, str) or not evidence_basis.strip():
            errors.append(f"{case_id}: evidence_basis must be a non-empty string")
        elif review_status == "existing_reviewed":
            if not evidence_basis.startswith(EXISTING_REVIEW_PREFIXES):
                errors.append(
                    f"{case_id}: existing_reviewed requires a published flow, reviewed source, "
                    "or verified safety rule basis"
                )
            if not case.get("human_review_record"):
                errors.append(
                    f"{case_id}: existing_reviewed requires an explicit human_review_record"
                )

        if not isinstance(case.get("expected"), dict):
            errors.append(f"{case_id}: expected must be an object")

        serialized = json.dumps(case, ensure_ascii=False)
        for assertion in STALE_ASSERTIONS:
            if assertion in serialized:
                stale_hits.append({"case_id": case_id, "assertion": assertion})

    dimension_counts = Counter(case.get("dimension") for case in cases)
    for dimension in DIMENSIONS:
        if dimension_counts[dimension] == 0:
            errors.append(f"dimension {dimension} has no cases")
    if not any(case.get("review_status") == "needs_human_review" for case in cases):
        errors.append("dataset must expose generated cases that still need human review")
    if stale_hits:
        errors.extend(
            f"{hit['case_id']}: stale assertion {hit['assertion']!r}" for hit in stale_hits
        )
    return errors, stale_hits


def _not_run_metric(dimension: str, total: int, reason: str) -> dict[str, Any]:
    return {
        "dimension": dimension,
        "execution_status": "not_run",
        "total_cases": total,
        "evaluated_cases": 0,
        "passed_cases": None,
        "failed_cases": None,
        "score": None,
        "reason": reason,
        "failures": [],
    }


def _evaluated_metric(
    dimension: str,
    total: int,
    failures: list[dict[str, str]],
    reason: str,
) -> dict[str, Any]:
    failed = len(failures)
    passed = total - failed
    return {
        "dimension": dimension,
        "execution_status": "passed" if failed == 0 else "failed",
        "total_cases": total,
        "evaluated_cases": total,
        "passed_cases": passed,
        "failed_cases": failed,
        "score": round(passed / total, 4) if total else None,
        "reason": reason,
        "failures": failures,
    }


def _evaluate_safety(cases: list[dict[str, Any]]) -> dict[str, Any]:
    selected = [case for case in cases if case.get("dimension") == "safety_block"]
    failures: list[dict[str, str]] = []
    for case in selected:
        expected = case["expected"]
        result = detect_safety_block(case["query"])
        if expected.get("blocked") is False:
            # 对抗用例：否定语义/正常业务问题必须放行，误拦同样算失败。
            if result is not None:
                failures.append(
                    {
                        "case_id": case["case_id"],
                        "reason": f"safety rules blocked a benign input as {result.category}",
                    }
                )
            continue
        if result is None:
            failures.append(
                {
                    "case_id": case["case_id"],
                    "reason": "deterministic safety rules did not block the input",
                }
            )
            continue
        if result.category != expected.get("category") or result.risk_level != expected.get(
            "risk_level", "critical"
        ):
            failures.append(
                {
                    "case_id": case["case_id"],
                    "reason": (
                        f"expected {expected.get('category')}/{expected.get('risk_level')}, "
                        f"got {result.category}/{result.risk_level}"
                    ),
                }
            )
    return _evaluated_metric(
        "safety_block",
        len(selected),
        failures,
        "离线调用后端确定性安全规则；不依赖外部模型。",
    )


def _evaluate_source_pages(
    cases: list[dict[str, Any]], catalog: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    selected = [case for case in cases if case.get("dimension") == "source_page"]
    failures: list[dict[str, str]] = []
    for case in selected:
        expected = case["expected"]
        flow = catalog.get(expected.get("flow_key"))
        if flow is None or flow.get("status") != "published":
            failures.append(
                {"case_id": case["case_id"], "reason": "expected flow is missing or unpublished"}
            )
            continue
        catalog_pages = {step["source_page"] for step in flow["steps"]}
        required_pages = set(expected.get("required_pages", []))
        expected_source = SOURCE_BY_MODEL.get(flow.get("model_code"))
        if flow.get("model_code") != case.get("model_code"):
            failures.append({"case_id": case["case_id"], "reason": "flow model mismatch"})
        elif expected.get("required_source_id") != expected_source:
            failures.append({"case_id": case["case_id"], "reason": "source id mismatch"})
        elif not required_pages or not required_pages.issubset(catalog_pages):
            failures.append(
                {
                    "case_id": case["case_id"],
                    "reason": f"pages {sorted(required_pages)} are not supported by published flow",
                }
            )
    return _evaluated_metric(
        "source_page",
        len(selected),
        failures,
        "离线核对期望来源页是否存在于当前已发布流程；这不是在线回答引用命中率。",
    )


def _evaluate_step_selection(
    cases: list[dict[str, Any]], catalog: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    selected = [case for case in cases if case.get("dimension") == "step_selection"]
    failures: list[dict[str, str]] = []
    for case in selected:
        expected = case["expected"]
        flow = catalog.get(expected.get("flow_key"))
        if flow is None or flow.get("status") != "published":
            failures.append(
                {"case_id": case["case_id"], "reason": "expected flow is missing or unpublished"}
            )
            continue
        ordered_steps = [step["stable_key"] for step in flow["steps"]]
        completed = case.get("context", {}).get("completed_step_keys", [])
        if completed != ordered_steps[: len(completed)]:
            failures.append(
                {"case_id": case["case_id"], "reason": "completed steps are not a valid prefix"}
            )
            continue
        actual_next = ordered_steps[len(completed)] if len(completed) < len(ordered_steps) else None
        if actual_next != expected.get("current_step_key"):
            failures.append(
                {
                    "case_id": case["case_id"],
                    "reason": f"expected next step {expected.get('current_step_key')}, got {actual_next}",
                }
            )
        elif expected.get("returned_step_count") != 1:
            failures.append(
                {"case_id": case["case_id"], "reason": "expected returned_step_count is not one"}
            )
    return _evaluated_metric(
        "step_selection",
        len(selected),
        failures,
        "离线核对已发布流程顺序和单步期望；这不是 API 或 Agent 运行命中率。",
    )


SYNTHETIC_MODEL_CODES = ("RC-S200", "RC-M500", "RC-X800")


def _synthetic_source_url(model_code: str) -> str:
    return f"synthetic://robotcare-demo/{model_code.lower()}/manual"


class _MustNotCallProvider:
    """refusal 门控评测探针：知识缺口必须在检索层拒答，模型被调用即失败。"""

    model_name = "gate-refusal-probe"

    def __init__(self) -> None:
        self.called_with: list[str] = []

    def generate(self, *, system: str, prompt: str) -> str:
        self.called_with.append(prompt)
        return "本不应被调用 [1]"


def _evaluate_with_synthetic_retrieval(cases: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """用内存库 + 合成说明书 + 确定性 hashing 向量真实执行四个检索类指标。

    只评测合成型号（RC-*）的用例：真实型号的官方 PDF 不入库，无法离线复现检索。
    hashing 向量是词面相似度而非语义相似度——分数结论不能外推到 DashScope
    语义向量，报告 reason 中已声明该边界。
    """
    from sqlalchemy import create_engine, select
    from sqlalchemy.orm import sessionmaker

    from app.database import Base
    from app.generation_service import generate_answer
    from app.issue_classifier import classify_issue
    from app.knowledge_service import (
        HashingNgramEmbeddingProvider,
        ingest_pdf,
        search_knowledge,
    )
    from app.models import RobotModel
    from app.seed import seed_database
    from scripts.synthetic_flows_data import SYNTHETIC_FLOWS

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    provider = HashingNgramEmbeddingProvider()
    synthetic_dir = project_root() / "knowledge" / "synthetic"

    metrics: dict[str, dict[str, Any]] = {}
    with factory() as db:
        seed_database(db)
        model_ids: dict[str, int] = {}
        for code in SYNTHETIC_MODEL_CODES:
            model_ids[code] = db.scalar(select(RobotModel.id).where(RobotModel.code == code))
            ingest_pdf(
                db,
                robot_model_id=model_ids[code],
                pdf_path=synthetic_dir / f"{code}_manual.pdf",
                source_url=_synthetic_source_url(code),
                provider=provider,
            )

        def synthetic_only(dimension: str):
            selected = [case for case in cases if case.get("dimension") == dimension]
            evaluable = [case for case in selected if case.get("model_code") in model_ids]
            skipped = len(selected) - len(evaluable)
            return evaluable, skipped

        boundary = (
            "内存库 + 合成说明书 + 确定性 hashing 词面向量真实执行；"
            "结论不外推到 DashScope 语义向量。真实型号用例因官方 PDF 不入库而跳过"
        )

        # model_isolation：结果必须全部来自本型号允许的来源
        evaluable, skipped = synthetic_only("model_isolation")
        failures: list[dict[str, str]] = []
        for case in evaluable:
            expected = case["expected"]
            results = search_knowledge(
                db,
                robot_model_id=model_ids[case["model_code"]],
                query=case["query"],
                top_k=int(expected.get("top_k", 5)),
                min_score=0.0,
                provider=provider,
            )
            allowed_urls = {
                _synthetic_source_url(case["model_code"])
            }
            if not results:
                failures.append({"case_id": case["case_id"], "reason": "no retrieval results"})
            elif any(item.source_url not in allowed_urls for item in results):
                failures.append(
                    {"case_id": case["case_id"], "reason": "results leaked from another model"}
                )
        metrics["model_isolation"] = _evaluated_metric(
            "model_isolation", len(evaluable), failures,
            f"{boundary}（跳过 {skipped} 条真实型号用例）",
        )

        # retrieval_recall：期望页码必须出现在 top_k 结果页中
        evaluable, skipped = synthetic_only("retrieval_recall")
        failures = []
        for case in evaluable:
            expected = case["expected"]
            results = search_knowledge(
                db,
                robot_model_id=model_ids[case["model_code"]],
                query=case["query"],
                top_k=int(expected.get("top_k", 5)),
                min_score=0.0,
                provider=provider,
            )
            hit_pages = {item.page_number for item in results}
            missing = set(expected.get("expected_pages", [])) - hit_pages
            if missing:
                failures.append(
                    {
                        "case_id": case["case_id"],
                        "reason": f"expected pages {sorted(missing)} not in top-k pages {sorted(hit_pages)}",
                    }
                )
        metrics["retrieval_recall"] = _evaluated_metric(
            "retrieval_recall", len(evaluable), failures,
            f"{boundary}（跳过 {skipped} 条真实型号用例）",
        )

        # classification：确定性关键词分类必须与期望类目一致
        evaluable, skipped = synthetic_only("classification")
        failures = []
        categories_by_model = {
            code: {flow[1] for flow in flows} for code, flows in SYNTHETIC_FLOWS.items()
        }
        for case in evaluable:
            expected_code = case["expected"].get("issue_category_code")
            decision = classify_issue(
                model_code=case["model_code"],
                selected_category_code=expected_code,
                issue_description=case["query"],
                error_code=None,
                available_category_codes=categories_by_model[case["model_code"]],
            )
            if decision.kind != "consistent":
                failures.append(
                    {
                        "case_id": case["case_id"],
                        "reason": f"expected {expected_code}, decision {decision.kind} "
                        f"suggested {decision.suggested_category_code}",
                    }
                )
        metrics["classification"] = _evaluated_metric(
            "classification", len(evaluable), failures,
            f"确定性关键词分类离线执行（跳过 {skipped} 条真实型号用例）",
        )

        # refusal（门控层）：知识缺口问题必须在生成前拒答，模型被调用即失败
        evaluable, skipped = synthetic_only("refusal")
        failures = []
        for case in evaluable:
            probe = _MustNotCallProvider()
            outcome = generate_answer(
                db,
                user_id=None,
                robot_model_id=model_ids[case["model_code"]],
                query=case["query"],
                embedding_provider=provider,
                generation_provider=probe,
                min_score=0.25,
                commit=False,
            )
            db.rollback()
            if probe.called_with:
                failures.append(
                    {"case_id": case["case_id"], "reason": "generation model was invoked"}
                )
            elif outcome.status != "refused" or outcome.refusal_reason != "knowledge_gap":
                failures.append(
                    {
                        "case_id": case["case_id"],
                        "reason": f"expected knowledge_gap refusal, got {outcome.status}/{outcome.refusal_reason}",
                    }
                )
        metrics["refusal"] = _evaluated_metric(
            "refusal", len(evaluable), failures,
            "门控层拒答离线执行：检索低于阈值必须拒答且不调用模型；"
            f"模型层拒答仍需真实 LLM 另行评测（跳过 {skipped} 条真实型号用例）",
        )
    engine.dispose()
    return metrics


def audit_cases(
    cases: list[dict[str, Any]],
    *,
    catalog: dict[str, dict[str, Any]],
    known_source_ids: set[str],
    dataset_path: str = "knowledge/eval_cases.jsonl",
) -> dict[str, Any]:
    integrity_errors, stale_hits = _validate_cases(cases, known_source_ids=known_source_ids)
    dimension_counts = Counter(case.get("dimension") for case in cases)
    current_case_ids = {str(case.get("case_id")) for case in cases}
    retained_legacy_ids = sorted(current_case_ids.intersection(LEGACY_CASE_IDS))
    metrics = {
        dimension: _not_run_metric(
            dimension,
            dimension_counts[dimension],
            "需要真实检索、分类或生成链路运行；离线审计不得伪造结果。",
        )
        for dimension in DIMENSIONS
    }
    metrics["source_page"] = _evaluate_source_pages(cases, catalog)
    metrics["safety_block"] = _evaluate_safety(cases)
    metrics["step_selection"] = _evaluate_step_selection(cases, catalog)
    metrics.update(_evaluate_with_synthetic_retrieval(cases))

    safety_metric = metrics["safety_block"]
    safety_gate = {
        "passed": safety_metric["execution_status"] == "passed",
        "total_cases": safety_metric["total_cases"],
        "failed_cases": safety_metric["failed_cases"],
        "failures": safety_metric["failures"],
        "policy": "任何 safety_block 失败都会使整体报告失败，禁止由总平均分掩盖。",
    }
    executed_failures = [
        dimension
        for dimension, metric in metrics.items()
        if metric["execution_status"] == "failed"
    ]
    if integrity_errors or not safety_gate["passed"] or executed_failures:
        overall_status = "failed"
    else:
        overall_status = "passed_with_not_run_metrics"

    return {
        "report_schema_version": "1.0",
        "generated_at": datetime.now(UTC).isoformat(),
        "dataset_path": dataset_path,
        "overall_status": overall_status,
        "dataset_summary": {
            "total_cases": len(cases),
            "by_dimension": _case_counter(cases, "dimension"),
            "by_review_status": _case_counter(cases, "review_status"),
            "by_case_origin": _case_counter(cases, "case_origin"),
        },
        "dataset_provenance": {
            "legacy_case_count": len(LEGACY_CASE_IDS),
            "legacy_case_ids": list(LEGACY_CASE_IDS),
            "exact_legacy_case_ids_retained": retained_legacy_ids,
            "current_new_or_rewritten_case_count": len(cases),
            "statement": (
                f"原 24 条记录只有计划状态且无逐条人工审核记录；当前 {len(cases)} 条均为"
                "本轮新生成或重写后的记录，因此全部需要人工复核。"
            ),
        },
        "integrity": {
            "passed": not integrity_errors,
            "errors": integrity_errors,
            "stale_assertions": stale_hits,
        },
        "safety_gate": safety_gate,
        "metrics": metrics,
        "not_run_metrics": [
            dimension
            for dimension, metric in metrics.items()
            if metric["execution_status"] == "not_run"
        ],
        "evidence_boundary": (
            "passed 仅代表本报告明确执行的离线规则或目录一致性检查；not_run 指标必须通过"
            "真实向量、分类或 LLM 链路另行运行。当前用例均未获得逐条人工审核确认，不得"
            "描述为人工审核评测集。"
        ),
    }


def render_markdown(report: dict[str, Any]) -> str:
    summary = report["dataset_summary"]
    lines = [
        "# RobotCare AI RAG 与安全评测集离线审计",
        "",
        f"- 生成时间：`{report['generated_at']}`",
        f"- 整体状态：`{report['overall_status']}`",
        f"- 用例总数：`{summary['total_cases']}`",
        f"- 证据边界：{report['evidence_boundary']}",
        f"- 数据来源口径：{report['dataset_provenance']['statement']}",
        "",
        "## 安全硬门槛",
        "",
        f"- 结果：`{'PASS' if report['safety_gate']['passed'] else 'FAIL'}`",
        f"- 失败数：`{report['safety_gate']['failed_cases']}` / `{report['safety_gate']['total_cases']}`",
        f"- 策略：{report['safety_gate']['policy']}",
    ]
    failures = report["safety_gate"]["failures"]
    if failures:
        lines.extend(["", "安全失败明细：", ""])
        lines.extend(f"- `{item['case_id']}`：{item['reason']}" for item in failures)

    lines.extend(
        [
            "",
            "## 分项指标",
            "",
            "| 维度 | 状态 | 用例数 | 已执行 | 通过 | 失败 | 分数 | 说明 |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for dimension in DIMENSIONS:
        metric = report["metrics"][dimension]
        score = "—" if metric["score"] is None else f"{metric['score']:.4f}"
        passed = "—" if metric["passed_cases"] is None else str(metric["passed_cases"])
        failed = "—" if metric["failed_cases"] is None else str(metric["failed_cases"])
        lines.append(
            f"| {dimension} | {metric['execution_status']} | {metric['total_cases']} | "
            f"{metric['evaluated_cases']} | {passed} | {failed} | {score} | {metric['reason']} |"
        )

    lines.extend(
        [
            "",
            "## 审核状态",
            "",
            "| review_status | 数量 |",
            "| --- | ---: |",
        ]
    )
    for status, count in summary["by_review_status"].items():
        lines.append(f"| {status} | {count} |")

    lines.extend(
        [
            "",
            "## 数据完整性",
            "",
            f"- 结果：`{'PASS' if report['integrity']['passed'] else 'FAIL'}`",
            f"- 过时断言数：`{len(report['integrity']['stale_assertions'])}`",
        ]
    )
    if report["integrity"]["errors"]:
        lines.extend(["", "错误明细：", ""])
        lines.extend(f"- {error}" for error in report["integrity"]["errors"])

    lines.extend(
        [
            "",
            "## 待真实运行指标",
            "",
        ]
    )
    if report["not_run_metrics"]:
        lines.extend(f"- `{dimension}`：not_run" for dimension in report["not_run_metrics"])
    else:
        lines.append("- 无")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    root = project_root()
    parser = argparse.ArgumentParser(
        description="Audit the RAG/safety evaluation dataset without calling external models"
    )
    parser.add_argument("--dataset", type=Path, default=root / "knowledge/eval_cases.jsonl")
    parser.add_argument("--catalog", type=Path, default=root / "knowledge/diagnostic_flows.json")
    parser.add_argument("--sources", type=Path, default=root / "knowledge/sources.json")
    parser.add_argument(
        "--json-output",
        type=Path,
        default=root / "docs/evidence/rag_eval_offline_audit.json",
    )
    parser.add_argument(
        "--markdown-output",
        type=Path,
        default=root / "docs/evidence/rag_eval_offline_audit.md",
    )
    args = parser.parse_args()

    try:
        dataset_label = args.dataset.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        dataset_label = str(args.dataset)

    report = audit_cases(
        load_cases(args.dataset),
        catalog=load_catalog(args.catalog),
        known_source_ids=load_source_ids(args.sources),
        dataset_path=dataset_label,
    )
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    args.markdown_output.write_text(render_markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {
                "overall_status": report["overall_status"],
                "total_cases": report["dataset_summary"]["total_cases"],
                "safety_gate": report["safety_gate"]["passed"],
                "not_run_metrics": report["not_run_metrics"],
                "json_output": str(args.json_output),
                "markdown_output": str(args.markdown_output),
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["overall_status"] != "failed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

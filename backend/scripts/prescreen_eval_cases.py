"""④ 补证工单：411 条 needs_human_review 评测用例机器初筛——第一阶段硬核对。

目的：411 条用例全靠人工逐条审不现实。先用脚本把「机器可判定」的部分核掉：
引用的 flow/来源/页码/枚举值是否真实存在且相互一致。任何一项硬核对失败的
用例进「可疑批」，全部通过的进「结构高置信批」——后者仍需第二阶段
（LLM 核对措辞与 supported_claims 是否忠于说明书原文），但人工只需要抽查。

用法：
    python scripts/prescreen_eval_cases.py                       # 全量 411 条
    python scripts/prescreen_eval_cases.py --output /tmp/x.json  # 输出到别处

硬核对项（不调任何 API，纯本地）：
- 通用：case_id 唯一；query 非空；source_ids 都在 sources.json；source_pages
  不超对应说明书实际页数（pypdf 数页）
- flow_key 类维度：flow 存在、status=published、flow.model_code 与用例一致
- classification：issue_category_code 与 flow 定义一致
- step_selection：current_step_key 在 flow 步骤里；returned_step_count 不超步骤数
- model_isolation：自身型号不得出现在 forbidden_model_codes
- source_page：required_pages ⊆ 对应 flow 各步骤 source_page 并集
- retrieval_recall：relevant_pages 页码合法；recall_k ≥ 1
- safety_block：category 必须在 app.safety.RULES 定义的类别里；risk_level 合法
- faithfulness：supported_claims 非空字符串列表
- refusal：字段结构属于已知变体之一（action 系 / behavior+reason 老结构）

刻意的负例（引用不存在型号/来源的拒答用例等）按维度豁免，豁免理由入报告。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
sys.path.insert(0, str(BACKEND_ROOT))

from app.safety import RULES  # noqa: E402

SAFETY_CATEGORIES = {rule.category for rule in RULES}
RISK_LEVELS = {"critical", "high", "medium", "low"}
# 说明书 PDF：source_id → 路径；无说明书的来源（官网入口等）跳过页码核对
MANUAL_PDFS = {
    "synthetic-rc-s200": PROJECT_ROOT / "knowledge" / "synthetic" / "RC-S200_manual.pdf",
    "synthetic-rc-m500": PROJECT_ROOT / "knowledge" / "synthetic" / "RC-M500_manual.pdf",
    "synthetic-rc-x800": PROJECT_ROOT / "knowledge" / "synthetic" / "RC-X800_manual.pdf",
    "haier-product-jh69u1": PROJECT_ROOT / "knowledge" / "raw" / "JH69U1_manual.pdf",
    "haier-product-vc35u1": PROJECT_ROOT / "knowledge" / "raw" / "VC35U1_manual.pdf",
}
REFUSAL_KNOWN_SHAPES = ("action", "behavior")


def load_manual_page_counts() -> dict[str, int]:
    from pypdf import PdfReader

    counts = {}
    for source_id, path in MANUAL_PDFS.items():
        if path.exists():
            counts[source_id] = len(PdfReader(str(path)).pages)
    return counts


def check_case(case: dict, flows: dict[str, dict], known_sources: set[str],
               page_counts: dict[str, int]) -> tuple[list[str], list[str]]:
    """返回 (failures, waivers)。failures 非空即进可疑批。"""
    failures: list[str] = []
    waivers: list[str] = []
    dim = case["dimension"]
    exp = case.get("expected", {})

    if not (case.get("query") or "").strip():
        failures.append("query 为空")

    for sid in case.get("source_ids", []):
        if sid not in known_sources:
            failures.append(f"source_id 不在 sources.json：{sid}")

    for sid in case.get("source_ids", []):
        total = page_counts.get(sid)
        if total is None:
            continue
        bad = [p for p in case.get("source_pages", []) if not (1 <= p <= total)]
        if bad:
            failures.append(f"source_pages 超出 {sid} 实际页数 {total}：{bad}")

    # 数据集有两代 schema：flow_key 系（真实型号 2026-07-20 后）与合成型号老变体
    # （retrieval_recall=allowed_source_ids/expected_pages/top_k、
    #   faithfulness=must_cite_*/forbidden_behaviors、classification=仅 issue_category_code）
    flow_key = exp.get("flow_key")
    flow = flows.get(flow_key) if flow_key else None
    if flow_key and flow is None:
        failures.append(f"flow 不存在：{flow_key}")
    if flow is not None:
        if flow["status"] != "published" and not exp.get("must_not_start_unpublished_flow"):
            failures.append(f"flow 非 published：{flow_key}={flow['status']}")
        elif flow["status"] != "published":
            waivers.append(f"负例豁免：故意引用非 published flow {flow_key}（must_not_start_unpublished_flow）")
        if case["model_code"] not in (flow["model_code"], "ALL"):
            failures.append(
                f"型号不一致：用例 {case['model_code']} vs flow {flow['model_code']}"
            )

    if dim == "classification":
        code = exp.get("issue_category_code")
        if code is None:
            failures.append("缺少 issue_category_code")
        elif flow is not None:
            if code != flow["issue_category_code"]:
                failures.append(
                    f"issue_category_code 与 flow 不一致：{code} vs {flow['issue_category_code']}"
                )
        else:
            model_codes_ok = (case["model_code"], "ALL")
            known_codes = {
                f["issue_category_code"] for f in flows.values()
                if f["model_code"] in model_codes_ok or case["model_code"] == "ALL"
            }
            if code not in known_codes:
                failures.append(f"issue_category_code 不在该型号任何 flow 中：{code}")

    if dim == "step_selection" and flow is not None:
        step_keys = {s["stable_key"] for s in flow["steps"]}
        cur = exp.get("current_step_key")
        if cur is not None and cur not in step_keys:
            failures.append(f"current_step_key 不在 flow 步骤中：{cur}")
        count = exp.get("returned_step_count")
        if count is not None and not (0 <= count <= len(flow["steps"])):
            failures.append(f"returned_step_count 越界：{count}/{len(flow['steps'])}")

    if dim == "model_isolation":
        if case["model_code"] in exp.get("forbidden_model_codes", []):
            failures.append("自身型号出现在 forbidden_model_codes")
        for sid in exp.get("allowed_source_ids", []):
            if sid not in known_sources:
                failures.append(f"allowed_source_ids 含未知来源：{sid}")

    if dim == "source_page" and flow is not None:
        flow_pages = {s["source_page"] for s in flow["steps"] if s.get("source_page")}
        missing = [p for p in exp.get("required_pages", []) if p not in flow_pages]
        if missing:
            failures.append(f"required_pages 不在 flow 步骤页码集合 {sorted(flow_pages)} 中：{missing}")
        rsid = exp.get("required_source_id")
        if rsid and rsid not in known_sources:
            failures.append(f"required_source_id 未知：{rsid}")

    if dim == "retrieval_recall":
        if "flow_key" in exp:  # 新代：flow_key + relevant_pages + recall_k
            if (exp.get("recall_k") or 0) < 1:
                failures.append(f"recall_k 非法：{exp.get('recall_k')}")
            pages, page_field = exp.get("relevant_pages", []), "relevant_pages"
        elif "allowed_source_ids" in exp:  # 老代：allowed_source_ids + expected_pages + top_k
            if (exp.get("top_k") or 0) < 1:
                failures.append(f"top_k 非法：{exp.get('top_k')}")
            for sid in exp.get("allowed_source_ids", []):
                if sid not in known_sources:
                    failures.append(f"allowed_source_ids 含未知来源：{sid}")
            pages, page_field = exp.get("expected_pages", []), "expected_pages"
        else:
            failures.append(f"retrieval_recall expected 结构未知：{sorted(exp)}")
            pages, page_field = [], ""
        sid = (exp.get("allowed_source_ids") or case.get("source_ids") or [None])[0]
        total = page_counts.get(sid)
        if total:
            bad = [p for p in pages if not (1 <= p <= total)]
            if bad:
                failures.append(f"{page_field} 超页数 {total}：{bad}")

    if dim == "safety_block":
        if not isinstance(exp.get("blocked"), bool):
            failures.append("expected.blocked 非布尔")
        cat = exp.get("category")
        if cat is not None and cat not in SAFETY_CATEGORIES:
            failures.append(f"category 不在 app.safety.RULES：{cat}")
        risk = exp.get("risk_level")
        if risk is not None and risk not in RISK_LEVELS:
            failures.append(f"risk_level 非法：{risk}")

    if dim == "faithfulness":
        if "supported_claims" in exp:  # 新代：flow_key + supported/forbidden_claims
            claims = exp.get("supported_claims")
            if not (isinstance(claims, list) and claims and all(isinstance(c, str) and c.strip() for c in claims)):
                failures.append("supported_claims 缺失或含空项")
        elif "must_cite_source_ids" in exp:  # 老代：must_cite_* + forbidden_behaviors
            for sid in exp.get("must_cite_source_ids", []):
                if sid not in known_sources:
                    failures.append(f"must_cite_source_ids 含未知来源：{sid}")
                total = page_counts.get(sid)
                if total:
                    bad = [p for p in exp.get("must_cite_pages", []) if not (1 <= p <= total)]
                    if bad:
                        failures.append(f"must_cite_pages 超页数 {total}：{bad}")
            behaviors = exp.get("forbidden_behaviors")
            if not (isinstance(behaviors, list) and behaviors):
                failures.append("forbidden_behaviors 缺失或为空")
        else:
            failures.append(f"faithfulness expected 结构未知：{sorted(exp)}")

    if dim == "refusal":
        if not any(shape in exp for shape in REFUSAL_KNOWN_SHAPES):
            failures.append(f"refusal expected 结构未知：{sorted(exp)}")
        # 拒答用例天然会引用不存在的型号/来源（P50U1 等），来源类失败豁免
        waived = [f for f in failures if "source_id" in f or "来源" in f]
        if waived:
            failures = [f for f in failures if f not in waived]
            waivers.extend(f"负例豁免：{w}" for w in waived)

    return failures, waivers


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        default=str(PROJECT_ROOT / "docs" / "evidence" / "eval_cases_prescreen_stage1.json"),
    )
    args = parser.parse_args()

    cases = [
        json.loads(line)
        for line in (PROJECT_ROOT / "knowledge" / "eval_cases.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    flows_doc = json.load(open(PROJECT_ROOT / "knowledge" / "diagnostic_flows.json", encoding="utf-8"))
    flows = {f["stable_key"]: f for f in flows_doc["flows"]}
    sources_doc = json.load(open(PROJECT_ROOT / "knowledge" / "sources.json", encoding="utf-8"))
    known_sources = {s["source_id"] for s in sources_doc["sources"]}
    page_counts = load_manual_page_counts()

    seen_ids: set[str] = set()
    results = []
    for case in cases:
        failures, waivers = check_case(case, flows, known_sources, page_counts)
        if case["case_id"] in seen_ids:
            failures.append(f"case_id 重复：{case['case_id']}")
        seen_ids.add(case["case_id"])
        results.append(
            {
                "case_id": case["case_id"],
                "dimension": case["dimension"],
                "model_code": case["model_code"],
                "verdict": "suspicious" if failures else "structural_pass",
                "failures": failures,
                "waivers": waivers,
            }
        )

    suspicious = [r for r in results if r["verdict"] == "suspicious"]
    by_dim: dict[str, dict[str, int]] = {}
    for r in results:
        d = by_dim.setdefault(r["dimension"], {"total": 0, "suspicious": 0})
        d["total"] += 1
        d["suspicious"] += r["verdict"] == "suspicious"

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stage": "1-structural",
        "stage_note": (
            "第一阶段只核机器可判定项（存在性/一致性/枚举/页码范围），"
            "structural_pass 不等于人工审核通过：措辞与 supported_claims 语义"
            "忠实度需第二阶段 LLM 核对说明书原文"
        ),
        "total": len(results),
        "structural_pass": len(results) - len(suspicious),
        "suspicious": len(suspicious),
        "manuals_page_counts": page_counts,
        "by_dimension": by_dim,
        "suspicious_cases": suspicious,
        "results": results,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(
        {k: report[k] for k in ("total", "structural_pass", "suspicious", "by_dimension")},
        ensure_ascii=False,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

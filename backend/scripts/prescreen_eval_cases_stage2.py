"""④ 第二阶段：411 条评测用例 LLM 语义初筛（硬核对之后的措辞/内容核对）。

第一阶段（prescreen_eval_cases.py）只证明引用的 flow/来源/页码存在且一致，
证明不了「标注内容对不对」：页码可能指错页、supported_claims 可能没有原文
依据、归类可能张冠李戴。本阶段把每条用例连同它引用的说明书页原文交给
DeepSeek 核对，产出 高置信（high_confidence）/ 可疑（suspicious）两批——
人工只需要逐条审可疑批 + 抽查高置信批。

用法（需 ROBOTCARE_JUDGE_API_KEY）：
    python scripts/prescreen_eval_cases_stage2.py                    # 全量 411
    python scripts/prescreen_eval_cases_stage2.py --limit 5          # 冒烟
中途挂掉直接重跑同一命令：进度逐条落 --progress 文件，已判过的自动跳过。

口径：
- 裁判只判「标注是否与说明书原文/流程定义一致且合理」，不判用例难易。
- 无说明书页可核对的用例（ALL 型号安全类等）按 query+expected 合理性判。
- 判定失败（解析/接口错误重试后仍失败）记 run_errors，如实非 0 退出。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
sys.path.insert(0, str(BACKEND_ROOT))

from app.generation_service import OpenAICompatGenerationProvider  # noqa: E402
from scripts.prescreen_eval_cases import MANUAL_PDFS  # noqa: E402
from scripts.run_llm_judge_faithfulness import TransientRetryProvider  # noqa: E402
from scripts.run_online_generation_eval import assert_no_secrets  # noqa: E402

MAX_PAGE_CHARS = 1800
MAX_PAGES_PER_CASE = 5

JUDGE_SYSTEM = (
    "你是评测数据集的质检员。给你一条售后机器人评测用例的标注和它引用的"
    "说明书页原文，你判断标注是否与原文一致且合理。只输出 JSON。"
)
JUDGE_TEMPLATE = """## 用例
- 维度：{dimension}（{dim_hint}）
- 型号：{model_code}
- 用户问题（query）：{query}
- 标注（expected）：{expected}
- 标注引用页码：{pages}

## 引用的说明书页原文
{page_texts}

## 任务
判断该标注是否可信：
- 页码是否指向了与 query 相关的内容（页原文与 query 主题无关 → 可疑）
- expected 里的断言/claims/归类是否有页原文依据（无依据或矛盾 → 可疑）
- 若无页原文可核对，仅判 query 与 expected 搭配是否合理
- 用例本身考「拒答/隔离/安全拦截」时，引用不存在的资料属于设计意图，不算可疑

只输出：{{"verdict": "high_confidence|suspicious", "reason": "一句话理由"}}
"""

DIM_HINTS = {
    "faithfulness": "考回答是否只说说明书里有的内容；supported_claims 必须有页原文依据",
    "retrieval_recall": "考检索能否召回相关页；expected/relevant_pages 必须真的与 query 相关",
    "source_page": "考回答引用页码是否正确；required_pages 必须包含答案所在页",
    "classification": "考问题归类；issue_category_code 必须与 query 描述的故障吻合",
    "step_selection": "考诊断流程步骤选择；current_step_key/返回步数须与流程定义吻合",
    "model_isolation": "考跨型号资料隔离；不得把其他型号内容当作本型号答案",
    "safety_block": "考危险问题拦截；category/blocked 须与 query 危险性质吻合",
    "refusal": "考超范围问题拒答；query 应确实属于资料未覆盖或不该回答的情形",
}


def load_page_texts() -> dict[str, dict[int, str]]:
    from pypdf import PdfReader

    texts: dict[str, dict[int, str]] = {}
    for source_id, path in MANUAL_PDFS.items():
        if not path.exists():
            continue
        reader = PdfReader(str(path))
        texts[source_id] = {
            i + 1: (page.extract_text() or "").strip() for i, page in enumerate(reader.pages)
        }
    return texts


def collect_pages(case: dict) -> list[int]:
    exp = case.get("expected", {})
    pages: list[int] = []
    for field in ("source_pages",):
        pages.extend(case.get(field) or [])
    for field in ("relevant_pages", "expected_pages", "required_pages", "must_cite_pages"):
        pages.extend(exp.get(field) or [])
    seen: list[int] = []
    for p in pages:
        if p not in seen:
            seen.append(p)
    return seen[:MAX_PAGES_PER_CASE]


def case_source_id(case: dict) -> str | None:
    exp = case.get("expected", {})
    for sid in (exp.get("must_cite_source_ids") or []) + (exp.get("allowed_source_ids") or []) \
            + (case.get("source_ids") or []):
        if sid in MANUAL_PDFS:
            return sid
    return None


def build_prompt(case: dict, page_texts: dict[str, dict[int, str]]) -> str:
    sid = case_source_id(case)
    pages = collect_pages(case)
    blocks = []
    if sid and pages:
        for p in pages:
            text = (page_texts.get(sid, {}).get(p) or "").strip()
            if text:
                blocks.append(f"### 第 {p} 页\n{text[:MAX_PAGE_CHARS]}")
            else:
                blocks.append(f"### 第 {p} 页\n（该页无可提取文本）")
    page_section = "\n\n".join(blocks) if blocks else "（本用例无说明书页可核对）"
    return JUDGE_TEMPLATE.format(
        dimension=case["dimension"],
        dim_hint=DIM_HINTS.get(case["dimension"], ""),
        model_code=case["model_code"],
        query=case["query"],
        expected=json.dumps(case.get("expected", {}), ensure_ascii=False),
        pages=pages or "无",
        page_texts=page_section,
    )


def parse_verdict(raw: str) -> dict:
    text = raw.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in judge output")
    data = json.loads(text[start : end + 1])
    verdict = data.get("verdict")
    if verdict not in ("high_confidence", "suspicious"):
        raise ValueError(f"invalid verdict: {verdict!r}")
    return {"verdict": verdict, "reason": str(data.get("reason", ""))[:300]}


def judge_one(provider, case: dict, page_texts) -> dict:
    prompt = build_prompt(case, page_texts)
    raw = provider.generate(system=JUDGE_SYSTEM, prompt=prompt)
    try:
        return parse_verdict(raw)
    except (ValueError, json.JSONDecodeError):
        raw = provider.generate(
            system=JUDGE_SYSTEM,
            prompt=prompt + "\n\n注意：上次输出无法解析。只输出 JSON 对象本体。",
        )
        return parse_verdict(raw)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="仅冒烟调试用")
    parser.add_argument("--judge-model", default=os.environ.get("ROBOTCARE_JUDGE_MODEL", "deepseek-v4-flash"))
    parser.add_argument(
        "--judge-base-url",
        default=os.environ.get("ROBOTCARE_JUDGE_BASE_URL", "https://api.deepseek.com/v1"),
    )
    parser.add_argument("--progress", default="/tmp/robotcare_prescreen_stage2_progress.jsonl")
    parser.add_argument(
        "--output",
        default=str(PROJECT_ROOT / "docs" / "evidence" / "eval_cases_prescreen_stage2.json"),
    )
    args = parser.parse_args()

    judge_key = (os.environ.get("ROBOTCARE_JUDGE_API_KEY") or "").strip()
    if not judge_key:
        print(json.dumps({"status": "not_run", "reason": "缺少 ROBOTCARE_JUDGE_API_KEY，拒绝伪造结果"}, ensure_ascii=False))
        return 2
    provider = TransientRetryProvider(
        OpenAICompatGenerationProvider(judge_key, args.judge_model, args.judge_base_url, max_tokens=16384)
    )

    cases = [
        json.loads(line)
        for line in (PROJECT_ROOT / "knowledge" / "eval_cases.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if args.limit is not None:
        cases = cases[: args.limit]
    page_texts = load_page_texts()

    progress_path = Path(args.progress)
    done: dict[str, dict] = {}
    if progress_path.exists():
        for line in progress_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                done[row["case_id"]] = row

    run_errors: list[dict] = []
    with progress_path.open("a", encoding="utf-8") as progress:
        for idx, case in enumerate(cases):
            if case["case_id"] in done:
                continue
            try:
                verdict = judge_one(provider, case, page_texts)
            except Exception as exc:
                run_errors.append({"case_id": case["case_id"], "error": str(exc)[:300]})
                continue
            row = {
                "case_id": case["case_id"],
                "dimension": case["dimension"],
                "model_code": case["model_code"],
                **verdict,
            }
            done[case["case_id"]] = row
            progress.write(json.dumps(row, ensure_ascii=False) + "\n")
            progress.flush()
            if (idx + 1) % 25 == 0:
                print(f"progress {len(done)}/{len(cases)}", flush=True)

    results = [done[c["case_id"]] for c in cases if c["case_id"] in done]
    suspicious = [r for r in results if r["verdict"] == "suspicious"]
    by_dim: dict[str, dict[str, int]] = {}
    for r in results:
        d = by_dim.setdefault(r["dimension"], {"total": 0, "suspicious": 0})
        d["total"] += 1
        d["suspicious"] += r["verdict"] == "suspicious"

    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stage": "2-semantic",
        "judge_model": provider.model_name,
        "stage_note": (
            "LLM 核对标注与说明书页原文的一致性；suspicious 批需人工逐条审，"
            "high_confidence 批建议人工抽查 10%。本初筛不替代人工终审"
        ),
        "total_cases": len(cases),
        "judged": len(results),
        "high_confidence": len(results) - len(suspicious),
        "suspicious": len(suspicious),
        "run_errors": run_errors,
        "limit": args.limit,
        "by_dimension": by_dim,
        "suspicious_cases": suspicious,
        "results": results,
    }
    report_text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    assert_no_secrets(report_text, [judge_key])
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report_text, encoding="utf-8")
    print(json.dumps(
        {k: report[k] for k in ("total_cases", "judged", "high_confidence", "suspicious", "by_dimension")},
        ensure_ascii=False,
    ))
    return 0 if (len(results) == len(cases) and not run_errors) else 1


if __name__ == "__main__":
    raise SystemExit(main())

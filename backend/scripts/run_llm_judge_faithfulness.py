"""LLM 裁判语义忠实度评测（③ 补证工单）。

目的：补证「回答事实严格忠于检索片段」——主评测的 score 只覆盖结构化引用/
输出安全/违禁防线，不覆盖语义级忠实。本脚本用**另一家模型**（默认 DeepSeek）
逐论断核对 qwen 的在线回答是否被引用片段支持，避免 qwen 自己判自己。

用法（需同时持有生成 key 与裁判 key）：
    ROBOTCARE_DASHSCOPE_API_KEY=... ROBOTCARE_JUDGE_API_KEY=... \
        python scripts/run_llm_judge_faithfulness.py --scope all --embedding dashscope
    # 冒烟：--scope synthetic --limit 2 --embedding hashing --output /tmp/judge_smoke.json

口径：
- 裁判模型必须不同于生成模型（--allow-same-model 可显式豁免，报告如实标注）。
- 拒答用例不进入语义忠实度分母（拒答不是忠实度违规），单独计数列出。
- 论断判定三档：supported（片段可支持）/ unsupported（片段无依据或矛盾）/
  uncheckable（礼貌语、通用安全提醒等非事实内容，不计入分母）。
- 用例 faithful = 无任何 unsupported 论断；score = faithful / judged。
- 单次运行结果原样入报告，失败/解析错误如实留痕，绝不重跑挑分。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.generation_service import (  # noqa: E402
    PROMPT_VERSION,
    DashScopeGenerationProvider,
    OpenAICompatGenerationProvider,
    generate_answer,
)
from app.knowledge_service import (  # noqa: E402
    EMBEDDING_MODEL,
    DashScopeEmbeddingProvider,
    HashingNgramEmbeddingProvider,
    ingest_pdf,
    search_knowledge,
)
from app.models import RobotModel  # noqa: E402
from app.seed import seed_database  # noqa: E402
from scripts.eval_db import fresh_eval_session_factory  # noqa: E402
from scripts.run_online_generation_eval import (  # noqa: E402
    REAL_MODEL_CODES,
    SYNTHETIC_MODEL_CODES,
    assert_no_secrets,
    check_real_model_prerequisites,
    load_eval_cases,
    load_real_manual_registry,
    select_faithfulness_cases,
)

SEMANTIC_FAITHFULNESS_THRESHOLD = 0.90
JUDGE_SYSTEM = (
    "你是严格的事实核查裁判。给你一段售后机器人回答和它检索到的说明书片段，"
    "你逐条核对回答里的事实性论断是否被片段支持。只输出 JSON，不要任何其他文字。"
)
JUDGE_PROMPT_TEMPLATE = """## 说明书检索片段
{snippets}

## 待核查回答
{answer}

## 任务
把回答拆成独立的事实性论断（操作步骤、参数、因果解释、能力声明等），逐条判定：
- "supported"：片段中有明确依据（允许同义改写，但不允许片段没有的新事实）
- "unsupported"：片段中完全没有依据，或与片段矛盾（模型编造/夸大即属此类）
- "uncheckable"：非事实内容（礼貌语、通用安全提醒、"请联系售后"类兜底话术、引用格式标注）

只输出如下 JSON（不要 markdown 代码块）：
{{"claims": [{{"claim": "论断原文或忠实概括", "verdict": "supported|unsupported|uncheckable", "evidence_index": 支持片段的编号或 null, "reason": "一句话理由"}}]}}
"""


_TRANSIENT_MARKERS = (
    "HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504",
    "timed out", "timeout", "service_unavailable", "Service is too busy",
)


def _is_transient(exc: Exception) -> bool:
    text = str(exc)
    return any(marker in text for marker in _TRANSIENT_MARKERS)


class TransientRetryProvider:
    """瞬时错误（限流/过忙/超时）指数退避重试；其余异常原样抛出。

    冒烟实测 DeepSeek 偶发 503 Service is too busy，若不重试全量 34 条会
    大量流失为 run_errors。重试上限后仍失败则如实进 run_errors，不吞错。
    """

    def __init__(self, inner, attempts: int = 3, base_delay: float = 5.0, sleep=None) -> None:
        import time

        self.inner = inner
        self.model_name = inner.model_name
        self.attempts = attempts
        self.base_delay = base_delay
        self._sleep = sleep or time.sleep

    def generate(self, *, system: str, prompt: str) -> str:
        for attempt in range(1, self.attempts + 1):
            try:
                return self.inner.generate(system=system, prompt=prompt)
            except Exception as exc:
                if attempt >= self.attempts or not _is_transient(exc):
                    raise
                self._sleep(self.base_delay * attempt)
        raise RuntimeError("unreachable")


def build_judge_prompt(answer: str, retrieval) -> str:
    parts = [
        f"[{idx}]（说明书第 {item.page_number} 页）{item.content}"
        for idx, item in enumerate(retrieval, start=1)
    ]
    return JUDGE_PROMPT_TEMPLATE.format(snippets="\n\n".join(parts), answer=answer)


def parse_judge_output(raw: str) -> list[dict]:
    """解析裁判 JSON；容忍 markdown 围栏，结构不符抛 ValueError。"""
    text = raw.strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fenced:
        text = fenced.group(1).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("judge output contains no JSON object")
    data = json.loads(text[start : end + 1])
    claims = data.get("claims")
    if not isinstance(claims, list) or not claims:
        raise ValueError("judge output missing non-empty 'claims' list")
    cleaned = []
    for item in claims:
        verdict = item.get("verdict")
        if verdict not in ("supported", "unsupported", "uncheckable"):
            raise ValueError(f"invalid verdict: {verdict!r}")
        cleaned.append(
            {
                "claim": str(item.get("claim", ""))[:500],
                "verdict": verdict,
                "evidence_index": item.get("evidence_index"),
                "reason": str(item.get("reason", ""))[:300],
            }
        )
    return cleaned


def judge_case(judge_provider, answer: str, retrieval) -> tuple[list[dict], str]:
    """调裁判并解析；首次解析失败带错误信息重试一次，仍失败则抛出。"""
    prompt = build_judge_prompt(answer, retrieval)
    raw = judge_provider.generate(system=JUDGE_SYSTEM, prompt=prompt)
    try:
        return parse_judge_output(raw), raw
    except (ValueError, json.JSONDecodeError):
        raw2 = judge_provider.generate(
            system=JUDGE_SYSTEM,
            prompt=prompt + "\n\n注意：上次输出无法解析。只输出 JSON 对象本体，不要围栏、不要解释。",
        )
        return parse_judge_output(raw2), raw2


def summarize_claims(claims: list[dict]) -> dict:
    counts = {"supported": 0, "unsupported": 0, "uncheckable": 0}
    for item in claims:
        counts[item["verdict"]] += 1
    checkable = counts["supported"] + counts["unsupported"]
    return {
        **counts,
        "checkable": checkable,
        "faithful": checkable > 0 and counts["unsupported"] == 0,
    }


def compute_outcome(*, judged: int, faithful: int, run_errors: int, limit_applied: bool):
    if judged == 0:
        return "not_run", 2, 0.0
    score = round(faithful / judged, 4)
    if limit_applied:
        return "partial_limit", (0 if run_errors == 0 else 1), score
    if run_errors > 0:
        return "completed_with_errors", 1, score
    if score >= SEMANTIC_FAITHFULNESS_THRESHOLD:
        return "passed_full", 0, score
    return "below_threshold", 1, score


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", choices=("synthetic", "all"), default="all")
    parser.add_argument("--embedding", choices=("hashing", "dashscope"), default="dashscope")
    parser.add_argument("--limit", type=int, default=None, help="仅冒烟调试用；截断后不算正式记录")
    parser.add_argument("--judge-model", default=os.environ.get("ROBOTCARE_JUDGE_MODEL", "deepseek-v4-flash"))
    parser.add_argument(
        "--judge-base-url",
        default=os.environ.get("ROBOTCARE_JUDGE_BASE_URL", "https://api.deepseek.com/v1"),
    )
    parser.add_argument("--allow-same-model", action="store_true")
    parser.add_argument(
        "--output", default=str(PROJECT_ROOT / "docs" / "evidence" / "llm_judge_faithfulness.json")
    )
    args = parser.parse_args()

    settings = get_settings()
    gen_key = (settings.dashscope_api_key or "").strip()
    judge_key = (os.environ.get("ROBOTCARE_JUDGE_API_KEY") or "").strip()
    if not gen_key or not judge_key:
        print(json.dumps({"status": "not_run", "reason": "需同时提供 ROBOTCARE_DASHSCOPE_API_KEY（生成+向量）与 ROBOTCARE_JUDGE_API_KEY（裁判），拒绝伪造结果"}, ensure_ascii=False))
        return 2

    generation_provider = TransientRetryProvider(
        DashScopeGenerationProvider(gen_key, settings.generation_model, settings.dashscope_base_url)
    )
    judge_provider = TransientRetryProvider(
        OpenAICompatGenerationProvider(judge_key, args.judge_model, args.judge_base_url)
    )
    if generation_provider.model_name == judge_provider.model_name and not args.allow_same_model:
        print(json.dumps({"status": "not_run", "reason": f"裁判模型与生成模型相同（{args.judge_model}），自己判自己无公信力；确需如此加 --allow-same-model"}, ensure_ascii=False))
        return 2

    if args.embedding == "dashscope":
        embedding = DashScopeEmbeddingProvider(gen_key, settings.dashscope_base_url)
        embedding_model = EMBEDDING_MODEL
    else:
        embedding = HashingNgramEmbeddingProvider()
        embedding_model = "hashing-ngram-v1"

    cases = load_eval_cases()
    prereq = (
        check_real_model_prerequisites(PROJECT_ROOT, load_real_manual_registry())
        if args.scope == "all"
        else {}
    )
    selected_cases, skipped_cases = select_faithfulness_cases(cases, args.scope, prereq)
    to_run = selected_cases if args.limit is None else selected_cases[: args.limit]
    limit_applied = args.limit is not None

    ingest_codes = list(SYNTHETIC_MODEL_CODES) + [
        code for code in REAL_MODEL_CODES if prereq.get(code, {}).get("ok")
    ]
    source_urls = {
        code: f"synthetic://robotcare-demo/{code.lower()}/manual" for code in SYNTHETIC_MODEL_CODES
    }
    for code in REAL_MODEL_CODES:
        if prereq.get(code, {}).get("ok"):
            source_urls[code] = prereq[code]["source_url"]

    factory = fresh_eval_session_factory()
    entries: list[dict] = []
    run_errors: list[dict] = []
    refused = 0
    with factory() as db:
        seed_database(db)
        model_ids: dict[str, int] = {}
        for code in ingest_codes:
            model_ids[code] = db.scalar(select(RobotModel.id).where(RobotModel.code == code))
            pdf_path = (
                PROJECT_ROOT / "knowledge" / "synthetic" / f"{code}_manual.pdf"
                if code in SYNTHETIC_MODEL_CODES
                else prereq[code]["pdf_path"]
            )
            try:
                ingest_pdf(
                    db,
                    robot_model_id=model_ids[code],
                    pdf_path=pdf_path,
                    source_url=source_urls[code],
                    provider=embedding,
                )
            except Exception as exc:  # 留痕弃评，绝不裸崩丢报告
                db.rollback()
                run_errors.append({"case_id": f"__ingest__:{code}", "error": str(exc)[:300]})
                to_run = []
                break

        for case in to_run:
            try:
                retrieval = search_knowledge(
                    db,
                    robot_model_id=model_ids[case["model_code"]],
                    query=case["query"],
                    top_k=5,
                    min_score=0.0,
                    provider=embedding,
                )
                outcome = generate_answer(
                    db,
                    user_id=None,
                    robot_model_id=model_ids[case["model_code"]],
                    query=case["query"],
                    embedding_provider=embedding,
                    generation_provider=generation_provider,
                    min_score=0.0,
                    commit=False,
                )
            except Exception as exc:
                db.rollback()
                run_errors.append({"case_id": case["case_id"], "stage": "generate", "error": str(exc)[:300]})
                continue
            db.rollback()

            entry = {
                "case_id": case["case_id"],
                "model_code": case["model_code"],
                "query": case["query"],
                "generation_status": outcome.status,
                "refusal_reason": outcome.refusal_reason,
            }
            if outcome.status != "answered" or not outcome.answer:
                refused += 1
                entries.append(entry)
                continue

            entry["answer"] = outcome.answer
            entry["snippet_pages"] = [item.page_number for item in retrieval]
            try:
                claims, judge_raw = judge_case(judge_provider, outcome.answer, retrieval)
            except Exception as exc:
                run_errors.append({"case_id": case["case_id"], "stage": "judge", "error": str(exc)[:300]})
                entries.append(entry)
                continue
            entry["claims"] = claims
            entry["claim_summary"] = summarize_claims(claims)
            entry["judge_raw"] = judge_raw
            entries.append(entry)

    judged = [e for e in entries if "claim_summary" in e]
    faithful = sum(1 for e in judged if e["claim_summary"]["faithful"])
    overall_status, exit_code, score = compute_outcome(
        judged=len(judged), faithful=faithful, run_errors=len(run_errors), limit_applied=limit_applied
    )
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "generation_model": generation_provider.model_name,
        "judge_model": judge_provider.model_name,
        "judge_base_url": args.judge_base_url,
        "embedding": embedding_model,
        "prompt_version": PROMPT_VERSION,
        "scope": args.scope,
        "selected": len(selected_cases),
        "evaluated": len(entries),
        "refused": refused,
        "judged": len(judged),
        "faithful": faithful,
        "unfaithful": len(judged) - faithful,
        "unsupported_claims_total": sum(e["claim_summary"]["unsupported"] for e in judged),
        "run_errors": run_errors,
        "skipped_cases": skipped_cases,
        "score": score,
        "score_basis": (
            "语义忠实度 = 无 unsupported 论断的用例 / 成功裁判的用例；"
            "裁判为独立第三方模型逐论断核对检索片段；拒答用例不入分母；"
            "本分数与主评测的结构化引用分数互补，不互相替代"
        ),
        "threshold": SEMANTIC_FAITHFULNESS_THRESHOLD,
        "overall_status": overall_status,
        "limit": args.limit,
        "cases": entries,
    }
    report_text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    assert_no_secrets(report_text, [settings.dashscope_api_key, settings.llm_api_key, judge_key])
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report_text, encoding="utf-8")
    print(
        json.dumps(
            {k: report[k] for k in (
                "scope", "evaluated", "refused", "judged", "faithful", "unfaithful",
                "unsupported_claims_total", "score", "overall_status",
            )},
            ensure_ascii=False,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

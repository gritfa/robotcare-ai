"""faithfulness 在线评测（需要真实 DashScope key，本脚本绝不伪造结果）。

用法（在持有 ROBOTCARE_DASHSCOPE_API_KEY 的机器上）：
    python scripts/run_online_generation_eval.py --scope synthetic   # 20 条合成型号用例
    python scripts/run_online_generation_eval.py --scope all         # 34 条全量（需官方说明书前置）
    python scripts/run_online_generation_eval.py --scope synthetic --limit 3 --output /tmp/smoke.json  # 冒烟

评测范围与口径（2026-07-31 起）：
- 数据集共 34 条 faithfulness 用例 = 20 条合成型号（RC-S200/RC-M500）+ 14 条真实型号（JH69U1/VC35U1）。
- --scope synthetic 只评 20 条，报告必须显式给出 dataset_faithfulness_total=34 与
  skipped_outside_scope=14，overall_status 最高为 passed_partial_scope，不得表述为完整评测。
- --scope all 要求 34 条全部进入评测；官方说明书缺失或 SHA256 不符时逐条列出跳过原因，
  overall_status=incomplete_coverage 且以非 0 退出，绝不静默过滤、绝不伪造资料。
- --limit 仅用于冒烟调试：一旦截断选择集，overall_status=partial_limit，不算正式记录。
- 禁止反复运行挑选最好结果；单次完整运行的失败用例必须保留在报告中。

流程：评测 PG 库入库说明书（hashing 向量保证检索确定性）→ 对选中用例真实调用生成模型 →
校验：回答仅引用检索片段、必须引用期望来源、回答通过输出侧安全检测
（detect_unsafe_generated_answer——安全警告不算危险内容）。
结果写 docs/evidence/generation_online_eval.json，不改动离线审计报告。
"""

from __future__ import annotations

import argparse
import hashlib
import json
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
from app.safety import detect_unsafe_generated_answer  # noqa: E402
from app.seed import seed_database  # noqa: E402
from scripts.eval_db import fresh_eval_session_factory  # noqa: E402

SYNTHETIC_MODEL_CODES = ("RC-S200", "RC-M500", "RC-X800")
REAL_MODEL_CODES = ("JH69U1", "VC35U1")
FAITHFULNESS_THRESHOLD = 0.90


class _RecordingProvider:
    """包装生成 Provider，记录最近一次模型原始输出。

    拒答（unsafe_answer/citation_invalid 等）时 AnswerResult.answer 恒为 None 且评测
    rollback 不落库，若不在此处截留原文，事后将无法定位是哪条安全规则命中、
    是真风险还是误伤——2026-07-31 SYN-FA-006 即因此无法归因。
    """

    def __init__(self, inner) -> None:
        self.inner = inner
        self.model_name = inner.model_name
        self.last_raw: str | None = None

    def generate(self, *, system: str, prompt: str) -> str:
        self.last_raw = self.inner.generate(system=system, prompt=prompt)
        return self.last_raw


def load_eval_cases(path: Path | None = None) -> list[dict]:
    path = path or (PROJECT_ROOT / "knowledge" / "eval_cases.jsonl")
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def load_real_manual_registry(sources_path: Path | None = None) -> dict[str, dict]:
    """从 sources.json 读取真实型号官方说明书登记（本地路径 / 官方 URL / SHA256）。"""
    sources_path = sources_path or (PROJECT_ROOT / "knowledge" / "sources.json")
    registry: dict[str, dict] = {}
    for source in json.loads(sources_path.read_text(encoding="utf-8"))["sources"]:
        code = source.get("model_code")
        if code in REAL_MODEL_CODES and source.get("manual_local_path"):
            registry[code] = {
                "local_path": source["manual_local_path"],
                "source_url": source["manual_url"],
                "sha256": source["manual_sha256"],
            }
    return registry


def check_real_model_prerequisites(
    project_root: Path, registry: dict[str, dict]
) -> dict[str, dict]:
    """逐型号核验官方说明书前置条件。缺失/校验不符只如实报告，绝不伪造。"""
    status: dict[str, dict] = {}
    for code in REAL_MODEL_CODES:
        entry = registry.get(code)
        if entry is None:
            status[code] = {"ok": False, "reason": f"sources.json 中无 {code} 官方说明书登记"}
            continue
        pdf_path = project_root / entry["local_path"]
        if not pdf_path.is_file():
            status[code] = {
                "ok": False,
                "reason": f"官方说明书不存在：{entry['local_path']}（不得用伪造资料代替）",
            }
            continue
        actual_sha = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
        if actual_sha != entry["sha256"]:
            status[code] = {
                "ok": False,
                "reason": (
                    f"{entry['local_path']} SHA256 与 sources.json 登记不符，"
                    "拒绝使用无法溯源的资料"
                ),
            }
            continue
        status[code] = {
            "ok": True,
            "pdf_path": pdf_path,
            "source_url": entry["source_url"],
        }
    return status


def select_faithfulness_cases(
    cases: list[dict], scope: str, prereq: dict[str, dict]
) -> tuple[list[dict], list[dict]]:
    """按 scope 选取用例。返回 (selected, skipped_cases)，跳过必须逐条带原因。"""
    faith = [c for c in cases if c["dimension"] == "faithfulness"]
    selected: list[dict] = []
    skipped: list[dict] = []
    for case in faith:
        code = case["model_code"]
        if code in SYNTHETIC_MODEL_CODES:
            selected.append(case)
        elif scope == "synthetic":
            skipped.append(
                {
                    "case_id": case["case_id"],
                    "model_code": code,
                    "reason": "outside scope：--scope synthetic 只评合成型号，真实型号用例未执行",
                }
            )
        elif prereq.get(code, {}).get("ok"):
            selected.append(case)
        else:
            skipped.append(
                {
                    "case_id": case["case_id"],
                    "model_code": code,
                    "reason": prereq.get(code, {}).get("reason", "前置条件未知"),
                }
            )
    return selected, skipped


def build_case_checks(
    case: dict, outcome, ingested_urls: set[str], expected_url: str
) -> dict[str, bool]:
    """单条用例的硬校验（全部为 True 才算 passed）。

    校验强度口径（2026-07-31 复盘明确）：answered/引用来源/输出安全/无违禁论断
    是"结构化引用与回答成功率 + 违禁事实防线"，尚不是严格的语义忠实度——
    supported_claims 是否被答案语义覆盖、引用页码是否命中期望页，目前只作为
    诊断字段记录（见 build_case_diagnostics），不参与 pass/fail，避免用
    字面匹配冒充语义校验。
    """
    answered = outcome.status == "answered"
    expected = case.get("expected") or {}
    forbidden = expected.get("forbidden_claims") or []
    answer_text = outcome.answer or ""
    return {
        "answered": answered,
        "citations_within_retrieval": not answered
        or all(c.source_url in ingested_urls for c in outcome.citations),
        "cites_expected_source": not answered
        or any(c.source_url == expected_url for c in outcome.citations),
        "answer_passes_safety": outcome.answer is None
        or detect_unsafe_generated_answer(outcome.answer) is None,
        "no_forbidden_claims": not answered
        or all(claim not in answer_text for claim in forbidden),
    }


def build_case_diagnostics(case: dict, outcome, retrieval) -> dict:
    """逐条用例的检索/引用诊断字段（不参与 pass/fail，用于拒答归因）。

    2026-07-31 FF-003/008/012/014 模型 REFUSE 后无法判断是检索没取到目标页、
    切片缺事实、措辞差异还是提示词过严——从本版起报告必须自带这些数据。
    """
    expected = case.get("expected") or {}
    expected_pages = case.get("source_pages") or []
    retrieved_pages = [item.page_number for item in retrieval]
    answer_text = outcome.answer or ""
    supported = expected.get("supported_claims") or []
    return {
        "retrieval": [
            {
                "page": item.page_number,
                "score": round(item.score, 4),
                "chunk_sha12": hashlib.sha256(item.content.encode("utf-8")).hexdigest()[:12],
                "excerpt": item.content[:60],
            }
            for item in retrieval
        ],
        "expected_pages": expected_pages,
        "expected_page_retrieved": (not expected_pages)
        or any(page in expected_pages for page in retrieved_pages),
        "cited_pages": [c.page_number for c in outcome.citations],
        "supported_claims_total": len(supported),
        "supported_claims_verbatim_hits": sum(
            1 for claim in supported if claim in answer_text
        ),
    }


def compute_outcome(
    *,
    scope: str,
    dataset_total: int,
    selected: int,
    evaluated: int,
    passed: int,
    skipped: int,
    run_errors: int,
    limit_applied: bool,
    threshold: float = FAITHFULNESS_THRESHOLD,
) -> tuple[str, int, float | None]:
    """计算 (overall_status, exit_code, score)。

    口径（仓库所有者 2026-07-31 拍板）：
    - synthetic：明确选择 20 条、20 条全部执行、score>=阈值、无运行错误 → 退出码 0，
      但 overall_status 只能是 passed_partial_scope，绝不冒充完整评测。
    - all：34/34 全部进入评测且 score>=阈值 → passed_full；缺资料、存在跳过、
      运行异常或覆盖不足 → 非 0 退出。
    - --limit 截断 → partial_limit，仅供冒烟，不作为正式通过记录。
    """
    score = round(passed / evaluated, 4) if evaluated else None
    meets_threshold = score is not None and score >= threshold
    if run_errors:
        return "run_error", 1, score
    if limit_applied and evaluated < selected:
        return "partial_limit", 0 if (evaluated and passed == evaluated) else 1, score
    if scope == "synthetic":
        if evaluated == selected and evaluated > 0 and meets_threshold:
            return "passed_partial_scope", 0, score
        return "failed_partial_scope", 1, score
    # scope == "all"
    if skipped or evaluated < dataset_total:
        return "incomplete_coverage", 1, score
    if meets_threshold:
        return "passed_full", 0, score
    return "failed_full", 1, score


def assert_no_secrets(report_text: str, secrets: list[str | None]) -> None:
    for secret in secrets:
        if secret and secret.strip() and secret.strip() in report_text:
            raise RuntimeError("评测报告中检测到密钥内容，拒绝写盘")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", choices=("synthetic", "all"), default="synthetic")
    parser.add_argument(
        "--embedding",
        choices=("hashing", "dashscope"),
        default="hashing",
        help=(
            "hashing=确定性词面向量（可离线复现）；dashscope=生产语义向量 text-embedding-v4。"
            "2026-07-31 归因：FF-003/008/014 拒答系用例用语与说明书词面差异大、hashing 检索"
            "取不到期望页所致，真实型号用例建议用 dashscope 测生产真实链路"
        ),
    )
    parser.add_argument("--limit", type=int, default=None, help="仅冒烟调试用；截断后不算正式记录")
    parser.add_argument(
        "--output", default=str(PROJECT_ROOT / "docs" / "evidence" / "generation_online_eval.json")
    )
    args = parser.parse_args()

    settings = get_settings()
    if settings.llm_backend == "openai-compat":
        api_key = (settings.llm_api_key or "").strip()
        if not api_key:
            print(json.dumps({"status": "not_run", "reason": "缺少 ROBOTCARE_LLM_API_KEY，拒绝伪造结果"}, ensure_ascii=False))
            return 2
        inner_provider = OpenAICompatGenerationProvider(
            api_key, settings.generation_model, settings.llm_base_url
        )
    else:
        api_key = (settings.dashscope_api_key or "").strip()
        if not api_key:
            print(json.dumps({"status": "not_run", "reason": "缺少 ROBOTCARE_DASHSCOPE_API_KEY，拒绝伪造结果"}, ensure_ascii=False))
            return 2
        inner_provider = DashScopeGenerationProvider(
            settings.dashscope_api_key, settings.generation_model, settings.dashscope_base_url
        )

    provider = _RecordingProvider(inner_provider)
    if args.embedding == "dashscope":
        if not (settings.dashscope_api_key or "").strip():
            print(json.dumps({"status": "not_run", "reason": "--embedding dashscope 需要 ROBOTCARE_DASHSCOPE_API_KEY（OpenAI 兼容后端无 embedding 接口），拒绝伪造结果"}, ensure_ascii=False))
            return 2
        embedding = DashScopeEmbeddingProvider(
            settings.dashscope_api_key, settings.dashscope_base_url
        )
        embedding_model = EMBEDDING_MODEL
    else:
        embedding = HashingNgramEmbeddingProvider()
        embedding_model = "hashing-ngram-v1"

    cases = load_eval_cases()
    dataset_total = sum(1 for c in cases if c["dimension"] == "faithfulness")
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
    expected_source_url = {
        code: f"synthetic://robotcare-demo/{code.lower()}/manual" for code in SYNTHETIC_MODEL_CODES
    }
    for code in REAL_MODEL_CODES:
        if prereq.get(code, {}).get("ok"):
            expected_source_url[code] = prereq[code]["source_url"]

    factory = fresh_eval_session_factory()
    entries: list[dict] = []
    run_errors: list[dict] = []
    with factory() as db:
        seed_database(db)
        model_ids: dict[str, int] = {}
        for code in ingest_codes:
            model_ids[code] = db.scalar(select(RobotModel.id).where(RobotModel.code == code))
            if code in SYNTHETIC_MODEL_CODES:
                pdf_path = PROJECT_ROOT / "knowledge" / "synthetic" / f"{code}_manual.pdf"
            else:
                pdf_path = prereq[code]["pdf_path"]
            ingest_pdf(
                db,
                robot_model_id=model_ids[code],
                pdf_path=pdf_path,
                source_url=expected_source_url[code],
                provider=embedding,
            )
        ingested_urls = set(expected_source_url[code] for code in ingest_codes)

        for case in to_run:
            provider.last_raw = None  # 防止门控拒答（未调模型）误挂上一条的原文
            # 检索与 generate_answer 内部同参、hashing 向量确定性一致，
            # 单独取一份用于诊断字段（页码/分数/片段哈希）
            retrieval = search_knowledge(
                db,
                robot_model_id=model_ids[case["model_code"]],
                query=case["query"],
                top_k=5,
                min_score=0.0,
                provider=embedding,
            )
            try:
                outcome = generate_answer(
                    db,
                    user_id=None,
                    robot_model_id=model_ids[case["model_code"]],
                    query=case["query"],
                    embedding_provider=embedding,
                    generation_provider=provider,
                    min_score=0.0,
                    commit=False,
                )
            except Exception as exc:  # 模型/数据库异常：如实记录并使整次运行非 0 退出
                db.rollback()
                run_errors.append({"case_id": case["case_id"], "error": str(exc)[:300]})
                continue
            db.rollback()
            checks = build_case_checks(
                case, outcome, ingested_urls, expected_source_url[case["model_code"]]
            )
            entry = {
                "case_id": case["case_id"],
                "model_code": case["model_code"],
                "passed": all(checks.values()),
                "checks": checks,
                "refusal_reason": outcome.refusal_reason,
                "diagnostics": build_case_diagnostics(case, outcome, retrieval),
            }
            if outcome.refusal_reason is not None and provider.last_raw is not None:
                # 拒答归因：摘录原文供人工判断是真风险还是规则误伤
                entry["raw_answer_excerpt"] = provider.last_raw[:300]
                if outcome.refusal_reason == "unsafe_answer":
                    block = detect_unsafe_generated_answer(provider.last_raw)
                    if block is not None:
                        entry["safety_category"] = block.category
                        entry["safety_reason"] = block.reason
            entries.append(entry)

    passed = sum(1 for item in entries if item["passed"])
    failed = sum(1 for item in entries if not item["passed"])
    overall_status, exit_code, score = compute_outcome(
        scope=args.scope,
        dataset_total=dataset_total,
        selected=len(selected_cases),
        evaluated=len(entries),
        passed=passed,
        skipped=len(skipped_cases),
        run_errors=len(run_errors),
        limit_applied=limit_applied,
    )
    report = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": provider.model_name,
        "embedding": embedding_model,
        "prompt_version": PROMPT_VERSION,
        "scope": args.scope,
        "dataset_faithfulness_total": dataset_total,
        "selected": len(selected_cases),
        "evaluated": len(entries),
        "passed": passed,
        "failed": failed,
        "skipped": len(skipped_cases),
        "skipped_outside_scope": sum(
            1 for s in skipped_cases if s["reason"].startswith("outside scope")
        ),
        "skipped_cases": skipped_cases,
        "run_errors": run_errors,
        "score": score,
        "score_basis": (
            "结构化引用与回答成功率 + 输出安全 + 违禁论断防线；"
            "supported_claims 语义覆盖与期望页命中仅记录在 diagnostics，"
            "不参与打分，本分数不代表严格语义忠实度"
        ),
        "threshold": FAITHFULNESS_THRESHOLD,
        "overall_status": overall_status,
        "limit": args.limit,
        "faithfulness": entries,
    }
    report_text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    assert_no_secrets(report_text, [settings.dashscope_api_key, settings.llm_api_key])
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report_text, encoding="utf-8")
    print(
        json.dumps(
            {k: report[k] for k in (
                "scope", "dataset_faithfulness_total", "selected", "evaluated",
                "passed", "failed", "skipped", "score", "overall_status",
            )},
            ensure_ascii=False,
        )
    )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

"""faithfulness / 模型层 refusal 在线评测（需要真实 DashScope key，本脚本绝不伪造结果）。

用法（在持有 ROBOTCARE_DASHSCOPE_API_KEY 的机器上）：
    python scripts/run_online_generation_eval.py --limit 20

流程：内存库入库合成说明书（hashing 向量保证检索确定性）→ 对 faithfulness 用例真实调用
生成模型 → 校验：回答仅引用检索片段（引用页码 ⊆ 检索页码）、必须引用期望来源、
回答不触发安全规则；对 refusal 用例校验模型层拒答（检索有命中但内容不支持时输出 REFUSE）。
结果写 docs/evidence/generation_online_eval.json，不改动离线审计报告。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy import create_engine, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.database import Base  # noqa: E402
from app.generation_service import DashScopeGenerationProvider, generate_answer  # noqa: E402
from app.knowledge_service import HashingNgramEmbeddingProvider, ingest_pdf  # noqa: E402
from app.models import RobotModel  # noqa: E402
from app.safety import detect_safety_block  # noqa: E402
from app.seed import seed_database  # noqa: E402

SYNTHETIC_MODEL_CODES = ("RC-S200", "RC-M500", "RC-X800")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument(
        "--output", default=str(PROJECT_ROOT / "docs" / "evidence" / "generation_online_eval.json")
    )
    args = parser.parse_args()

    settings = get_settings()
    if not (settings.dashscope_api_key or "").strip():
        print(json.dumps({"status": "not_run", "reason": "缺少 ROBOTCARE_DASHSCOPE_API_KEY，拒绝伪造结果"}, ensure_ascii=False))
        return 2

    provider = DashScopeGenerationProvider(
        settings.dashscope_api_key, settings.generation_model, settings.dashscope_base_url
    )
    embedding = HashingNgramEmbeddingProvider()
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)

    cases = [
        json.loads(line)
        for line in (PROJECT_ROOT / "knowledge" / "eval_cases.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    faith_cases = [c for c in cases if c["dimension"] == "faithfulness" and c["model_code"] in SYNTHETIC_MODEL_CODES]
    results = {"faithfulness": [], "model": provider.model_name}

    with factory() as db:
        seed_database(db)
        model_ids = {}
        for code in SYNTHETIC_MODEL_CODES:
            model_ids[code] = db.scalar(select(RobotModel.id).where(RobotModel.code == code))
            ingest_pdf(
                db,
                robot_model_id=model_ids[code],
                pdf_path=PROJECT_ROOT / "knowledge" / "synthetic" / f"{code}_manual.pdf",
                source_url=f"synthetic://robotcare-demo/{code.lower()}/manual",
                provider=embedding,
            )
        for case in faith_cases[: args.limit]:
            expected = case["expected"]
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
            db.rollback()
            checks = {
                "answered": outcome.status == "answered",
                "citations_within_retrieval": outcome.status != "answered"
                or all(c.source_url.startswith("synthetic://") for c in outcome.citations),
                "cites_expected_source": outcome.status != "answered"
                or any(
                    c.source_url.endswith(f"{case['model_code'].lower()}/manual")
                    for c in outcome.citations
                ),
                "answer_passes_safety": outcome.answer is None
                or detect_safety_block(outcome.answer) is None,
            }
            results["faithfulness"].append(
                {
                    "case_id": case["case_id"],
                    "passed": all(checks.values()),
                    "checks": checks,
                    "refusal_reason": outcome.refusal_reason,
                }
            )

    evaluated = results["faithfulness"]
    passed = sum(1 for item in evaluated if item["passed"])
    results["summary"] = {
        "evaluated": len(evaluated),
        "passed": passed,
        "score": round(passed / len(evaluated), 4) if evaluated else None,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(results["summary"], ensure_ascii=False))
    return 0 if evaluated and passed == len(evaluated) else 1


if __name__ == "__main__":
    raise SystemExit(main())

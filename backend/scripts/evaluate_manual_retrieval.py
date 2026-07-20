from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.config import get_settings
from app.database import build_session_factory
from app.knowledge_service import DashScopeEmbeddingProvider, get_knowledge_status, search_knowledge
from app.models import RobotModel


CASES = [
    {
        "model_code": "JH69U1",
        "query": "机器人找不到基站，无法自动回充",
        "expected_pages": [15, 16],
        "min_score": 0.25,
    },
    {
        "model_code": "JH69U1",
        "query": "清水箱缺水，基站红灯闪烁怎么办",
        "expected_pages": [7, 8, 9, 10, 15, 16],
        "min_score": 0.25,
    },
    {
        "model_code": "VC35U1",
        "query": "手机搜索不到设备，配网失败",
        "expected_pages": [4, 7, 8, 15],
        "min_score": 0.25,
    },
    {
        "model_code": "VC35U1",
        "query": "清扫时有异响，应该检查什么",
        "expected_pages": [11, 13, 15],
        "min_score": 0.25,
    },
    {
        "model_code": "VC35U1",
        "query": "完全无关的烘焙蛋糕配方",
        "expected_pages": [],
        "min_score": 0.9,
    },
]


def run(output: Path) -> dict:
    settings = get_settings()
    provider = DashScopeEmbeddingProvider(settings.dashscope_api_key)
    session_factory = build_session_factory(settings.database_url)
    case_results = []

    with session_factory() as db:
        status_rows = [item.__dict__ for item in get_knowledge_status(db)]
        for case in CASES:
            robot_model = db.scalar(select(RobotModel).where(RobotModel.code == case["model_code"]))
            if robot_model is None:
                raise RuntimeError(f"Missing robot model: {case['model_code']}")

            results = search_knowledge(
                db,
                robot_model_id=robot_model.id,
                query=case["query"],
                top_k=3,
                min_score=case["min_score"],
                provider=provider,
            )
            returned_pages = [item.page_number for item in results]
            expected_pages = case["expected_pages"]
            passed = (
                not results
                if not expected_pages
                else any(page in expected_pages for page in returned_pages)
            )
            case_results.append(
                {
                    **case,
                    "passed": passed,
                    "returned": [
                        {
                            "score": round(item.score, 6),
                            "page_number": item.page_number,
                            "document_title": item.document_title,
                            "source_url": item.source_url,
                        }
                        for item in results
                    ],
                }
            )

    passed_count = sum(1 for item in case_results if item["passed"])
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "embedding_model": "text-embedding-v4",
        "embedding_dimension": 256,
        "knowledge_status": status_rows,
        "total": len(case_results),
        "passed": passed_count,
        "pass_rate": round(passed_count / len(case_results), 4),
        "cases": case_results,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run real manual vector retrieval smoke evaluation")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("../docs/evidence/rag_smoke_20260720.json"),
    )
    args = parser.parse_args()
    report = run(args.output)
    print(
        json.dumps(
            {
                "total": report["total"],
                "passed": report["passed"],
                "pass_rate": report["pass_rate"],
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["passed"] == report["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

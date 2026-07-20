from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from sqlalchemy import select

from .config import get_settings
from .database import Base, build_session_factory
from .knowledge_service import DashScopeEmbeddingProvider, EmbeddingProvider, ingest_pdf
from .models import RobotModel
from .seed import seed_database


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="RobotCare AI knowledge-base tools")
    subparsers = parser.add_subparsers(dest="command", required=True)
    ingest = subparsers.add_parser("ingest", help="Ingest or replace a model-specific PDF")
    ingest.add_argument("--model-code", required=True)
    ingest.add_argument("--pdf", required=True)
    ingest.add_argument("--source-url", required=True)
    ingest.add_argument("--title")
    return parser


def main(argv: Sequence[str] | None = None, provider: EmbeddingProvider | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    session_factory = build_session_factory(settings.database_url)
    engine = session_factory.kw["bind"]
    Base.metadata.create_all(engine)
    try:
        with session_factory() as db:
            seed_database(db)
            robot_model = db.scalar(select(RobotModel).where(RobotModel.code == args.model_code))
            if robot_model is None:
                raise SystemExit(f"Unknown robot model: {args.model_code}")
            result = ingest_pdf(
                db,
                robot_model_id=robot_model.id,
                pdf_path=args.pdf,
                source_url=args.source_url,
                title=args.title,
                provider=provider or DashScopeEmbeddingProvider(settings.dashscope_api_key),
            )
            print(json.dumps(result.__dict__, ensure_ascii=False))
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

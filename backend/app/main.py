from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .routers import router
from .config import get_settings
from .database import Base, build_session_factory
from .generation_service import (
    GenerationProvider,
    build_generation_provider,
    generation_backend_configured,
)
from .knowledge_service import DashScopeEmbeddingProvider, EmbeddingProvider
from .migration_guard import ensure_database_at_head
from .observability import install_observability, readiness_status, request_trace_id
from .rate_limit_service import KnowledgeSearchCache
from .seed import seed_database


def create_app(
    database_url: str | None = None,
    attachment_dir: str | Path | None = None,
    report_dir: str | Path | None = None,
    knowledge_dir: str | Path | None = None,
    embedding_provider: EmbeddingProvider | None = None,
    generation_provider: "GenerationProvider | None" = None,
    auto_create_schema: bool | None = None,
) -> FastAPI:
    settings = get_settings()
    session_factory = build_session_factory(database_url or settings.database_url)
    should_auto_create = settings.auto_create_schema if auto_create_schema is None else auto_create_schema

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = session_factory.kw["bind"]
        if should_auto_create:
            Base.metadata.create_all(engine)
        else:
            ensure_database_at_head(engine)
        with session_factory() as db:
            seed_database(db)
        yield
        engine.dispose()

    application = FastAPI(title="RobotCare AI API", version="0.1.0", lifespan=lifespan)
    application.state.session_factory = session_factory
    application.state.embedding_provider = embedding_provider or DashScopeEmbeddingProvider(
        settings.dashscope_api_key,
        settings.dashscope_base_url,
    )
    application.state.knowledge_search_cache = KnowledgeSearchCache(
        settings.knowledge_search_cache_ttl_seconds
    )
    application.state.generation_provider = generation_provider or build_generation_provider(settings)
    application.state.generation_configured = bool(
        generation_provider is not None or generation_backend_configured(settings)
    )
    application.state.environment = settings.environment.strip().lower()
    application.state.embedding_configured = bool(
        embedding_provider is not None or (settings.dashscope_api_key or "").strip()
    )
    application.state.attachment_dir = Path(attachment_dir or settings.attachment_dir).resolve()
    application.state.attachment_dir.mkdir(parents=True, exist_ok=True)
    application.state.report_dir = Path(report_dir or settings.report_dir).resolve()
    application.state.report_dir.mkdir(parents=True, exist_ok=True)
    # 知识原件存档目录：重新向量化、版本回滚、下载原件都依赖它
    application.state.knowledge_dir = Path(knowledge_dir or settings.knowledge_dir).resolve()
    application.state.knowledge_dir.mkdir(parents=True, exist_ok=True)
    application.state.report_filename_secret = settings.jwt_secret
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    install_observability(application)
    application.include_router(router)

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/ready")
    def ready(request: Request) -> JSONResponse:
        status_code, content = readiness_status(application, request_trace_id(request))
        return JSONResponse(status_code=status_code, content=content)

    return application


app = create_app()

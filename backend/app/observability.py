from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
from collections.abc import Mapping
from contextvars import ContextVar
from pathlib import Path
from time import monotonic, perf_counter
from typing import Any
from uuid import uuid4

from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException

from .migration_guard import alembic_config


TRACE_HEADER = "X-Request-ID"
MAX_TRACE_ID_LENGTH = 128
_SAFE_TRACE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_REDACTED = "[REDACTED]"
_SENSITIVE_KEY_PARTS = (
    "authorization",
    "cookie",
    "password",
    "passwd",
    "passcode",
    "token",
    "secret",
    "wifi",
    "wlan",
    "phone",
    "mobile",
    "email",
    "contact",
    "image",
    "photo",
    "attachment",
    "filecontent",
)

request_logger = logging.getLogger("robotcare.observability")
_trace_context: ContextVar[str | None] = ContextVar("robotcare_trace_id", default=None)
_handler_lock = threading.Lock()
_HANDLER_MARKER = "_robotcare_json_handler"


class _JsonMessageFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return record.getMessage()


def _env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def configure_json_logging() -> None:
    """Configure one process-local JSON sink without duplicating handlers.

    The logger can be disabled with ``ROBOTCARE_JSON_LOGS=false`` and its level
    can be changed with ``ROBOTCARE_LOG_LEVEL``.  Tests may attach an additional
    capture handler directly to ``request_logger``.
    """

    enabled = _env_flag("ROBOTCARE_JSON_LOGS", True)
    level_name = os.getenv("ROBOTCARE_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    request_logger.setLevel(level)
    request_logger.disabled = not enabled
    request_logger.propagate = False
    if not enabled:
        return

    with _handler_lock:
        if any(getattr(handler, _HANDLER_MARKER, False) for handler in request_logger.handlers):
            return
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(_JsonMessageFormatter())
        setattr(handler, _HANDLER_MARKER, True)
        request_logger.addHandler(handler)


def _normalized_key(key: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(key).lower())


def _is_sensitive_key(key: object) -> bool:
    normalized = _normalized_key(key)
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def redact(value: Any) -> Any:
    """Return a recursively redacted, JSON-compatible representation."""

    if isinstance(value, Mapping):
        return {
            str(key): _REDACTED if _is_sensitive_key(key) else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    if isinstance(value, set):
        return [redact(item) for item in value]
    return value


def sanitize_validation_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep FastAPI's error shape while removing echoed inputs and validator context."""

    return [
        {
            str(key): redact(item)
            for key, item in error.items()
            if key not in {"input", "ctx"}
        }
        for error in errors
    ]


def emit_json_log(level: int, event: str, **fields: Any) -> None:
    payload = redact({"event": event, **fields})
    request_logger.log(
        level,
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str),
    )


def safe_trace_id(candidate: str | None) -> str:
    if (
        candidate
        and len(candidate) <= MAX_TRACE_ID_LENGTH
        and _SAFE_TRACE_ID.fullmatch(candidate)
    ):
        return candidate
    return str(uuid4())


def request_trace_id(request: Request) -> str:
    existing = getattr(request.state, "trace_id", None)
    if isinstance(existing, str) and existing:
        return existing
    trace_id = safe_trace_id(None)
    request.state.trace_id = trace_id
    return trace_id


def current_trace_id() -> str | None:
    return _trace_context.get()


def _internal_error_response(request: Request, exc: Exception) -> JSONResponse:
    trace_id = request_trace_id(request)
    emit_json_log(
        logging.ERROR,
        "unhandled_exception",
        trace_id=trace_id,
        exception_type=type(exc).__name__,
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error", "trace_id": trace_id},
        headers={TRACE_HEADER: trace_id},
    )


def install_observability(application: FastAPI) -> None:
    configure_json_logging()

    @application.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        trace_id = request_trace_id(request)
        headers = dict(exc.headers or {})
        headers[TRACE_HEADER] = trace_id
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": redact(jsonable_encoder(exc.detail)), "trace_id": trace_id},
            headers=headers,
        )

    @application.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        trace_id = request_trace_id(request)
        return JSONResponse(
            status_code=422,
            content={
                "detail": jsonable_encoder(sanitize_validation_errors(exc.errors())),
                "trace_id": trace_id,
            },
            headers={TRACE_HEADER: trace_id},
        )

    @application.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        return _internal_error_response(request, exc)

    @application.middleware("http")
    async def request_context_middleware(request: Request, call_next):
        trace_id = safe_trace_id(request.headers.get(TRACE_HEADER))
        request.state.trace_id = trace_id
        context_token = _trace_context.set(trace_id)
        started_at = perf_counter()
        try:
            try:
                response = await call_next(request)
            except Exception as exc:  # Last-resort boundary; never expose exception text.
                response = _internal_error_response(request, exc)

            response.headers[TRACE_HEADER] = trace_id
            emit_json_log(
                logging.INFO,
                "http_request",
                trace_id=trace_id,
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
                duration_ms=round((perf_counter() - started_at) * 1000, 3),
            )
            return response
        finally:
            _trace_context.reset(context_token)


class EmbeddingReachability:
    """带缓存的 embedding 轻量可达性探测。

    /ready 被容器 healthcheck 每 10s 打一次，若每次都真发一次 embedding 调用，
    光探针一天就是近万次付费请求。所以成功结果缓存较久、失败结果缓存较短
    （坏了要尽快恢复感知），并用锁保证同一时刻只有一个探测在飞。
    """

    def __init__(self, success_ttl_seconds: int = 300, failure_ttl_seconds: int = 30):
        self.success_ttl_seconds = success_ttl_seconds
        self.failure_ttl_seconds = failure_ttl_seconds
        self._lock = threading.Lock()
        self._checked_at: float | None = None
        self._reachable = False
        self._error_type: str | None = None

    def _fresh(self, now: float) -> bool:
        if self._checked_at is None:
            return False
        ttl = self.success_ttl_seconds if self._reachable else self.failure_ttl_seconds
        return (now - self._checked_at) < ttl

    def check(self, provider: Any) -> tuple[bool, str | None]:
        now = monotonic()
        with self._lock:
            if self._fresh(now):
                return self._reachable, self._error_type
            try:
                vector = provider.embed_query("ready")
                self._reachable = bool(vector)
                self._error_type = None if self._reachable else "EmptyEmbedding"
            except Exception as exc:  # noqa: BLE001 — 就绪探针不该把异常抛给调用方
                self._reachable = False
                self._error_type = type(exc).__name__
            self._checked_at = monotonic()
            return self._reachable, self._error_type


def _storage_component(path: Path) -> dict[str, Any]:
    exists = path.exists()
    is_directory = path.is_dir() if exists else False
    writable = bool(exists and is_directory and os.access(path, os.W_OK))
    status = "ok" if writable else "error"
    return {
        "status": status,
        "exists": exists,
        "is_directory": is_directory,
        "writable": writable,
    }


def readiness_status(application: FastAPI, trace_id: str) -> tuple[int, dict[str, Any]]:
    components: dict[str, dict[str, Any]] = {}
    session_factory = application.state.session_factory

    try:
        with session_factory() as db:
            db.execute(text("SELECT 1"))
        components["database"] = {"status": "ok"}
    except Exception as exc:
        components["database"] = {
            "status": "error",
            "error_type": type(exc).__name__,
        }

    try:
        expected_revision = ScriptDirectory.from_config(alembic_config()).get_current_head()
        with session_factory() as db:
            current_revision = MigrationContext.configure(db.connection()).get_current_revision()
        at_head = bool(expected_revision and current_revision == expected_revision)
        components["alembic"] = {
            "status": "ok" if at_head else "error",
            "at_head": at_head,
            "current_revision": current_revision,
            "expected_revision": expected_revision,
        }
    except Exception as exc:
        components["alembic"] = {
            "status": "error",
            "at_head": False,
            "error_type": type(exc).__name__,
        }

    components["attachments"] = _storage_component(application.state.attachment_dir)
    components["reports"] = _storage_component(application.state.report_dir)
    # 知识原件目录此前不在就绪检查里：卷没挂上时服务照样报 ready，
    # 直到用户点开"查看原页"才 404 —— 就绪探针的意义就是提前拦住这种半瘫状态。
    components["knowledge"] = _storage_component(application.state.knowledge_dir)
    production = getattr(application.state, "environment", "development") == "production"
    embedding_configured = bool(
        getattr(application.state, "embedding_configured", False)
    )
    embedding_component: dict[str, Any] = {
        "status": "ok" if embedding_configured or not production else "error",
        "configured": embedding_configured,
        "required": production,
    }
    # 配置存在不等于服务可达（key 失效 / DNS / 权限都可能）。做一次**带缓存**的
    # /ready 由健康检查高频调用，外部模型探测结果需要短时缓存以控制成本。
    probe = getattr(application.state, "embedding_reachability", None)
    if probe is not None and embedding_configured:
        reachable, error_type = probe.check(application.state.embedding_provider)
        embedding_component["reachable"] = reachable
        if error_type:
            embedding_component["error_type"] = error_type
        if not reachable and production:
            # 非 production 不因外部模型不可达而 not_ready：本地开发常年没 key
            embedding_component["status"] = "error"
    components["embedding"] = embedding_component
    is_ready = all(component["status"] == "ok" for component in components.values())
    return (
        200 if is_ready else 503,
        {
            "status": "ready" if is_ready else "not_ready",
            "trace_id": trace_id,
            "components": components,
        },
    )

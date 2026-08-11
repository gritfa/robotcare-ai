from __future__ import annotations

import json
import logging
import sys
from io import StringIO
from time import sleep
from types import SimpleNamespace
from uuid import UUID

from fastapi import Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select, text

from app.knowledge_service import DashScopeEmbeddingProvider
from app.main import create_app
from app.models import KnowledgeChunk, KnowledgeDocument, RobotModel
from app.observability import TRACE_HEADER, configure_json_logging, redact, request_logger
from conftest import TEST_DATABASE_URL, auth, register


def _migrated_client(tmp_path, monkeypatch) -> tuple[TestClient, object]:
    # The shared test database is migrated to head once per session by conftest.
    monkeypatch.delenv("ROBOTCARE_DATABASE_URL", raising=False)
    app = create_app(
        TEST_DATABASE_URL,
        attachment_dir=tmp_path / "attachments",
        report_dir=tmp_path / "reports",
        auto_create_schema=False,
    )
    return TestClient(app), app


def test_trace_id_is_propagated_and_unsafe_values_are_replaced(client):
    @client.app.get("/test-trace-state")
    def trace_state(request: Request):
        return {"trace_id": request.state.trace_id}

    supplied = "web-client.request_123"
    response = client.get("/test-trace-state", headers={TRACE_HEADER: supplied})
    assert response.status_code == 200
    assert response.headers[TRACE_HEADER] == supplied
    assert response.json()["trace_id"] == supplied

    for unsafe in ("contains spaces", "x" * 129, "../not-safe"):
        response = client.get("/health", headers={TRACE_HEADER: unsafe})
        replacement = response.headers[TRACE_HEADER]
        assert replacement != unsafe
        UUID(replacement)


def test_http_and_validation_errors_keep_detail_and_add_top_level_trace(client):
    trace_id = "error-request-1"
    not_found = client.get("/missing", headers={TRACE_HEADER: trace_id})
    assert not_found.status_code == 404
    assert not_found.json() == {"detail": "Not Found", "trace_id": trace_id}
    assert not_found.headers[TRACE_HEADER] == trace_id

    invalid = client.post(
        "/api/v1/auth/register",
        headers={TRACE_HEADER: trace_id},
        json={"email": "not-an-email", "password": "leakme!"},
    )
    assert invalid.status_code == 422
    assert isinstance(invalid.json()["detail"], list)
    assert all("input" not in error and "ctx" not in error for error in invalid.json()["detail"])
    assert "not-an-email" not in invalid.text
    assert "leakme!" not in invalid.text
    assert invalid.json()["trace_id"] == trace_id
    assert invalid.headers[TRACE_HEADER] == trace_id


def test_unhandled_exception_is_generic_and_does_not_log_exception_message(tmp_path):
    app = create_app(
        TEST_DATABASE_URL,
        attachment_dir=tmp_path / "attachments",
        report_dir=tmp_path / "reports",
        auto_create_schema=False,
    )

    @app.get("/test-unhandled")
    def test_unhandled():
        raise RuntimeError("password=must-not-appear")

    stream = StringIO()
    capture = logging.StreamHandler(stream)
    capture.setFormatter(logging.Formatter("%(message)s"))
    request_logger.addHandler(capture)
    try:
        with TestClient(app) as test_client:
            response = test_client.get("/test-unhandled", headers={TRACE_HEADER: "error-500"})
    finally:
        request_logger.removeHandler(capture)

    assert response.status_code == 500
    assert response.json() == {
        "detail": "Internal server error",
        "trace_id": "error-500",
    }
    assert response.headers[TRACE_HEADER] == "error-500"
    assert "must-not-appear" not in stream.getvalue()
    assert "RuntimeError" in stream.getvalue()


def test_ready_reports_database_revision_and_storage_success(tmp_path, monkeypatch):
    client, _app = _migrated_client(tmp_path, monkeypatch)
    with client:
        response = client.get("/ready", headers={TRACE_HEADER: "ready-success"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["trace_id"] == "ready-success"
    assert payload["components"]["database"] == {"status": "ok"}
    assert payload["components"]["alembic"]["status"] == "ok"
    assert payload["components"]["alembic"]["at_head"] is True
    assert payload["components"]["attachments"]["writable"] is True
    assert payload["components"]["reports"]["writable"] is True


def test_ready_fails_closed_but_health_remains_live(tmp_path, monkeypatch):
    client, app = _migrated_client(tmp_path, monkeypatch)

    class BrokenSession:
        def __enter__(self):
            raise ConnectionError("database unavailable")

        def __exit__(self, exc_type, exc, traceback):
            return False

    with client:
        app.state.session_factory = lambda: BrokenSession()
        monkeypatch.setattr("app.observability.os.access", lambda path, mode: False)
        response = client.get("/ready", headers={TRACE_HEADER: "ready-failure"})
        health = client.get("/health")

    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "not_ready"
    assert payload["trace_id"] == "ready-failure"
    assert payload["components"]["database"]["status"] == "error"
    assert payload["components"]["alembic"]["status"] == "error"
    assert payload["components"]["attachments"]["writable"] is False
    assert payload["components"]["reports"]["writable"] is False
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}


def test_ready_rejects_an_unversioned_but_reachable_database(
    tmp_path, migration_database_url
):
    # A reachable database whose schema was auto-created without Alembic
    # stamping must fail readiness.
    engine = create_engine(migration_database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    engine.dispose()
    app = create_app(
        migration_database_url,
        attachment_dir=tmp_path / "attachments",
        report_dir=tmp_path / "reports",
        auto_create_schema=True,
    )
    with TestClient(app) as unversioned_client:
        response = unversioned_client.get(
            "/ready", headers={TRACE_HEADER: "unversioned-db"}
        )

    assert response.status_code == 503
    payload = response.json()
    assert payload["trace_id"] == "unversioned-db"
    assert payload["components"]["database"] == {"status": "ok"}
    assert payload["components"]["alembic"]["status"] == "error"
    assert payload["components"]["alembic"]["at_head"] is False


def test_ready_requires_embedding_configuration_in_production(tmp_path, monkeypatch):
    client, app = _migrated_client(tmp_path, monkeypatch)
    app.state.environment = "production"
    app.state.embedding_configured = False

    with client:
        response = client.get("/ready")

    assert response.status_code == 503
    component = response.json()["components"]["embedding"]
    assert component == {"status": "error", "configured": False, "required": True}


def test_redact_recursively_covers_identity_credentials_and_image_content():
    value = {
        "profile": {
            "email": "person@example.com",
            "contact_phone": "13800000000",
            "display_name": "allowed",
        },
        "credentials": [
            {"password": "pw", "access_token": "jwt"},
            {"wifi_ssid": "home", "wifiSecret": "wifi-pw"},
        ],
        "attachment": {"image_content": "base64-data"},
    }

    result = redact(value)
    assert result["profile"] == {
        "email": "[REDACTED]",
        "contact_phone": "[REDACTED]",
        "display_name": "allowed",
    }
    assert result["credentials"][0] == {
        "password": "[REDACTED]",
        "access_token": "[REDACTED]",
    }
    assert result["credentials"][1] == {
        "wifi_ssid": "[REDACTED]",
        "wifiSecret": "[REDACTED]",
    }
    assert result["attachment"] == "[REDACTED]"


def test_request_log_is_json_and_excludes_headers_query_and_body(tmp_path):
    app = create_app(
        TEST_DATABASE_URL,
        attachment_dir=tmp_path / "attachments",
        report_dir=tmp_path / "reports",
        auto_create_schema=False,
    )

    @app.post("/test-log")
    def test_log_route():
        return {"status": "ok"}

    stream = StringIO()
    capture = logging.StreamHandler(stream)
    capture.setFormatter(logging.Formatter("%(message)s"))
    request_logger.addHandler(capture)
    try:
        with TestClient(app) as test_client:
            response = test_client.post(
                "/test-log?token=query-secret",
                headers={
                    TRACE_HEADER: "logging-test",
                    "Authorization": "Bearer header-secret",
                    "Cookie": "session=cookie-secret",
                },
                json={"email": "private@example.com", "password": "BodySecret123"},
            )
    finally:
        request_logger.removeHandler(capture)

    assert response.status_code == 200
    log_lines = [line for line in stream.getvalue().splitlines() if line]
    request_logs = [json.loads(line) for line in log_lines if '"event":"http_request"' in line]
    assert len(request_logs) == 1
    entry = request_logs[0]
    assert entry["event"] == "http_request"
    assert entry["trace_id"] == "logging-test"
    assert entry["method"] == "POST"
    assert entry["path"] == "/test-log"
    assert entry["status_code"] == 200
    assert isinstance(entry["duration_ms"], (int, float))

    complete_log = stream.getvalue()
    for secret in (
        "query-secret",
        "header-secret",
        "cookie-secret",
        "private@example.com",
        "BodySecret123",
    ):
        assert secret not in complete_log


def test_json_logging_configuration_does_not_duplicate_its_handler():
    configure_json_logging()
    configure_json_logging()
    robotcare_handlers = [
        handler
        for handler in request_logger.handlers
        if getattr(handler, "_robotcare_json_handler", False)
    ]
    assert len(robotcare_handlers) == 1


def test_domain_logs_cover_safety_and_diagnostic_transitions_without_user_text(client):
    token = register(client, "domain-log@example.com")["access_token"]
    model_id = next(
        item["id"] for item in client.get("/api/v1/models").json() if item["code"] == "JH69U1"
    )
    device_id = client.post(
        "/api/v1/devices",
        headers=auth(token),
        json={"robot_model_id": model_id, "nickname": "日志测试设备"},
    ).json()["id"]

    stream = StringIO()
    capture = logging.StreamHandler(stream)
    capture.setFormatter(logging.Formatter("%(message)s"))
    request_logger.addHandler(capture)
    try:
        dangerous_text = "机器正在冒烟，private-user-text"
        blocked = client.post(
            "/api/v1/diagnostics",
            headers={**auth(token), TRACE_HEADER: "safety-domain-log"},
            json={
                "device_id": device_id,
                "issue_category_code": "return_to_dock_failure",
                "issue_description": dangerous_text,
            },
        )
        assert blocked.status_code == 422

        private_description = "无法回充，private-diagnostic-description"
        created = client.post(
            "/api/v1/diagnostics",
            headers={**auth(token), TRACE_HEADER: "diagnostic-domain-log"},
            json={
                "device_id": device_id,
                "issue_category_code": "return_to_dock_failure",
                "issue_description": private_description,
            },
        )
        assert created.status_code == 201, created.text
        diagnostic_id = created.json()["id"]
        step = client.get(
            f"/api/v1/diagnostics/{diagnostic_id}/steps/current", headers=auth(token)
        ).json()
        finished = client.post(
            f"/api/v1/diagnostics/{diagnostic_id}/feedback",
            headers={**auth(token), TRACE_HEADER: "feedback-domain-log"},
            json={"step_id": step["id"], "outcome": "resolved"},
        )
        assert finished.status_code == 200, finished.text
    finally:
        request_logger.removeHandler(capture)

    entries = [json.loads(line) for line in stream.getvalue().splitlines() if line]
    by_event = {entry["event"]: entry for entry in entries if entry["event"] != "http_request"}
    assert by_event["safety_block"]["category"] == "smoke"
    assert by_event["safety_block"]["trace_id"] == "safety-domain-log"
    assert by_event["diagnostic_created"]["diagnostic_id"] == diagnostic_id
    assert by_event["diagnostic_state_changed"]["status"] == "resolved"
    assert by_event["diagnostic_state_changed"]["trace_id"] == "feedback-domain-log"
    assert dangerous_text not in stream.getvalue()
    assert private_description not in stream.getvalue()


def test_knowledge_log_records_version_source_score_and_timing_without_query_or_content(client):
    token = register(client, "knowledge-log@example.com")["access_token"]

    class FakeProvider:
        def embed_query(self, text):
            del text
            return [1.0, *([0.0] * 255)]

    client.app.state.embedding_provider = FakeProvider()
    with client.app.state.session_factory() as db:
        model_id = db.scalar(select(RobotModel.id).where(RobotModel.code == "JH69U1"))
        document = KnowledgeDocument(
            robot_model_id=model_id,
            title="JH69U1 official manual",
            source_url="https://example.com/manual.pdf",
            sha256="d" * 64,
            page_count=1,
        )
        db.add(document)
        db.flush()
        db.add(
            KnowledgeChunk(
                document_id=document.id,
                chunk_index=0,
                page_number=7,
                content="private-retrieved-content",
                embedding=json.dumps([1.0, *([0.0] * 255)]),
            )
        )
        db.commit()

    stream = StringIO()
    capture = logging.StreamHandler(stream)
    capture.setFormatter(logging.Formatter("%(message)s"))
    request_logger.addHandler(capture)
    try:
        private_query = "private-knowledge-query"
        response = client.post(
            "/api/v1/knowledge/search",
            headers={**auth(token), TRACE_HEADER: "knowledge-domain-log"},
            json={"robot_model_id": model_id, "query": private_query, "top_k": 3},
        )
        assert response.status_code == 200, response.text
    finally:
        request_logger.removeHandler(capture)

    entries = [json.loads(line) for line in stream.getvalue().splitlines() if line]
    entry = next(item for item in entries if item["event"] == "knowledge_search")
    assert entry["trace_id"] == "knowledge-domain-log"
    assert entry["result_count"] == 1
    assert entry["sources"] == [
        {
            "document_title": "JH69U1 official manual",
            "document_sha256": "d" * 64,
            "page_number": 7,
            "score": 1.0,
        }
    ]
    assert isinstance(entry["duration_ms"], (int, float))
    assert private_query not in stream.getvalue()
    assert "private-retrieved-content" not in stream.getvalue()


def test_embedding_log_records_model_latency_and_failure_without_input(monkeypatch):
    class FakeTextEmbedding:
        @staticmethod
        def call(**kwargs):
            return SimpleNamespace(
                status_code=200,
                output={
                    "embeddings": [
                        {"text_index": index, "embedding": [1.0, *([0.0] * 255)]}
                        for index, _text in enumerate(kwargs["input"])
                    ]
                },
            )

    dashscope_module = SimpleNamespace(TextEmbedding=FakeTextEmbedding, base_http_api_url=None)
    monkeypatch.setitem(sys.modules, "dashscope", dashscope_module)
    stream = StringIO()
    capture = logging.StreamHandler(stream)
    capture.setFormatter(logging.Formatter("%(message)s"))
    request_logger.addHandler(capture)
    try:
        vectors = DashScopeEmbeddingProvider(
            api_key="private-api-key",
            base_url="https://workspace.cn-beijing.maas.aliyuncs.com/api/v1/",
        ).embed_documents(["private-embedding-input"])
    finally:
        request_logger.removeHandler(capture)

    assert len(vectors) == 1
    assert (
        dashscope_module.base_http_api_url
        == "https://workspace.cn-beijing.maas.aliyuncs.com/api/v1"
    )
    entry = next(
        json.loads(line)
        for line in stream.getvalue().splitlines()
        if '"event":"embedding_call"' in line
    )
    assert entry["model"] == "text-embedding-v4"
    assert entry["item_count"] == 1
    assert entry["dimension"] == 256
    assert entry["outcome"] == "success"
    assert isinstance(entry["duration_ms"], (int, float))
    assert "private-embedding-input" not in stream.getvalue()
    assert "private-api-key" not in stream.getvalue()


def test_ready_checks_knowledge_dir(tmp_path, monkeypatch):
    """D6：知识原件目录没挂上时服务照样报 ready，直到用户点"查看原页"才 404。"""
    client, app = _migrated_client(tmp_path, monkeypatch)
    with client:
        healthy = client.get("/ready")
        assert healthy.status_code == 200
        assert healthy.json()["components"]["knowledge"]["writable"] is True

        # 目录消失（卷没挂上的等价状态）
        app.state.knowledge_dir = tmp_path / "knowledge-not-mounted"
        broken = client.get("/ready")

    assert broken.status_code == 503
    assert broken.json()["components"]["knowledge"]["status"] == "error"
    assert broken.json()["components"]["knowledge"]["exists"] is False


def test_ready_probes_embedding_reachability_in_production(tmp_path, monkeypatch):
    """D6：配置存在不等于可达，key 失效时 /ready 必须 not_ready。"""
    from app.observability import EmbeddingReachability

    class DeadProvider:
        def embed_query(self, text):
            raise ConnectionError("dns failure")

    client, app = _migrated_client(tmp_path, monkeypatch)
    app.state.environment = "production"
    app.state.embedding_configured = True
    app.state.embedding_provider = DeadProvider()
    app.state.embedding_reachability = EmbeddingReachability()

    with client:
        response = client.get("/ready")

    assert response.status_code == 503
    component = response.json()["components"]["embedding"]
    assert component["status"] == "error"
    assert component["configured"] is True
    assert component["reachable"] is False
    assert component["error_type"] == "ConnectionError"


def test_embedding_reachability_caches_result(tmp_path, monkeypatch):
    """/ready 的高频健康检查应复用短时缓存，避免重复调用外部模型。"""
    from app.observability import EmbeddingReachability

    class CountingProvider:
        def __init__(self):
            self.calls = 0

        def embed_query(self, text):
            self.calls += 1
            return [0.1, 0.2]

    provider = CountingProvider()
    probe = EmbeddingReachability(success_ttl_seconds=300, failure_ttl_seconds=30)
    for _ in range(5):
        assert probe.check(provider) == (True, None)
    assert provider.calls == 1

    # 失败结果缓存更短：坏了要尽快恢复感知
    fast = EmbeddingReachability(success_ttl_seconds=300, failure_ttl_seconds=0.01)

    class FlakyProvider:
        def __init__(self):
            self.calls = 0

        def embed_query(self, text):
            self.calls += 1
            raise TimeoutError("slow")

    flaky = FlakyProvider()
    assert fast.check(flaky) == (False, "TimeoutError")
    sleep(0.05)
    fast.check(flaky)
    assert flaky.calls == 2

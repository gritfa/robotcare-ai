"""智能客服多轮会话：建会话/列表/恢复/多轮引用/拒答留痕/越权隔离/安全拦截/SSE 流式。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from sqlalchemy import select

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
sys.path.insert(0, str(BACKEND_ROOT))

from app.config import Settings
from app.knowledge_service import HashingNgramEmbeddingProvider, ingest_pdf
from app.models import RobotModel
from conftest import auth, register, submit_feedback

SYNTHETIC_DIR = PROJECT_ROOT / "knowledge" / "synthetic"


class ScriptedGenerationProvider:
    model_name = "scripted-test-model"

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.prompts: list[str] = []

    def generate(self, *, system: str, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.outputs.pop(0)


def _setup(client, outputs, monkeypatch):
    # hashing 向量得分低于默认阈值 0.25，与现有端点测试同姿势归零
    monkeypatch.setattr(
        "app.routers.conversations.get_settings",
        lambda: Settings(knowledge_min_score=0.0, _env_file=None),
    )
    with client.app.state.session_factory() as db:
        model_id = db.scalar(select(RobotModel.id).where(RobotModel.code == "RC-S200"))
        ingest_pdf(
            db,
            robot_model_id=model_id,
            pdf_path=SYNTHETIC_DIR / "RC-S200_manual.pdf",
            source_url="synthetic://robotcare-demo/rc-s200/manual",
            provider=HashingNgramEmbeddingProvider(),
        )
    provider = ScriptedGenerationProvider(outputs)
    client.app.state.embedding_provider = HashingNgramEmbeddingProvider()
    client.app.state.generation_provider = provider
    token = register(client, "chat-user@gritfa-test.com")["access_token"]
    return model_id, provider, token


def _create_conversation(client, token, model_id) -> int:
    resp = client.post(
        "/api/v1/conversations", json={"robot_model_id": model_id}, headers=auth(token)
    )
    assert resp.status_code == 201
    return resp.json()["id"]


def test_multi_turn_chat_with_citations_and_history(client, monkeypatch):
    model_id, provider, token = _setup(
        client,
        monkeypatch=monkeypatch,
        outputs=["先清空尘盒并清理滤网 [1]。", "按说明书步骤重新安装滤网即可 [1]。"],
    )
    conversation_id = _create_conversation(client, token, model_id)

    r1 = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"content": "吸力变小了怎么办"},
        headers=auth(token),
    )
    assert r1.status_code == 200
    body = r1.json()
    assert body["assistant_message"]["citations"], "回答必须带引用页码"
    assert body["assistant_message"]["citations"][0]["page_number"] >= 1

    r2 = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"content": "那第二步怎么做"},
        headers=auth(token),
    )
    assert r2.status_code == 200
    # 第二轮 prompt 必须携带此前对话语境
    assert "此前对话" in provider.prompts[1]
    assert "吸力变小了怎么办" in provider.prompts[1]

    # 会话标题取自首问；列表按更新时间排序；详情可恢复完整历史
    listing = client.get("/api/v1/conversations", headers=auth(token)).json()
    assert listing[0]["id"] == conversation_id
    assert listing[0]["title"] == "吸力变小了怎么办"
    detail = client.get(
        f"/api/v1/conversations/{conversation_id}", headers=auth(token)
    ).json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant", "user", "assistant"]
    assert detail["robot_model_code"] == "RC-S200"


def test_refusal_recorded_as_visible_assistant_message(client, monkeypatch):
    model_id, provider, token = _setup(client, ["REFUSE"], monkeypatch)
    conversation_id = _create_conversation(client, token, model_id)
    resp = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"content": "吸力变小了怎么办"},
        headers=auth(token),
    )
    assert resp.status_code == 200
    assistant = resp.json()["assistant_message"]
    assert assistant["refusal_reason"] == "model_refused"
    assert "官方售后" in assistant["content"]
    assert assistant["citations"] == []


def test_dangerous_question_blocked_before_generation(client, monkeypatch):
    model_id, provider, token = _setup(client, [], monkeypatch)
    conversation_id = _create_conversation(client, token, model_id)
    resp = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"content": "机器人冒烟了，教我拆机看看里面"},
        headers=auth(token),
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "SAFETY_BLOCKED"
    assert provider.prompts == []  # 未触达生成模型
    # 被拦截的消息不落会话历史
    detail = client.get(
        f"/api/v1/conversations/{conversation_id}", headers=auth(token)
    ).json()
    assert detail["messages"] == []


def test_conversation_is_owner_isolated(client, monkeypatch):
    model_id, provider, token = _setup(client, [], monkeypatch)
    conversation_id = _create_conversation(client, token, model_id)
    other_token = register(client, "chat-intruder@gritfa-test.com")["access_token"]
    assert (
        client.get(
            f"/api/v1/conversations/{conversation_id}", headers=auth(other_token)
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={"content": "偷看一下"},
            headers=auth(other_token),
        ).status_code
        == 404
    )
    assert client.get("/api/v1/conversations", headers=auth(other_token)).json() == []


def test_create_conversation_rejects_unknown_model(client, monkeypatch):
    _, _, token = _setup(client, [], monkeypatch)
    resp = client.post(
        "/api/v1/conversations", json={"robot_model_id": 99999}, headers=auth(token)
    )
    assert resp.status_code == 404


def _sse_events(body: str) -> list[tuple[str, dict]]:
    events = []
    for block in body.strip().split("\n\n"):
        lines = block.split("\n")
        assert lines[0].startswith("event: ") and lines[1].startswith("data: ")
        events.append((lines[0][len("event: "):], json.loads(lines[1][len("data: "):])))
    return events


def test_stream_answer_emits_validated_chunks_then_final_message(client, monkeypatch):
    model_id, provider, token = _setup(
        client, ["先清空尘盒并清理滤网 [1]。", ], monkeypatch
    )
    conversation_id = _create_conversation(client, token, model_id)
    resp = client.post(
        f"/api/v1/conversations/{conversation_id}/messages/stream",
        json={"content": "吸力变小了怎么办"},
        headers=auth(token),
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _sse_events(resp.text)
    names = [name for name, _ in events]
    assert names[:2] == ["user_message", "stage"]
    assert names[-2:] == ["assistant_message", "done"]
    final = next(data for name, data in events if name == "assistant_message")
    deltas = "".join(data["text"] for name, data in events if name == "delta")
    # delta 拼接必须与最终已校验消息完全一致，且带引用页码
    assert deltas == final["content"]
    assert final["citations"] and final["citations"][0]["page_number"] >= 1
    detail = client.get(
        f"/api/v1/conversations/{conversation_id}", headers=auth(token)
    ).json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]


def test_stream_refusal_has_no_delta_and_records_reason(client, monkeypatch):
    model_id, provider, token = _setup(client, ["REFUSE"], monkeypatch)
    conversation_id = _create_conversation(client, token, model_id)
    resp = client.post(
        f"/api/v1/conversations/{conversation_id}/messages/stream",
        json={"content": "吸力变小了怎么办"},
        headers=auth(token),
    )
    events = _sse_events(resp.text)
    assert not [name for name, _ in events if name == "delta"]
    final = next(data for name, data in events if name == "assistant_message")
    assert final["refusal_reason"] == "model_refused"
    assert "官方售后" in final["content"]


def test_stream_safety_block_returns_http_error_not_stream(client, monkeypatch):
    model_id, provider, token = _setup(client, [], monkeypatch)
    conversation_id = _create_conversation(client, token, model_id)
    resp = client.post(
        f"/api/v1/conversations/{conversation_id}/messages/stream",
        json={"content": "机器人冒烟了，教我拆机看看里面"},
        headers=auth(token),
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "SAFETY_BLOCKED"
    assert provider.prompts == []
    detail = client.get(
        f"/api/v1/conversations/{conversation_id}", headers=auth(token)
    ).json()
    assert detail["messages"] == []


class ExplodingGenerationProvider:
    model_name = "exploding-test-model"

    def generate(self, *, system: str, prompt: str) -> str:
        raise RuntimeError("provider down")


def _add_device(client, token, model_id, nickname="客厅机器人"):
    resp = client.post(
        "/api/v1/devices",
        headers=auth(token),
        json={"robot_model_id": model_id, "nickname": nickname},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_conversation_escalates_to_diagnostic_and_report_includes_summary(
    client, monkeypatch
):
    model_id, provider, token = _setup(
        client, ["先清空尘盒并清理滤网 [1]。"], monkeypatch
    )
    conversation_id = _create_conversation(client, token, model_id)
    assert (
        client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={"content": "吸力变小了怎么办"},
            headers=auth(token),
        ).status_code
        == 200
    )

    device_id = _add_device(client, token, model_id)
    created = client.post(
        "/api/v1/diagnostics",
        headers=auth(token),
        json={
            "device_id": device_id,
            "issue_category_code": "suction_drop",
            "issue_description": "吸力变小，按客服建议清理后仍未解决",
            "source_conversation_id": conversation_id,
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["source_conversation_id"] == conversation_id
    diagnostic_id = created.json()["id"]

    for _ in range(3):
        assert submit_feedback(client, token, diagnostic_id, "not_resolved").status_code == 200

    report = client.post(
        f"/api/v1/diagnostics/{diagnostic_id}/report", headers=auth(token)
    )
    assert report.status_code == 201, report.text
    content = report.json()["content"]
    assert "此前智能客服会话摘要" in content
    assert "[用户] 吸力变小了怎么办" in content
    assert "[AI] 先清空尘盒并清理滤网" in content
    assert "引用说明书第" in content


def test_escalation_rejects_foreign_or_mismatched_conversation(client, monkeypatch):
    model_id, provider, token = _setup(client, [], monkeypatch)
    conversation_id = _create_conversation(client, token, model_id)

    # 他人会话 → 404，不泄露存在性
    other_token = register(client, "chat-escalate-intruder@gritfa-test.com")["access_token"]
    other_device_id = _add_device(client, other_token, model_id, nickname="入侵者设备")
    resp = client.post(
        "/api/v1/diagnostics",
        headers=auth(other_token),
        json={
            "device_id": other_device_id,
            "issue_category_code": "suction_drop",
            "issue_description": "试图挂他人会话",
            "source_conversation_id": conversation_id,
        },
    )
    assert resp.status_code == 404

    # 会话型号与设备型号不一致 → 409
    with client.app.state.session_factory() as db:
        other_model_id = db.scalar(select(RobotModel.id).where(RobotModel.code == "JH69U1"))
    mismatched_device_id = _add_device(client, token, other_model_id, nickname="别的型号")
    resp = client.post(
        "/api/v1/diagnostics",
        headers=auth(token),
        json={
            "device_id": mismatched_device_id,
            "issue_category_code": "suction_drop",
            "issue_description": "型号不匹配的转诊断",
            "source_conversation_id": conversation_id,
        },
    )
    assert resp.status_code == 409


def test_stream_generation_failure_emits_error_event_and_rolls_back(client, monkeypatch):
    model_id, provider, token = _setup(client, [], monkeypatch)
    client.app.state.generation_provider = ExplodingGenerationProvider()
    conversation_id = _create_conversation(client, token, model_id)
    resp = client.post(
        f"/api/v1/conversations/{conversation_id}/messages/stream",
        json={"content": "吸力变小了怎么办"},
        headers=auth(token),
    )
    assert resp.status_code == 200
    events = _sse_events(resp.text)
    assert events[-1][0] == "error"
    assert events[-1][1]["code"] == "GENERATION_UNAVAILABLE"
    # 与同步端点 503 语义一致：用户消息随事务回滚，不留半截历史
    detail = client.get(
        f"/api/v1/conversations/{conversation_id}", headers=auth(token)
    ).json()
    assert detail["messages"] == []

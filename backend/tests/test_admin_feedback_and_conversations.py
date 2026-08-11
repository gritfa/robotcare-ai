"""运营可见性：反馈能读、客诉能查到那次对话。

问题背景：
- MessageFeedback 的 reason 枚举注释写着"才能在管理端按原因聚合出优化优先级"，
  但 admin.py / knowledge_admin.py / AdminView.vue 一次都没引用过它——只能连 psql。
- GenerationRecord 无 admin list/detail，conversations 路由全按 user.id 过滤且
  无 admin 变体 → 客诉"机器人让我拆电池"在后台根本找不到那次对话。
"""

from __future__ import annotations

from sqlalchemy import select

from app.models import Conversation, ConversationMessage, MessageFeedback, RobotModel, User
from conftest import auth, register
from test_admin_api import make_admin


def _seed_conversation(client, email: str, *, answer: str, model_code: str = "JH69U1"):
    """直接建一轮会话数据：这里测的是读取入口，不是生成链路。"""
    token = register(client, email)["access_token"]
    with client.app.state.session_factory() as db:
        user = db.scalar(select(User).where(User.email == email))
        model = db.scalar(select(RobotModel).where(RobotModel.code == model_code))
        conversation = Conversation(user_id=user.id, robot_model_id=model.id, title="回充失败")
        db.add(conversation)
        db.flush()
        question = ConversationMessage(
            conversation_id=conversation.id, role="user", content="回充一直失败"
        )
        reply = ConversationMessage(
            conversation_id=conversation.id, role="assistant", content=answer
        )
        db.add_all([question, reply])
        db.flush()
        db.add(
            MessageFeedback(
                message_id=reply.id,
                user_id=user.id,
                helpful=False,
                reason="unclear_steps",
            )
        )
        db.commit()
        return token, conversation.id, reply.id


def test_feedback_aggregates_by_reason_and_model(client):
    _seed_conversation(client, "fb-user1@example.com", answer="请按第 3 页步骤操作 [1]。")
    admin_token = make_admin(client, "fb-admin@example.com")

    response = client.get("/api/v1/admin/feedback", headers=auth(admin_token))

    assert response.status_code == 200
    body = response.json()
    assert body["total_count"] == 1
    assert body["unhelpful_count"] == 1
    assert body["by_reason"]["unclear_steps"] == 1
    assert body["by_model"]["JH69U1"] == 1


def test_feedback_items_link_back_to_the_conversation(client):
    """从一条差评必须能直接跳到那次对话——否则聚合数字没有落点。"""
    _token, conversation_id, message_id = _seed_conversation(
        client, "fb-user2@example.com", answer="建议联系官方售后。"
    )
    admin_token = make_admin(client, "fb-admin2@example.com")

    body = client.get("/api/v1/admin/feedback", headers=auth(admin_token)).json()

    assert body["items"], "差评明细不能为空"
    item = body["items"][0]
    assert item["conversation_id"] == conversation_id
    assert item["message_id"] == message_id
    assert item["answer_excerpt"]
    # 明细不该带用户身份
    assert "email" not in item and "user_id" not in item


def test_feedback_can_filter_by_reason(client):
    _seed_conversation(client, "fb-user3@example.com", answer="步骤不清楚的回答")
    admin_token = make_admin(client, "fb-admin3@example.com")

    matched = client.get(
        "/api/v1/admin/feedback?reason=unclear_steps", headers=auth(admin_token)
    ).json()
    other = client.get(
        "/api/v1/admin/feedback?reason=wrong_model", headers=auth(admin_token)
    ).json()

    assert len(matched["items"]) == 1
    assert other["items"] == []


def test_non_admin_cannot_read_feedback(client):
    token = register(client, "fb-plain@example.com")["access_token"]

    assert client.get("/api/v1/admin/feedback", headers=auth(token)).status_code == 403


def test_admin_can_find_conversation_by_answer_text(client):
    """客诉场景：用户转述的是回答里的一句话，不是会话标题。"""
    _token, conversation_id, _message_id = _seed_conversation(
        client, "conv-user1@example.com", answer="请先拆下电池仓盖板再检查触点。"
    )
    admin_token = make_admin(client, "conv-admin@example.com")

    found = client.get(
        "/api/v1/admin/conversations?search=拆下电池", headers=auth(admin_token)
    ).json()

    assert [item["id"] for item in found] == [conversation_id]
    assert found[0]["message_count"] == 2
    # 列表只给掩码邮箱
    assert "***" in found[0]["user_email_masked"]


def test_admin_conversation_list_filters_by_model(client):
    _seed_conversation(client, "conv-user2@example.com", answer="型号过滤用例")
    admin_token = make_admin(client, "conv-admin2@example.com")

    matched = client.get(
        "/api/v1/admin/conversations?model_code=JH69U1", headers=auth(admin_token)
    ).json()
    other = client.get(
        "/api/v1/admin/conversations?model_code=VC35U1", headers=auth(admin_token)
    ).json()

    assert len(matched) == 1
    assert other == []


def test_admin_conversation_detail_returns_full_transcript(client):
    _token, conversation_id, _message_id = _seed_conversation(
        client, "conv-user3@example.com", answer="完整对话可读性用例"
    )
    admin_token = make_admin(client, "conv-admin3@example.com")

    detail = client.get(
        f"/api/v1/admin/conversations/{conversation_id}", headers=auth(admin_token)
    )

    assert detail.status_code == 200
    body = detail.json()
    assert [m["role"] for m in body["messages"]] == ["user", "assistant"]
    assert body["messages"][1]["content"] == "完整对话可读性用例"
    assert "***" in body["user_email_masked"]


def test_reading_someone_elses_conversation_is_audited(client):
    """管理员读他人对话必须留痕，沿用既有 fail-closed 审计。"""
    _token, conversation_id, _message_id = _seed_conversation(
        client, "conv-user4@example.com", answer="审计用例"
    )
    admin_token = make_admin(client, "conv-admin4@example.com")

    client.get(f"/api/v1/admin/conversations/{conversation_id}", headers=auth(admin_token))
    audits = client.get("/api/v1/admin/audit-logs", headers=auth(admin_token)).json()

    reads = [item for item in audits if item["action"] == "conversation.detail_read"]
    assert reads, "读他人对话必须留审计"
    assert reads[0]["resource_id"] == str(conversation_id)


def test_non_admin_cannot_read_conversations_endpoint(client):
    token = register(client, "conv-plain@example.com")["access_token"]

    assert client.get("/api/v1/admin/conversations", headers=auth(token)).status_code == 403
    assert client.get("/api/v1/admin/conversations/1", headers=auth(token)).status_code == 403


def test_missing_conversation_returns_404(client):
    admin_token = make_admin(client, "conv-admin5@example.com")

    assert client.get(
        "/api/v1/admin/conversations/999999", headers=auth(admin_token)
    ).status_code == 404

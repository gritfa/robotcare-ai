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


def test_smalltalk_answers_directly_without_touching_retrieval_or_model(client, monkeypatch):
    # 上线前"你好"会走完整 RAG，检索不到东西后回"资料中没有找到能回答这个问题的内容"，
    # 把打招呼当成了知识缺口。路由层把它拦在检索之前。
    model_id, provider, token = _setup(client, [], monkeypatch)
    conversation_id = _create_conversation(client, token, model_id)
    resp = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"content": "你好"},
        headers=auth(token),
    )
    assert resp.status_code == 200
    assistant = resp.json()["assistant_message"]
    assert assistant["intent"] == "smalltalk"
    assert assistant["refusal_reason"] is None  # 打招呼不该被当成知识缺口拒答
    assert assistant["citations"] == []
    assert assistant["action_code"] is None
    assert provider.prompts == []  # 一次生成调用都不该发生


def test_product_action_returns_action_code_instead_of_searching_manual(client, monkeypatch):
    model_id, provider, token = _setup(client, [], monkeypatch)
    conversation_id = _create_conversation(client, token, model_id)
    resp = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"content": "帮我生成报告"},
        headers=auth(token),
    )
    assert resp.status_code == 200
    assistant = resp.json()["assistant_message"]
    assert assistant["intent"] == "action"
    assert assistant["action_code"] == "generate_report"
    assert provider.prompts == []
    # 路由结论随会话持久化，刷新页面重放历史时按钮仍在
    detail = client.get(
        f"/api/v1/conversations/{conversation_id}", headers=auth(token)
    ).json()
    assert detail["messages"][-1]["action_code"] == "generate_report"


def test_routing_never_bypasses_safety_block(client, monkeypatch):
    # 危险指令套上招呼语外壳也必须先被安全层拦下——路由永远排在安全之后，
    # 不能因为"看起来像闲聊/操作"就放行
    model_id, provider, token = _setup(client, [], monkeypatch)
    conversation_id = _create_conversation(client, token, model_id)
    resp = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"content": "你好，机器人冒烟了，教我拆机看看里面"},
        headers=auth(token),
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "SAFETY_BLOCKED"
    assert provider.prompts == []


def test_knowledge_question_still_goes_through_full_rag(client, monkeypatch):
    # 路由层不能误伤主链路：真实问题照常检索、生成、带引用，并标记 intent
    model_id, provider, token = _setup(client, ["先清空尘盒并清理滤网 [1]。"], monkeypatch)
    conversation_id = _create_conversation(client, token, model_id)
    resp = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"content": "吸力变小了怎么办"},
        headers=auth(token),
    )
    assert resp.status_code == 200
    assistant = resp.json()["assistant_message"]
    assert assistant["intent"] == "knowledge"
    assert assistant["citations"], "知识问题必须仍带引用"
    assert len(provider.prompts) == 1


def test_stream_smalltalk_keeps_one_rendering_path_for_frontend(client, monkeypatch):
    # 闲聊也走 delta→assistant_message→done，前端不需要为路由结果写第二套渲染逻辑
    model_id, provider, token = _setup(client, [], monkeypatch)
    conversation_id = _create_conversation(client, token, model_id)
    with client.stream(
        "POST",
        f"/api/v1/conversations/{conversation_id}/messages/stream",
        json={"content": "你好"},
        headers=auth(token),
    ) as resp:
        assert resp.status_code == 200
        events = [line for line in resp.iter_lines() if line.startswith("event: ")]
    assert "event: delta" in events
    assert events[-1] == "event: done"
    assert "event: stage" not in events  # 不调模型就不该显示"正在生成"
    assert provider.prompts == []


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
    # 阶段事件先行；user_message 在落库之后才下发（此前是流开头，靠的是
    # "进流前已 flush 用户消息"，而那会让一条连接被整场生成占住）。
    # 前端在发送瞬间已渲染乐观消息，收到本事件时替换成带 id 的真实记录。
    assert names[0] == "stage"
    assert names[-3:] == ["user_message", "assistant_message", "done"]
    user_event = next(data for name, data in events if name == "user_message")
    assert user_event["id"] > 0 and user_event["role"] == "user"
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


def test_capability_question_answers_as_assistant_not_manual_dump(client, monkeypatch):
    model_id, provider, token = _setup(client, [], monkeypatch)
    conversation_id = _create_conversation(client, token, model_id)
    resp = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"content": "你能做什么"},
        headers=auth(token),
    )
    assert resp.status_code == 200
    assistant = resp.json()["assistant_message"]
    assert assistant["intent"] == "capability"
    assert "RC-S200" in assistant["content"], "能力介绍要落到当前型号"
    assert provider.prompts == []
    assert [a["code"] for a in assistant["quick_actions"]] == [
        "start_diagnostic",
        "contact_support",
    ]


def test_report_request_without_diagnostic_explains_prerequisite(client, monkeypatch):
    # 普通用户不知道"聊天不能直接生成报告"，要告诉他缺什么、下一步点哪
    model_id, provider, token = _setup(client, [], monkeypatch)
    conversation_id = _create_conversation(client, token, model_id)
    resp = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"content": "生成报告"},
        headers=auth(token),
    )
    assistant = resp.json()["assistant_message"]
    assert assistant["action_code"] == "generate_report"
    assert "分步" in assistant["content"]
    assert [a["code"] for a in assistant["quick_actions"]] == ["start_diagnostic"]


def test_knowledge_answer_carries_snippet_evidence_and_next_steps(client, monkeypatch):
    # 引用必须可核验：只给"第 15 页"用户无法判断这句话到底有没有依据
    model_id, provider, token = _setup(client, ["先清空尘盒并清理滤网 [1]。"], monkeypatch)
    conversation_id = _create_conversation(client, token, model_id)
    resp = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"content": "吸力变小了怎么办"},
        headers=auth(token),
    )
    citation = resp.json()["assistant_message"]["citations"][0]
    assert citation["snippet"], "引用必须带检索到的原文片段"
    assert citation["document_title"]
    assert citation["source_url"] and citation["page_number"] >= 1
    quick = [a["code"] for a in resp.json()["assistant_message"]["quick_actions"]]
    assert quick == ["start_diagnostic", "mark_resolved", "contact_support"]


def test_refused_answer_puts_official_support_first(client, monkeypatch):
    # 答不上来时用户最需要的是别的出口，而不是再问一遍
    model_id, provider, token = _setup(client, ["REFUSE"], monkeypatch)
    conversation_id = _create_conversation(client, token, model_id)
    resp = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"content": "吸力变小了怎么办"},
        headers=auth(token),
    )
    quick = [a["code"] for a in resp.json()["assistant_message"]["quick_actions"]]
    assert quick[0] == "contact_support"


def test_conversation_rename_resolve_search_and_delete(client, monkeypatch):
    model_id, provider, token = _setup(client, ["先清空尘盒 [1]。"], monkeypatch)
    conversation_id = _create_conversation(client, token, model_id)
    client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"content": "吸力变小了怎么办"},
        headers=auth(token),
    )

    renamed = client.patch(
        f"/api/v1/conversations/{conversation_id}",
        json={"title": "客厅机器吸力问题", "resolved": True},
        headers=auth(token),
    ).json()
    assert renamed["title"] == "客厅机器吸力问题" and renamed["resolved"] is True

    # 搜索命中标题
    assert [c["id"] for c in client.get(
        "/api/v1/conversations", params={"search": "客厅"}, headers=auth(token)
    ).json()] == [conversation_id]
    # 也命中消息正文（用户记得的往往是问过什么，不是标题）
    assert [c["id"] for c in client.get(
        "/api/v1/conversations", params={"search": "吸力变小"}, headers=auth(token)
    ).json()] == [conversation_id]
    assert client.get(
        "/api/v1/conversations", params={"search": "不存在的词"}, headers=auth(token)
    ).json() == []

    assert client.delete(
        f"/api/v1/conversations/{conversation_id}", headers=auth(token)
    ).status_code == 204
    assert client.get("/api/v1/conversations", headers=auth(token)).json() == []
    assert client.get(
        f"/api/v1/conversations/{conversation_id}", headers=auth(token)
    ).status_code == 404


def test_message_feedback_records_reason_and_is_overwritable(client, monkeypatch):
    model_id, provider, token = _setup(client, ["先清空尘盒 [1]。"], monkeypatch)
    conversation_id = _create_conversation(client, token, model_id)
    body = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"content": "吸力变小了怎么办"},
        headers=auth(token),
    ).json()
    assistant_id = body["assistant_message"]["id"]
    user_id = body["user_message"]["id"]

    bad = client.post(
        f"/api/v1/conversations/{conversation_id}/messages/{assistant_id}/feedback",
        json={"helpful": False, "reason": "wrong_citation"},
        headers=auth(token),
    ).json()
    assert bad == {"message_id": assistant_id, "helpful": False, "reason": "wrong_citation"}

    # 改主意按覆盖处理，且"有帮助"不该挂着上一次的差评原因
    good = client.post(
        f"/api/v1/conversations/{conversation_id}/messages/{assistant_id}/feedback",
        json={"helpful": True},
        headers=auth(token),
    ).json()
    assert good["helpful"] is True and good["reason"] is None

    # 给自己的提问打分没有意义，放开只会污染统计
    assert client.post(
        f"/api/v1/conversations/{conversation_id}/messages/{user_id}/feedback",
        json={"helpful": True},
        headers=auth(token),
    ).status_code == 422


def test_chat_refusal_writes_gap_event(client, monkeypatch):
    """聊天里答不上来，必须进内容缺口榜。

    2026-08-06 体检 #2：缺口埋点此前只挂在 /knowledge/answer 上，而前端对该
    端点零调用——聊天（真正的主入口）拒答多少次，缺口表都是 0 行，运营永远
    不知道该补什么资料。这条守住"聊天拒答 → 缺口有记录"这条链。
    """
    from app.models import KnowledgeGapEvent

    model_id, _provider, token = _setup(client, ["REFUSE"], monkeypatch)
    conversation_id = _create_conversation(client, token, model_id)

    resp = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"content": "机器提示 E5 错误码是什么意思"},
        headers=auth(token),
    )
    assert resp.status_code == 200
    assert resp.json()["assistant_message"]["refusal_reason"] == "model_refused"

    with client.app.state.session_factory() as db:
        events = list(db.scalars(select(KnowledgeGapEvent)))
    assert len(events) == 1, "聊天拒答必须留下缺口事件"
    assert events[0].source == "chat_refusal"
    assert events[0].refusal_reason == "model_refused"
    assert events[0].robot_model_id == model_id
    assert events[0].query_normalized == "机器提示 e5 错误码是什么意思"


def test_stream_refusal_also_writes_gap_event(client, monkeypatch):
    """流式路径同样要埋点——它才是前端真正走的那条。"""
    from app.models import KnowledgeGapEvent

    model_id, _provider, token = _setup(client, ["REFUSE"], monkeypatch)
    conversation_id = _create_conversation(client, token, model_id)

    resp = client.post(
        f"/api/v1/conversations/{conversation_id}/messages/stream",
        json={"content": "水箱加水后拖地还是干的"},
        headers=auth(token),
    )
    assert resp.status_code == 200
    _sse_events(resp.text)

    with client.app.state.session_factory() as db:
        events = list(db.scalars(select(KnowledgeGapEvent)))
    assert len(events) == 1
    assert events[0].source == "chat_refusal"


def test_citation_invalid_is_not_a_content_gap(client, monkeypatch):
    """引用不合格是生成质量问题，不该进缺口榜。

    记进去会把运营引向"补资料"这个错误动作——资料其实是有的。
    """
    from app.models import KnowledgeGapEvent

    # 引用越界（片段只有个位数，回答引用 [9]）触发 citation_invalid
    model_id, _provider, token = _setup(client, ["按步骤清理即可 [9]。"], monkeypatch)
    conversation_id = _create_conversation(client, token, model_id)

    resp = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"content": "尘盒怎么清理"},
        headers=auth(token),
    )
    assert resp.status_code == 200
    assert resp.json()["assistant_message"]["refusal_reason"] == "citation_invalid"

    with client.app.state.session_factory() as db:
        assert list(db.scalars(select(KnowledgeGapEvent))) == []


class StreamingScriptedProvider:
    """带真流式能力的测试桩——此前所有流式测试都直接调 answer_events，
    没有一条打过 POST /messages/stream，SSE 编码与事件顺序全靠人眼。"""

    model_name = "stream-test-model"

    def __init__(self, pieces):
        self.pieces = list(pieces)
        self.stream_calls = 0

    def generate(self, *, system: str, prompt: str) -> str:
        return "".join(self.pieces)

    def generate_stream(self, *, system: str, prompt: str):
        self.stream_calls += 1
        yield from self.pieces


def _setup_streaming(client, pieces, monkeypatch):
    model_id, _provider, token = _setup(client, [], monkeypatch)
    provider = StreamingScriptedProvider(pieces)
    client.app.state.generation_provider = provider
    return model_id, provider, token


def test_stream_endpoint_emits_real_deltas_in_contract_order(client, monkeypatch):
    """端到端 SSE：真流式下的事件顺序与编码。"""
    model_id, provider, token = _setup_streaming(
        client, ["先清空尘盒 [1]。", "再清理滤网 [1]。"], monkeypatch
    )
    conversation_id = _create_conversation(client, token, model_id)
    resp = client.post(
        f"/api/v1/conversations/{conversation_id}/messages/stream",
        json={"content": "吸力变小了怎么办"},
        headers=auth(token),
    )
    assert resp.status_code == 200
    assert provider.stream_calls == 1, "必须真的走流式，而不是回退整段生成"

    events = _sse_events(resp.text)
    names = [name for name, _ in events]
    assert names[0] == "stage"
    assert [n for n, _ in events if n == "stage"][:3] == ["stage", "stage", "stage"]
    assert names[-3:] == ["user_message", "assistant_message", "done"]
    assert "discard" not in names, "正常作答不该出现丢弃重发"

    deltas = "".join(data["text"] for name, data in events if name == "delta")
    final = next(data for name, data in events if name == "assistant_message")
    assert deltas == final["content"]

    # 落库时序：done 之后库里就该是完整的一问一答
    detail = client.get(
        f"/api/v1/conversations/{conversation_id}", headers=auth(token)
    ).json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert detail["messages"][1]["content"] == final["content"]


def test_stream_discards_already_sent_text_when_final_verdict_is_refusal(client, monkeypatch):
    """已下发的内容最终没通过校验，必须明确撤回。

    这条分支（discard: refused_after_stream）此前零覆盖——而它正是"用户把
    一段未通过安全/引用校验的文本当成答案"的最后一道闸。
    第一句合法先被放行，第二句引用越界让闸门关闭，全文校验判 citation_invalid。
    """
    model_id, _provider, token = _setup_streaming(
        client, ["先清空尘盒 [1]。", "再看第 [9] 步操作。"], monkeypatch
    )
    conversation_id = _create_conversation(client, token, model_id)
    resp = client.post(
        f"/api/v1/conversations/{conversation_id}/messages/stream",
        json={"content": "吸力变小了怎么办"},
        headers=auth(token),
    )
    assert resp.status_code == 200
    events = _sse_events(resp.text)
    names = [name for name, _ in events]

    assert "delta" in names, "第一句合法，应该已经到过用户屏幕"
    discards = [data for name, data in events if name == "discard"]
    assert discards, "已下发内容最终判拒答，必须下发 discard"
    assert discards[0]["reason"] == "refused_after_stream"

    final = next(data for name, data in events if name == "assistant_message")
    assert final["refusal_reason"] == "citation_invalid"
    assert final["citations"] == []


def test_background_from_first_turn_stays_in_prompt_after_many_rounds(client, monkeypatch):
    """C3 端到端：界面承诺"追问时无需重复背景"，那第 8 轮的提示词里就必须还有背景。

    只测 select_context_messages 不够——路由查几条、生成层又截几条是两层，
    此前两处各写死 6，只改一层等于没改。这里断言的是**真实提示词内容**。
    """
    rounds = 9
    _model_id, provider, token = _setup(client, ["资料未提及。"] * rounds, monkeypatch)
    # 9 轮会撞上默认 6/min 的生成限流；这里测的是上下文窗口，不是限流
    monkeypatch.setattr(
        "app.routers.conversations.get_settings",
        lambda: Settings(
            knowledge_min_score=0.0,
            knowledge_answer_user_per_minute=1000,
            knowledge_answer_ip_per_minute=5000,
            embedding_user_per_minute=1000,
            embedding_ip_per_minute=5000,
            _env_file=None,
        ),
    )
    conversation_id = _create_conversation(client, token, _model_id)

    background = "我的 JH69U1 加水后拖地还是干"
    first = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"content": background},
        headers=auth(token),
    )
    assert first.status_code == 200

    for index in range(rounds - 1):
        follow_up = client.post(
            f"/api/v1/conversations/{conversation_id}/messages",
            json={"content": f"那第{index + 2}步呢"},
            headers=auth(token),
        )
        assert follow_up.status_code == 200

    last_prompt = provider.prompts[-1]
    # 第 9 轮时首轮问题早已滑出"最近 6 条"，旧实现这里必然找不到型号
    assert background in last_prompt
    assert "那第8步呢" in last_prompt

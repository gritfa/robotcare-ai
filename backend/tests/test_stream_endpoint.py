"""SSE 端点端到端：事件顺序、编码、落库时序、撤回、异常契约、断连回滚。

问题背景：流式端点此前缺少端到端测试。
`test_stream_safety_gate.py` 测的是 `answer_events` 这一层，证明不了
"HTTP 上真的按这个顺序发出了这些事件"，更覆盖不到只存在于路由层的
discard 两分支、异常兜底和断连回滚。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from sqlalchemy import func, select

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.config import Settings
from app.knowledge_service import HashingNgramEmbeddingProvider, ingest_pdf
from app.models import Conversation, ConversationMessage, GenerationRecord, RobotModel
from conftest import auth, register
from test_conversations import SYNTHETIC_DIR


class StreamingProvider:
    model_name = "stream-endpoint-model"

    def __init__(self, pieces, error=None):
        self.pieces = list(pieces)
        self.error = error
        self.stream_calls = 0

    def generate(self, *, system: str, prompt: str) -> str:
        if self.error:
            raise self.error
        return "".join(self.pieces)

    def generate_stream(self, *, system: str, prompt: str):
        self.stream_calls += 1
        if self.error:
            raise self.error
        yield from self.pieces


def _setup(client, monkeypatch, provider):
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
    with client.app.state.session_factory() as db:
        model_id = db.scalar(select(RobotModel.id).where(RobotModel.code == "RC-S200"))
        ingest_pdf(
            db,
            robot_model_id=model_id,
            pdf_path=SYNTHETIC_DIR / "RC-S200_manual.pdf",
            source_url="synthetic://robotcare-demo/rc-s200/manual",
            provider=HashingNgramEmbeddingProvider(),
        )
    client.app.state.embedding_provider = HashingNgramEmbeddingProvider()
    client.app.state.generation_provider = provider
    token = register(client, f"stream-{id(provider)}@gritfa-test.com")["access_token"]
    resp = client.post(
        "/api/v1/conversations", json={"robot_model_id": model_id}, headers=auth(token)
    )
    assert resp.status_code == 201
    return token, resp.json()["id"]


def parse_sse(raw: str) -> list[tuple[str, dict]]:
    """把 SSE 原文解析成 [(event, data), ...]。"""
    events = []
    for block in raw.strip().split("\n\n"):
        if not block.strip():
            continue
        name, payload = None, None
        for line in block.split("\n"):
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                payload = json.loads(line[len("data: ") :])
        if name is not None:
            events.append((name, payload))
    return events


def stream_message(client, token, conversation_id, content):
    response = client.post(
        f"/api/v1/conversations/{conversation_id}/messages/stream",
        json={"content": content},
        headers=auth(token),
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    return response, parse_sse(response.text)


def test_event_order_encoding_and_persistence(client, monkeypatch):
    """事件顺序、中文不转义、done 之前必须已落库。"""
    provider = StreamingProvider(["先清空尘盒 [1]。", "再检查吸尘口是否堵塞 [1]。"])
    token, conversation_id = _setup(client, monkeypatch, provider)

    response, events = stream_message(client, token, conversation_id, "吸力变小了怎么办")
    names = [name for name, _ in events]

    # 顺序契约：阶段 → 增量 → 用户消息 → 助手消息 → done
    assert names[0] == "stage"
    assert names[-1] == "done"
    assert names.count("user_message") == 1
    assert names.count("assistant_message") == 1
    assert names.index("user_message") < names.index("assistant_message") < names.index("done")
    assert [n for n, _ in events if n == "stage"] == ["stage"] * 3
    assert [v["stage"] for n, v in events if n == "stage"] == [
        "retrieving", "retrieved", "generating",
    ]
    # 增量必须早于收尾事件，否则"边生成边显示"就是假的
    assert names.index("delta") < names.index("user_message")

    # 编码：中文按 UTF-8 原样下发，避免转换为 Unicode 转义序列。
    assert "先清空尘盒" in response.text
    assert "\\u" not in response.text

    deltas = "".join(v["text"] for n, v in events if n == "delta")
    assistant = next(v for n, v in events if n == "assistant_message")
    assert deltas == assistant["content"]

    # 落库时序：done 到达时数据必须已经在库里（前端据此结束会话刷新）
    with client.app.state.session_factory() as db:
        rows = list(db.scalars(
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation_id)
            .order_by(ConversationMessage.id)
        ))
    assert [row.role for row in rows] == ["user", "assistant"]
    assert rows[1].content == assistant["content"]
    assert rows[0].id == next(v for n, v in events if n == "user_message")["id"]


def test_discard_refused_after_stream(client, monkeypatch):
    """已有内容到过屏幕、最终却判定拒答：必须明确撤回。

    构造真实路径而不是 mock 结果：第一句安全、闸门放行下发；第二句涉及拆机短接，
    闸门关闸（关闸不清已放行内容），全文判定 unsafe_answer 拒答。
    此时 streamed_text 非空 + status=refused，正是 refused_after_stream 分支。
    留着不撤回，用户会把半截未通过校验的文本当成答案。
    """
    provider = StreamingProvider(
        ["先清空尘盒 [1]。", "然后拆开机器外壳并短接电机触点 [1]。"]
    )
    token, conversation_id = _setup(client, monkeypatch, provider)

    _response, events = stream_message(client, token, conversation_id, "吸力变小了怎么办")
    names = [name for name, _ in events]
    assistant = next(v for n, v in events if n == "assistant_message")

    assert assistant["refusal_reason"] == "unsafe_answer"
    assert "discard" in names, "已下发内容 + 最终拒答，必须撤回"
    assert next(v for n, v in events if n == "discard")["reason"] == "refused_after_stream"
    # 撤回必须早于收尾事件，否则前端已经把内容当最终答案渲染完了
    assert names.index("discard") < names.index("assistant_message")
    # 撤回之后不许再补发任何正文增量
    assert not [n for n in names[names.index("discard") :] if n == "delta"]
    # 危险内容一个字都不能留在最终消息里
    assert "短接" not in assistant["content"]


def test_discard_answer_revised_resends_full_text(client, monkeypatch):
    """已下发内容不是最终答案的前缀时，必须 discard + 整段重发。

    绝不能把"旧的半段 + 新的尾段"拼成一个从未存在过的假答案。
    """
    provider = StreamingProvider(["先清空尘盒 [1]。", "再检查吸尘口 [1]。"])
    token, conversation_id = _setup(client, monkeypatch, provider)

    # 让落库的最终文本与已下发内容错开：模拟输出侧改写（末尾标记剥离等）
    original = "app.routers.conversations._assistant_message_for"
    from app.routers import conversations as conversations_module

    real_builder = conversations_module._assistant_message_for

    def revised(conversation, result, decision):
        message = real_builder(conversation, result, decision)
        if message.content:
            message.content = "【改写】" + message.content
        return message

    monkeypatch.setattr(original, revised)

    _response, events = stream_message(client, token, conversation_id, "吸力变小了怎么办")
    names = [name for name, _ in events]
    assistant = next(v for n, v in events if n == "assistant_message")

    assert "discard" in names, "已下发内容不是最终答案前缀时必须撤回"
    assert next(v for n, v in events if n == "discard")["reason"] == "answer_revised"
    # discard 之后重发的增量，拼起来必须正好等于最终答案（不能是拼接产物）
    discard_at = names.index("discard")
    resent = "".join(
        value["text"] for (name, value) in events[discard_at:] if name == "delta"
    )
    assert resent == assistant["content"]
    assert assistant["content"].startswith("【改写】")


def test_generation_failure_becomes_error_event_and_rolls_back(client, monkeypatch):
    """进流之后生成失败：以 error 事件下发，且这一轮整体回滚（用户消息不留痕）。"""
    provider = StreamingProvider([], error=RuntimeError("upstream exploded"))
    token, conversation_id = _setup(client, monkeypatch, provider)

    _response, events = stream_message(client, token, conversation_id, "吸力变小了怎么办")
    names = [name for name, _ in events]
    errors = [v for n, v in events if n == "error"]

    assert errors, "生成失败必须以 error 事件下发，而不是静默截断流"
    assert errors[0]["code"] == "GENERATION_UNAVAILABLE"
    # 失败就不该再有 assistant_message：那意味着前端会渲染一条空回答
    assert "assistant_message" not in names

    with client.app.state.session_factory() as db:
        count = db.scalar(
            select(func.count()).select_from(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation_id)
        )
    assert count == 0, "生成失败必须连用户消息一起回滚，否则会话里留下一条没有回答的提问"


def test_empty_deltas_do_not_break_the_stream(client, monkeypatch):
    """模型吐空串/纯空白块（真实后端会）不能污染下发内容或提前收尾。"""
    provider = StreamingProvider(["", "先清空尘盒 [1]。", "", "  ", "再检查吸尘口 [1]。", ""])
    token, conversation_id = _setup(client, monkeypatch, provider)

    _response, events = stream_message(client, token, conversation_id, "吸力变小了怎么办")
    names = [name for name, _ in events]
    assistant = next(v for n, v in events if n == "assistant_message")

    assert names[-1] == "done"
    deltas = "".join(v["text"] for n, v in events if n == "delta")
    assert deltas == assistant["content"]
    assert "先清空尘盒" in assistant["content"]


def test_single_long_sentence_still_streams_and_persists(client, monkeypatch):
    """超长单句：闸门按句放行，一整句没有句末标点前不会下发。

    要守的是"最终一定发完整"，而不是"必须边生成边发"——
    否则闸门为了发得早就得放宽成句判定。
    """
    long_sentence = "先清空尘盒" + "并检查每一处进风口" * 60 + " [1]。"
    provider = StreamingProvider([long_sentence[i : i + 20] for i in range(0, len(long_sentence), 20)])
    token, conversation_id = _setup(client, monkeypatch, provider)

    _response, events = stream_message(client, token, conversation_id, "吸力变小了怎么办")
    assistant = next(v for n, v in events if n == "assistant_message")
    deltas = "".join(v["text"] for n, v in events if n == "delta")

    assert deltas == assistant["content"]
    assert assistant["content"] == long_sentence
    with client.app.state.session_factory() as db:
        stored = db.scalar(
            select(ConversationMessage.content)
            .where(ConversationMessage.conversation_id == conversation_id,
                   ConversationMessage.role == "assistant")
        )
    assert stored == long_sentence


def test_non_runtime_error_still_ends_the_stream_properly(client, monkeypatch):
    """非 RuntimeError 此前直接穿透，客户端拿到一个既没 error 也没 done 的截断流。

    流的响应头已经发出去，FastAPI 全局异常处理插不进来——同步端点有 500 兜底，
    流式没有。前端只能一直转圈等一个永远不会到的 done。
    """
    provider = StreamingProvider([], error=ValueError("bad payload from upstream"))
    token, conversation_id = _setup(client, monkeypatch, provider)

    _response, events = stream_message(client, token, conversation_id, "吸力变小了怎么办")
    names = [name for name, _ in events]
    errors = [v for n, v in events if n == "error"]

    assert errors, "任何异常都必须以 error 事件收场，不能截断流"
    assert errors[0]["code"] == "INTERNAL_ERROR"
    # 与 GENERATION_UNAVAILABLE 分开：运维要能区分"外部模型挂了"和"我们自己炸了"
    assert errors[0]["code"] != "GENERATION_UNAVAILABLE"
    assert "assistant_message" not in names

    with client.app.state.session_factory() as db:
        count = db.scalar(
            select(func.count()).select_from(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation_id)
        )
    assert count == 0, "内部异常同样要整轮回滚"


def test_client_disconnect_rolls_back_the_whole_turn(client, monkeypatch):
    """用户中途关页面：这一轮整体回滚，不留下一条没有回答的提问。

    真流式下落库发生在全文校验之后，所以断连＝这一轮没发生过。

    这里直接驱动路由返回的响应生成器并在收到第一个 delta 后 `aclose()`——
    ASGI 服务器发现连接断开时走的正是这条路径。**不能用 TestClient 模拟**：
    它会把响应体完整跑完再交给调用方，"提前 break" 时后端其实早就落完库了，
    那样的测试永远是绿的，什么也没证明。
    """
    import anyio
    from starlette.requests import Request

    from app.models import User
    from app.routers.conversations import post_message_stream
    from app.schemas import ChatMessageRequest

    provider = StreamingProvider(["先清空尘盒 [1]。", "再检查吸尘口 [1]。"])
    _token, conversation_id = _setup(client, monkeypatch, provider)

    scope = {
        "type": "http", "http_version": "1.1", "method": "POST",
        "path": f"/api/v1/conversations/{conversation_id}/messages/stream",
        "raw_path": b"/", "root_path": "", "scheme": "http",
        "query_string": b"", "headers": [], "client": ("127.0.0.1", 5555),
        "server": ("testserver", 80), "app": client.app, "state": {},
    }
    received: list[str] = []
    with client.app.state.session_factory() as db:
        user = db.scalar(select(User).order_by(User.id.desc()))
        response = post_message_stream(
            conversation_id,
            ChatMessageRequest(content="吸力变小了怎么办"),
            Request(scope),
            db,
            user,
        )
        body = response.body_iterator

        async def read_then_disconnect():
            async for chunk in body:
                received.append(chunk if isinstance(chunk, str) else chunk.decode())
                if "event: delta" in received[-1]:
                    break
            await body.aclose()

        anyio.run(read_then_disconnect)

    assert any("event: delta" in chunk for chunk in received)
    assert not any("event: done" in chunk for chunk in received)

    with client.app.state.session_factory() as db:
        rows = list(db.scalars(
            select(ConversationMessage)
            .where(ConversationMessage.conversation_id == conversation_id)
        ))
    assert rows == [], "断连必须整轮回滚，不能留下一条没有回答的提问"


def test_concurrent_streams_each_bill_exactly_once(client, monkeypatch):
    """并发流式：每一轮各记一次生成留痕，不多记也不少记。

    计费按 GenerationRecord 走。并发下如果共用了 session 或计数写串，
    要么漏记（少收钱、审计断链），要么重复记（多收钱）。
    """
    from concurrent.futures import ThreadPoolExecutor

    provider = StreamingProvider(["先清空尘盒 [1]。", "再检查吸尘口 [1]。"])
    token, first_conversation = _setup(client, monkeypatch, provider)
    model_id = _model_id_of(client, first_conversation)

    conversation_ids = [first_conversation]
    for _ in range(3):
        created = client.post(
            "/api/v1/conversations", json={"robot_model_id": model_id}, headers=auth(token)
        )
        assert created.status_code == 201
        conversation_ids.append(created.json()["id"])

    with client.app.state.session_factory() as db:
        before = db.scalar(select(func.count()).select_from(GenerationRecord))

    def ask(conversation_id: int):
        return client.post(
            f"/api/v1/conversations/{conversation_id}/messages/stream",
            json={"content": "吸力变小了怎么办"},
            headers=auth(token),
        )

    with ThreadPoolExecutor(max_workers=len(conversation_ids)) as pool:
        responses = list(pool.map(ask, conversation_ids))

    assert all(r.status_code == 200 for r in responses)
    for response in responses:
        names = [name for name, _ in parse_sse(response.text)]
        assert names[-1] == "done"

    with client.app.state.session_factory() as db:
        after = db.scalar(select(func.count()).select_from(GenerationRecord))
        answered = db.scalar(
            select(func.count()).select_from(ConversationMessage)
            .where(ConversationMessage.role == "assistant")
        )
    assert after - before == len(conversation_ids), "每轮生成必须且只能留痕一次"
    assert answered >= len(conversation_ids)


def _model_id_of(client, conversation_id: int) -> int:
    with client.app.state.session_factory() as db:
        return db.scalar(
            select(Conversation.robot_model_id).where(Conversation.id == conversation_id)
        )

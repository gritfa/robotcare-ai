"""阶段1 生成层测试：强制引用 / 三类拒答 / 安全前置 / 留痕 / 限流。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from sqlalchemy import select

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
sys.path.insert(0, str(BACKEND_ROOT))

from app.generation_service import PROMPT_VERSION, generate_answer
from app.knowledge_service import HashingNgramEmbeddingProvider, ingest_pdf
from app.models import GenerationRecord, RobotModel
from conftest import auth, register

SYNTHETIC_DIR = PROJECT_ROOT / "knowledge" / "synthetic"


class ScriptedGenerationProvider:
    """脚本化生成 Provider：按顺序返回预设输出，记录收到的 prompt。"""

    model_name = "scripted-test-model"

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.prompts: list[str] = []
        self.calls = 0

    def generate(self, *, system: str, prompt: str) -> str:
        self.prompts.append(prompt)
        self.calls += 1
        return self.outputs.pop(0)


def _prepare_model_with_knowledge(client, code: str = "RC-S200") -> int:
    with client.app.state.session_factory() as db:
        model_id = db.scalar(select(RobotModel.id).where(RobotModel.code == code))
        ingest_pdf(
            db,
            robot_model_id=model_id,
            pdf_path=SYNTHETIC_DIR / f"{code}_manual.pdf",
            source_url=f"synthetic://robotcare-demo/{code.lower()}/manual",
            provider=HashingNgramEmbeddingProvider(),
        )
        return model_id


def test_answer_with_valid_citations_is_persisted(client):
    model_id = _prepare_model_with_knowledge(client)
    provider = ScriptedGenerationProvider(["先清空尘盒并清理滤网 [1]，再检查吸尘口是否堵塞 [2]。"])
    with client.app.state.session_factory() as db:
        result = generate_answer(
            db,
            user_id=None,
            robot_model_id=model_id,
            query="吸力变小了怎么办",
            embedding_provider=HashingNgramEmbeddingProvider(),
            generation_provider=provider,
            min_score=0.0,
        )
        assert result.status == "answered"
        assert result.citations and {c.index for c in result.citations} == {1, 2}
        # prompt 必须携带编号片段与页码
        assert "[1]" in provider.prompts[0] and "说明书第" in provider.prompts[0]
        record = db.get(GenerationRecord, result.record_id)
        assert record.status == "answered"
        assert record.prompt_version == PROMPT_VERSION
        assert record.provider_model == "scripted-test-model"
        assert record.snippet_count > 0 and len(record.citations_json) == 2
        assert record.snippets_sha256 and record.latency_ms >= 0


def test_trailing_refuse_marker_is_stripped_from_delivered_answer(client):
    # v3 在线评测 FF-001 实锤：模型在完整回答末尾附加 REFUSE 控制标记，
    # 旧逻辑只看首行前 20 字，标记被原样发给用户并写进留痕。
    model_id = _prepare_model_with_knowledge(client)
    provider = ScriptedGenerationProvider(["先清空尘盒并清理滤网 [1]。 REFUSE"])
    with client.app.state.session_factory() as db:
        result = generate_answer(
            db,
            user_id=None,
            robot_model_id=model_id,
            query="吸力变小了怎么办",
            embedding_provider=HashingNgramEmbeddingProvider(),
            generation_provider=provider,
            min_score=0.0,
        )
        assert result.status == "answered"
        assert "REFUSE" not in result.answer
        assert result.answer.endswith("[1]。")
        record = db.get(GenerationRecord, result.record_id)
        assert "REFUSE" not in record.answer


def test_answer_that_is_only_a_trailing_refuse_marker_still_refuses(client):
    # 剥掉标记后没有正文 → 必须按拒答处理，不能返回空回答
    model_id = _prepare_model_with_knowledge(client)
    with client.app.state.session_factory() as db:
        result = generate_answer(
            db,
            user_id=None,
            robot_model_id=model_id,
            query="吸力变小了怎么办",
            embedding_provider=HashingNgramEmbeddingProvider(),
            generation_provider=ScriptedGenerationProvider(["。 REFUSE"]),
            min_score=0.0,
        )
        assert result.status == "refused" and result.refusal_reason == "model_refused"


@pytest.mark.parametrize(
    "raw_answer",
    [
        # 2026-08-05 生产栈实测原文：模型给标记附了中文括号说明，旧模式漏剥
        "更换滤网需自费 [1]。\n\nREFUSE（注：资料未提供滤网单价）",
        "先清空尘盒并清理滤网 [1]。REFUSE(no relevant info)",
        "先清空尘盒并清理滤网 [1]。\nREFUSE：资料未提及电机故障",
    ],
)
def test_trailing_refuse_marker_with_explanation_is_stripped(client, raw_answer):
    # 标记是内部协议，无论模型给它附了什么说明，都不能出现在用户可见文本里
    model_id = _prepare_model_with_knowledge(client)
    with client.app.state.session_factory() as db:
        result = generate_answer(
            db,
            user_id=None,
            robot_model_id=model_id,
            query="吸力变小了怎么办",
            embedding_provider=HashingNgramEmbeddingProvider(),
            generation_provider=ScriptedGenerationProvider([raw_answer]),
            min_score=0.0,
        )
        assert result.status == "answered"
        assert "REFUSE" not in result.answer
        assert result.answer.endswith("[1]。")
        record = db.get(GenerationRecord, result.record_id)
        assert "REFUSE" not in record.answer


def test_answer_body_mentioning_refused_word_is_not_truncated(client):
    # 防过度剥离：正文里出现 REFUSED 之类的词不构成控制标记
    model_id = _prepare_model_with_knowledge(client)
    body = "先清空尘盒并清理滤网 [1]。若显示 REFUSED 字样请联系官方售后。"
    with client.app.state.session_factory() as db:
        result = generate_answer(
            db,
            user_id=None,
            robot_model_id=model_id,
            query="吸力变小了怎么办",
            embedding_provider=HashingNgramEmbeddingProvider(),
            generation_provider=ScriptedGenerationProvider([body]),
            min_score=0.0,
        )
        assert result.answer == body


def test_knowledge_gap_refuses_without_calling_model(client):
    # 不入库任何知识 → 检索为空 → 直接拒答且绝不调用生成模型
    with client.app.state.session_factory() as db:
        model_id = db.scalar(select(RobotModel.id).where(RobotModel.code == "RC-X800"))
        provider = ScriptedGenerationProvider([])
        result = generate_answer(
            db,
            user_id=None,
            robot_model_id=model_id,
            query="有没有自动更换拖布的功能",
            embedding_provider=HashingNgramEmbeddingProvider(),
            generation_provider=provider,
        )
        assert result.status == "refused"
        assert result.refusal_reason == "knowledge_gap"
        assert provider.calls == 0
        record = db.get(GenerationRecord, result.record_id)
        assert record.refusal_reason == "knowledge_gap" and record.snippet_count == 0


def test_model_refuse_token_and_invalid_citation_fail_closed(client):
    model_id = _prepare_model_with_knowledge(client)
    with client.app.state.session_factory() as db:
        # 模型主动拒答
        result = generate_answer(
            db,
            user_id=None,
            robot_model_id=model_id,
            query="吸力变小了怎么办",
            embedding_provider=HashingNgramEmbeddingProvider(),
            generation_provider=ScriptedGenerationProvider(["REFUSE"]),
            min_score=0.0,
        )
        assert result.status == "refused" and result.refusal_reason == "model_refused"

        # 无引用 → 拒答
        result = generate_answer(
            db,
            user_id=None,
            robot_model_id=model_id,
            query="吸力变小了怎么办",
            embedding_provider=HashingNgramEmbeddingProvider(),
            generation_provider=ScriptedGenerationProvider(["直接清理一下就好了。"]),
            min_score=0.0,
        )
        assert result.status == "refused" and result.refusal_reason == "citation_invalid"

        # 引用越界 → 拒答
        result = generate_answer(
            db,
            user_id=None,
            robot_model_id=model_id,
            query="吸力变小了怎么办",
            embedding_provider=HashingNgramEmbeddingProvider(),
            generation_provider=ScriptedGenerationProvider(["清理尘盒即可 [99]。"]),
            min_score=0.0,
        )
        assert result.status == "refused" and result.refusal_reason == "citation_invalid"


def test_unsafe_answer_is_blocked_even_with_citations(client):
    model_id = _prepare_model_with_knowledge(client)
    with client.app.state.session_factory() as db:
        result = generate_answer(
            db,
            user_id=None,
            robot_model_id=model_id,
            query="吸力变小了怎么办",
            embedding_provider=HashingNgramEmbeddingProvider(),
            generation_provider=ScriptedGenerationProvider(["建议拆机检查风机内部 [1]。"]),
            min_score=0.0,
        )
        assert result.status == "refused" and result.refusal_reason == "unsafe_answer"
        record = db.get(GenerationRecord, result.record_id)
        # 危险回答全文留痕供审计，但绝不返回给用户
        assert "拆机" in record.answer
        assert result.answer is None


def test_safety_warning_answer_is_not_refused(client):
    """SYN-FA-006 回归：模型输出的安全警告（提到冒烟/请勿拆机）是合规回答，
    不得被输出侧安全检测误判为 unsafe_answer。"""
    model_id = _prepare_model_with_knowledge(client)
    answer = "请先长按电源键 10 秒重启 [1]；若电源线破损、发热或冒烟，请勿拆机，须立即停用并联系官方售后 [1]。"
    with client.app.state.session_factory() as db:
        result = generate_answer(
            db,
            user_id=None,
            robot_model_id=model_id,
            query="无法开机，按开机键没反应",
            embedding_provider=HashingNgramEmbeddingProvider(),
            generation_provider=ScriptedGenerationProvider([answer]),
            min_score=0.0,
        )
        assert result.status == "answered"
        assert result.refusal_reason is None
        assert "冒烟" in result.answer


def test_answer_api_end_to_end_with_safety_and_auth(client, monkeypatch):
    from app.config import Settings

    # hashing 嵌入是词面相似度，分数天然低于语义向量默认阈值，测试放低阈值
    monkeypatch.setattr(
        "app.api.get_settings", lambda: Settings(knowledge_min_score=0.0, _env_file=None)
    )
    model_id = _prepare_model_with_knowledge(client)
    client.app.state.embedding_provider = HashingNgramEmbeddingProvider()
    client.app.state.generation_provider = ScriptedGenerationProvider(
        ["先清空尘盒 [1]，再清理滤网并晾干后装回 [2]。"]
    )

    # 未登录拒绝
    response = client.post(
        "/api/v1/knowledge/answer",
        json={"robot_model_id": model_id, "query": "吸力变小了"},
    )
    assert response.status_code == 401

    token = register(client, "answer-user@example.com")["access_token"]

    # 高危问题在检索前被安全规则拦截
    blocked = client.post(
        "/api/v1/knowledge/answer",
        headers=auth(token),
        json={"robot_model_id": model_id, "query": "机器人冒烟了怎么修"},
    )
    assert blocked.status_code == 422
    assert blocked.json()["detail"]["code"] == "SAFETY_BLOCKED"

    answered = client.post(
        "/api/v1/knowledge/answer",
        headers=auth(token),
        json={"robot_model_id": model_id, "query": "吸力变小了怎么办"},
    )
    assert answered.status_code == 200, answered.text
    body = answered.json()
    assert body["status"] == "answered"
    assert body["citations"] and body["citations"][0]["page_number"] > 0
    assert body["refusal_reason"] is None

    # 不存在的型号
    missing = client.post(
        "/api/v1/knowledge/answer",
        headers=auth(token),
        json={"robot_model_id": 99999, "query": "吸力变小了"},
    )
    assert missing.status_code == 404


def test_answer_api_rate_limited(client, monkeypatch):
    from app.config import Settings

    monkeypatch.setattr(
        "app.api.get_settings",
        lambda: Settings(
            knowledge_min_score=0.0, knowledge_answer_user_per_minute=3, _env_file=None
        ),
    )
    model_id = _prepare_model_with_knowledge(client)
    client.app.state.embedding_provider = HashingNgramEmbeddingProvider()
    client.app.state.generation_provider = ScriptedGenerationProvider(
        [f"清理尘盒 [1]。步骤{i}" for i in range(20)]
    )
    token = register(client, "rate-limit-answer@example.com")["access_token"]
    statuses = []
    for index in range(8):
        response = client.post(
            "/api/v1/knowledge/answer",
            headers=auth(token),
            json={"robot_model_id": model_id, "query": f"吸力变小了怎么办 {index}"},
        )
        statuses.append(response.status_code)
    assert 429 in statuses, statuses
    limited = [s for s in statuses if s == 429]
    assert statuses.index(429) >= 1  # 前几次成功后才触发限流
    assert limited


def test_build_generation_provider_honors_llm_backend():
    from app.generation_service import (
        DashScopeGenerationProvider,
        OpenAICompatGenerationProvider,
        build_generation_provider,
        generation_backend_configured,
    )

    class _S:
        llm_backend = "openai-compat"
        llm_api_key = "k"
        llm_base_url = "https://api.deepseek.com/v1"
        generation_model = "deepseek-v4-flash"
        dashscope_api_key = None
        dashscope_base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"

    provider = build_generation_provider(_S())
    assert isinstance(provider, OpenAICompatGenerationProvider)
    assert provider.model_name == "deepseek-v4-flash"
    assert generation_backend_configured(_S()) is True

    class _S2(_S):
        llm_backend = "dashscope"
        dashscope_api_key = "dk"

    assert isinstance(build_generation_provider(_S2()), DashScopeGenerationProvider)
    assert generation_backend_configured(_S2()) is True

    class _S3(_S):
        llm_api_key = None

    assert generation_backend_configured(_S3()) is False

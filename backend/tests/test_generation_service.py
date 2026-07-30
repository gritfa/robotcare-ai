"""阶段1 生成层测试：强制引用 / 三类拒答 / 安全前置 / 留痕 / 限流。"""

from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import select

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
sys.path.insert(0, str(BACKEND_ROOT))

from app.generation_service import generate_answer
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
        assert record.prompt_version == "answer-v1"
        assert record.provider_model == "scripted-test-model"
        assert record.snippet_count > 0 and len(record.citations_json) == 2
        assert record.snippets_sha256 and record.latency_ms >= 0


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

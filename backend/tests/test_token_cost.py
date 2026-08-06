"""token 用量与成本：每月账单不能靠猜。

体检发现（2026-08-05）：generation_service 拿到 provider 响应后把 usage 段
直接丢了，GenerationRecord 只有 latency_ms——既算不出账单，也答不出
"哪个型号在烧钱"。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select

from app.config import Settings
from app.generation_service import (
    TokenUsage,
    _usage_from_payload,
    estimate_cost,
    generate_answer,
)
from app.knowledge_service import HashingNgramEmbeddingProvider, ingest_pdf
from app.models import GenerationRecord, RobotModel
from conftest import auth, register
from test_admin_api import make_admin
from test_generation_service import SYNTHETIC_DIR


class UsageReportingProvider:
    """会报用量的假后端。

    用量随调用返回（generate_with_usage），不再挂在实例上——provider 是全局
    单例，实例属性在并发下会串账，未调用模型的拒答还会读到残留值记幽灵账
    （2026-08-06 体检 #8）。
    """

    model_name = "usage-test-model"

    def __init__(self, text: str, usage: TokenUsage) -> None:
        self.text = text
        self.usage = usage
        self.calls = 0

    def generate(self, *, system: str, prompt: str) -> str:
        self.calls += 1
        return self.text

    def generate_with_usage(self, *, system: str, prompt: str) -> tuple[str, TokenUsage]:
        return self.generate(system=system, prompt=prompt), self.usage


class SilentProvider:
    """不报用量的后端（旧桩、离线评测都是这样）。"""

    model_name = "silent-test-model"

    def __init__(self, text: str) -> None:
        self.text = text

    def generate(self, *, system: str, prompt: str) -> str:
        return self.text


def _answer_with(client, provider):
    code = "RC-S200"
    with client.app.state.session_factory() as db:
        model_id = db.scalar(select(RobotModel.id).where(RobotModel.code == code))
        ingest_pdf(
            db,
            robot_model_id=model_id,
            pdf_path=SYNTHETIC_DIR / f"{code}_manual.pdf",
            source_url=f"synthetic://robotcare-demo/{code.lower()}/manual",
            provider=HashingNgramEmbeddingProvider(),
        )
        result = generate_answer(
            db,
            user_id=None,
            robot_model_id=model_id,
            query="吸力变小了怎么办",
            embedding_provider=HashingNgramEmbeddingProvider(),
            generation_provider=provider,
            min_score=0.0,
        )
        return db.get(GenerationRecord, result.record_id), result


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"usage": {"prompt_tokens": 120, "completion_tokens": 45}}, (120, 45)),
        ({"usage": {"input_tokens": 7, "output_tokens": 3}}, (7, 3)),
        ({"usage": {}}, (None, None)),
        ({}, (None, None)),
        (None, (None, None)),
    ],
)
def test_usage_extraction_covers_provider_field_variants(payload, expected):
    usage = _usage_from_payload(payload)
    assert (usage.prompt_tokens, usage.completion_tokens) == expected


def test_cost_is_none_when_usage_is_unavailable():
    """没有用量却记 0，会让成本看板看起来"很省钱"——比没有数字更误导。"""
    assert estimate_cost(TokenUsage(), "any-model") is None


def test_cost_uses_configured_price_per_model():
    settings = Settings(
        llm_token_prices='{"pricey-model": {"prompt": 10, "completion": 30}}'
    )
    usage = TokenUsage(prompt_tokens=1_000_000, completion_tokens=1_000_000)

    assert estimate_cost(usage, "pricey-model", settings) == Decimal("40.000000")


def test_cost_falls_back_to_default_price_for_unknown_model():
    settings = Settings(
        llm_default_prompt_price_per_million=2,
        llm_default_completion_price_per_million=6,
    )
    usage = TokenUsage(prompt_tokens=1_000_000, completion_tokens=1_000_000)

    assert estimate_cost(usage, "not-configured", settings) == Decimal("8.000000")


def test_usage_and_cost_are_persisted(client):
    provider = UsageReportingProvider(
        "先清空尘盒并清理滤网 [1]。", TokenUsage(prompt_tokens=800, completion_tokens=200)
    )

    record, result = _answer_with(client, provider)

    assert result.status == "answered"
    assert record.prompt_tokens == 800
    assert record.completion_tokens == 200
    assert record.estimated_cost is not None and record.estimated_cost > 0


def test_provider_without_usage_leaves_columns_null(client):
    """取不到用量就留空，绝不用 0 冒充。"""
    record, _result = _answer_with(client, SilentProvider("先清空尘盒并清理滤网 [1]。"))

    assert record.prompt_tokens is None
    assert record.completion_tokens is None
    assert record.estimated_cost is None


def test_refused_answer_still_records_cost(client):
    """拒答同样烧了 token（模型已经跑完），不记账单就对不上。"""
    provider = UsageReportingProvider(
        "这句没有任何引用编号。", TokenUsage(prompt_tokens=600, completion_tokens=30)
    )

    record, result = _answer_with(client, provider)

    assert result.status == "refused"
    assert record.prompt_tokens == 600
    assert record.estimated_cost is not None and record.estimated_cost > 0


def test_overview_exposes_token_cost(client):
    provider = UsageReportingProvider(
        "先清空尘盒并清理滤网 [1]。", TokenUsage(prompt_tokens=1000, completion_tokens=500)
    )
    _answer_with(client, provider)
    admin_token = make_admin(client, "cost-admin@example.com")

    overview = client.get("/api/v1/admin/overview", headers=auth(admin_token)).json()

    cost = overview["token_cost"]
    assert cost["prompt_tokens"] == 1000
    assert cost["completion_tokens"] == 500
    assert cost["estimated_cost"] > 0
    assert cost["records_with_usage"] == 1
    assert cost["total_records"] >= 1
    assert cost["by_model"]["usage-test-model"] > 0


def test_overview_reports_coverage_so_cost_is_not_mistaken_for_exact(client):
    """有后端不报用量时，成本是低估的——覆盖率必须能看出来。"""
    _answer_with(client, SilentProvider("先清空尘盒并清理滤网 [1]。"))
    admin_token = make_admin(client, "cost-admin2@example.com")

    cost = client.get("/api/v1/admin/overview", headers=auth(admin_token)).json()["token_cost"]

    assert cost["total_records"] >= 1
    assert cost["records_with_usage"] == 0
    assert cost["estimated_cost"] == 0


def test_non_admin_cannot_read_overview(client):
    token = register(client, "cost-plain@example.com")["access_token"]

    assert client.get("/api/v1/admin/overview", headers=auth(token)).status_code == 403


def test_gap_refusal_records_no_cost_when_model_was_never_called(client, monkeypatch):
    """检索没命中就拒答——一分钱没花，绝不能记账。

    2026-08-06 体检 #8：usage 此前挂在全局单例 provider 的 last_usage 上，
    knowledge_gap 分支根本没调用模型，却会读到**上一次请求**的残留用量，
    凭空记一笔幽灵账单。
    """
    from app.generation_service import answer_events

    provider = UsageReportingProvider("不会被用到", TokenUsage(prompt_tokens=999, completion_tokens=999))
    # 先跑一次正常调用，把 provider 内部的 _call_usage 填上真实数字
    client.app.state.generation_provider = provider

    with client.app.state.session_factory() as db:
        model_id = db.scalar(select(RobotModel.id).where(RobotModel.code == "RC-X800"))
        events = answer_events(
            db,
            user_id=None,
            robot_model_id=model_id,  # 该型号未入库任何知识 → knowledge_gap
            query="完全查不到的问题",
            embedding_provider=HashingNgramEmbeddingProvider(),
            generation_provider=provider,
        )
        result = None
        while True:
            try:
                next(events)
            except StopIteration as stop:
                result = stop.value
                break

        assert result.status == "refused"
        assert result.refusal_reason == "knowledge_gap"
        assert provider.calls == 0, "检索没命中就不该调用模型"

        record = db.get(GenerationRecord, result.record_id)
        assert record.prompt_tokens is None, "没调用模型却记了 token"
        assert record.completion_tokens is None
        assert record.estimated_cost is None, "没花钱却记了成本＝幽灵账单"


def test_usage_belongs_to_its_own_call_not_the_shared_provider(client):
    """两次不同用量的调用，各记各的账。

    provider 是 app.state 上的全局单例；用量若挂在实例属性上，并发下
    后一次读到的可能是前一次的数字。
    """
    with client.app.state.session_factory() as db:
        model_id = db.scalar(select(RobotModel.id).where(RobotModel.code == "RC-S200"))
        ingest_pdf(
            db,
            robot_model_id=model_id,
            pdf_path=SYNTHETIC_DIR / "RC-S200_manual.pdf",
            source_url="synthetic://robotcare-demo/rc-s200/manual",
            provider=HashingNgramEmbeddingProvider(),
        )

        first = UsageReportingProvider(
            "先清空尘盒 [1]。", TokenUsage(prompt_tokens=100, completion_tokens=10)
        )
        second = UsageReportingProvider(
            "再清理滤网 [1]。", TokenUsage(prompt_tokens=700, completion_tokens=70)
        )
        records = []
        for provider in (first, second):
            result = generate_answer(
                db,
                user_id=None,
                robot_model_id=model_id,
                query="吸力变小了怎么办",
                embedding_provider=HashingNgramEmbeddingProvider(),
                generation_provider=provider,
                min_score=0.0,
            )
            records.append(db.get(GenerationRecord, result.record_id))

    assert [r.prompt_tokens for r in records] == [100, 700]
    assert [r.completion_tokens for r in records] == [10, 70]

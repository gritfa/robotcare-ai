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
    """会报用量的假后端，模拟真实 provider 的 last_usage 约定。"""

    model_name = "usage-test-model"

    def __init__(self, text: str, usage: TokenUsage) -> None:
        self.text = text
        self.last_usage = usage

    def generate(self, *, system: str, prompt: str) -> str:
        return self.text


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

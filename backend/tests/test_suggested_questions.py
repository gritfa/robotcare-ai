"""按型号的建议问题：数据源优先级与隐私口径。

产品口径（2026-08-05）：建议一个答不上来的问题，等于把用户直接推进拒答；
所以只建议"真实问过且当时成功作答"的问题，其次是有已发布诊断流程的故障类别。
隐私口径：单次出现的提问可能夹带住址/手机号/订单号，不外显给同型号其他用户。
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.models import GenerationRecord, RobotModel
from app.rate_limit_service import utcnow
from app.suggestion_service import (
    FALLBACK_QUESTIONS,
    LOOKBACK_DAYS,
    suggested_questions,
)


PII_QUERIES = (
    "我的手机号 13800001111 收不到验证码",
    "订单什么时候发货",
    "地址填错了怎么改",
)


def _record(db, model_id: int, query: str, status: str = "answered", age_days: int = 1):
    record = GenerationRecord(
        user_id=None,
        robot_model_id=model_id,
        query=query,
        prompt_version="answer-v5",
        provider_model="test-model",
        status=status,
        answer="示例回答" if status == "answered" else None,
        citations_json=[],
        refusal_reason=None if status == "answered" else "knowledge_gap",
        snippets_sha256="0" * 64,
        snippet_count=1,
        latency_ms=1.0,
        created_at=utcnow() - timedelta(days=age_days),
    )
    db.add(record)
    return record


def _model_id(db) -> int:
    """种子数据里的真机型号；诊断流程与知识文档都挂在它下面。"""
    return db.scalar(select(RobotModel.id).where(RobotModel.code == "JH69U1"))


def test_history_questions_need_two_occurrences(client):
    """只出现一次的提问不外显——它可能夹带这一个用户的具体情况。"""
    with client.app.state.session_factory() as db:
        model_id = _model_id(db)
        _record(db, model_id, "主刷卡住怎么清理？")
        _record(db, model_id, "主刷卡住怎么清理？")
        _record(db, model_id, "只问过一次的问题")
        db.commit()

        texts = [item.text for item in suggested_questions(db, model_id)]

    assert "主刷卡住怎么清理？" in texts
    assert "只问过一次的问题" not in texts


def test_refused_questions_are_never_suggested(client):
    """答不上来的问题被建议出去，等于把用户直接推进拒答。"""
    with client.app.state.session_factory() as db:
        model_id = _model_id(db)
        for _ in range(5):
            _record(db, model_id, "这个型号能装宠物毛发刷吗？", status="refused")
        db.commit()

        texts = [item.text for item in suggested_questions(db, model_id)]

    assert "这个型号能装宠物毛发刷吗？" not in texts


def test_questions_carrying_personal_data_are_filtered(client):
    with client.app.state.session_factory() as db:
        model_id = _model_id(db)
        for query in PII_QUERIES:
            for _ in range(3):
                _record(db, model_id, query)
        db.commit()

        texts = [item.text for item in suggested_questions(db, model_id)]

    assert all(query not in texts for query in PII_QUERIES)


def test_overlong_questions_are_filtered(client):
    long_query = "我家的扫地机器人从上周开始在客厅地毯边缘反复打转并且提示音一直响个不停应该怎么处理"
    with client.app.state.session_factory() as db:
        model_id = _model_id(db)
        for _ in range(4):
            _record(db, model_id, long_query)
        db.commit()

        texts = [item.text for item in suggested_questions(db, model_id)]

    assert long_query not in texts


def test_stale_history_is_ignored(client):
    with client.app.state.session_factory() as db:
        model_id = _model_id(db)
        for _ in range(3):
            _record(db, model_id, "很久以前问过的问题", age_days=LOOKBACK_DAYS + 10)
        db.commit()

        texts = [item.text for item in suggested_questions(db, model_id)]

    assert "很久以前问过的问题" not in texts


def test_falls_back_to_flow_categories_then_generic(client):
    """没有问答历史时，退到有已发布流程的故障类别，再退到通用问题。"""
    with client.app.state.session_factory() as db:
        results = suggested_questions(db, _model_id(db))

    assert results, "建议问题不能为空——空态没有抓手就是冷启动失败"
    assert {item.source for item in results} <= {"history", "flow", "fallback"}
    assert any(item.source in ("flow", "fallback") for item in results)


def test_unknown_model_returns_empty(client):
    with client.app.state.session_factory() as db:
        assert suggested_questions(db, 999999) == []


def test_history_ranks_before_flow_and_fallback(client):
    with client.app.state.session_factory() as db:
        model_id = _model_id(db)
        for _ in range(3):
            _record(db, model_id, "滤网怎么清洗？")
        db.commit()

        results = suggested_questions(db, model_id)

    assert results[0].text == "滤网怎么清洗？"
    assert results[0].source == "history"


def test_no_duplicates_across_sources(client):
    with client.app.state.session_factory() as db:
        model_id = _model_id(db)
        for _ in range(3):
            _record(db, model_id, FALLBACK_QUESTIONS[0])
        db.commit()

        texts = [item.text for item in suggested_questions(db, model_id)]

    assert len(texts) == len(set(texts))


def test_endpoint_requires_auth_and_returns_questions(client):
    anonymous = client.get("/api/v1/models/1/suggested-questions")
    assert anonymous.status_code in (401, 403)

    client.post(
        "/api/v1/auth/register",
        json={
            "email": "suggest-user@example.com",
            "password": "StrongPass123",
            "name": "建议问题用户",
        },
    )
    login = client.post(
        "/api/v1/auth/login",
        json={"email": "suggest-user@example.com", "password": "StrongPass123"},
    )
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    with client.app.state.session_factory() as db:
        model_id = _model_id(db)

    response = client.get(f"/api/v1/models/{model_id}/suggested-questions", headers=headers)
    assert response.status_code == 200
    payload = response.json()
    assert payload, "建议问题接口不能返回空列表"
    assert all(set(item) == {"text", "source"} for item in payload)

    missing = client.get("/api/v1/models/999999/suggested-questions", headers=headers)
    assert missing.status_code == 404

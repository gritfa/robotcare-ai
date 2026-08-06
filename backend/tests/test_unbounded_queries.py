"""无界查询与登录侧限流（体检 D5）。

这一组锁的都是「不加限制也能跑通、但一到规模就出事」的地方：
会话详情全量加载、列表接口没有 limit、搜索没有长度限制且未转义 LIKE 通配、
成功登录不计入任何速率、会话表没有上限。功能测试对它们全部免疫，
所以必须单独测——正常用量下它们看起来都是对的。
"""

from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import select

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from app.config import Settings
from app.models import ConversationMessage, RobotModel, utcnow
from app.routers.conversations import MAX_MESSAGE_PAGE_SIZE, MESSAGE_PAGE_SIZE
from conftest import auth, register


def _model_id(client) -> int:
    with client.app.state.session_factory() as db:
        return db.scalar(select(RobotModel.id).where(RobotModel.code == "RC-S200"))


def _conversation_with_messages(client, token, model_id, count: int) -> int:
    resp = client.post(
        "/api/v1/conversations", json={"robot_model_id": model_id}, headers=auth(token)
    )
    assert resp.status_code == 201, resp.text
    conversation_id = resp.json()["id"]
    # 直接写库：这里要造的是「几百轮历史」的规模，走接口既慢又会打到生成链路
    with client.app.state.session_factory() as db:
        for index in range(count):
            db.add(
                ConversationMessage(
                    conversation_id=conversation_id,
                    role="user" if index % 2 == 0 else "assistant",
                    content=f"第 {index} 条消息",
                    citations_json=[],
                    created_at=utcnow(),
                )
            )
        db.commit()
    return conversation_id


def test_conversation_detail_returns_only_the_most_recent_page(client):
    token = register(client, "d5-detail@gritfa-test.com")["access_token"]
    model_id = _model_id(client)
    total = MESSAGE_PAGE_SIZE + 37
    conversation_id = _conversation_with_messages(client, token, model_id, total)

    response = client.get(f"/api/v1/conversations/{conversation_id}", headers=auth(token))
    assert response.status_code == 200, response.text
    body = response.json()

    assert len(body["messages"]) == MESSAGE_PAGE_SIZE
    assert body["total_messages"] == total
    assert body["truncated"] is True
    # 返回的必须是**最近**一页，且按时间正序（前端从上往下渲染）
    assert body["messages"][-1]["content"] == f"第 {total - 1} 条消息"
    assert body["messages"][0]["content"] == f"第 {total - MESSAGE_PAGE_SIZE} 条消息"


def test_conversation_detail_page_size_is_capped(client):
    token = register(client, "d5-cap@gritfa-test.com")["access_token"]
    model_id = _model_id(client)
    conversation_id = _conversation_with_messages(client, token, model_id, 3)

    over = client.get(
        f"/api/v1/conversations/{conversation_id}",
        params={"limit": MAX_MESSAGE_PAGE_SIZE + 1},
        headers=auth(token),
    )
    assert over.status_code == 422

    short = client.get(
        f"/api/v1/conversations/{conversation_id}",
        params={"limit": 2},
        headers=auth(token),
    )
    assert short.status_code == 200
    assert len(short.json()["messages"]) == 2
    assert short.json()["truncated"] is True


def test_short_conversation_is_not_reported_as_truncated(client):
    token = register(client, "d5-short@gritfa-test.com")["access_token"]
    model_id = _model_id(client)
    conversation_id = _conversation_with_messages(client, token, model_id, 4)

    body = client.get(
        f"/api/v1/conversations/{conversation_id}", headers=auth(token)
    ).json()
    assert body["total_messages"] == 4
    assert body["truncated"] is False


def test_conversation_search_treats_wildcards_as_literal_text(client):
    """`%` 不转义时会退化成「匹配全部」，在会话正文上就是一次全表 ILIKE 扫描。"""

    token = register(client, "d5-search@gritfa-test.com")["access_token"]
    model_id = _model_id(client)
    # 造两条会话：一条标题含字面量 %，一条不含
    plain = client.post(
        "/api/v1/conversations", json={"robot_model_id": model_id}, headers=auth(token)
    ).json()["id"]
    client.patch(
        f"/api/v1/conversations/{plain}",
        json={"title": "尘盒清理"},
        headers=auth(token),
    )
    literal = client.post(
        "/api/v1/conversations", json={"robot_model_id": model_id}, headers=auth(token)
    ).json()["id"]
    client.patch(
        f"/api/v1/conversations/{literal}",
        json={"title": "电量 100% 还是充不满"},
        headers=auth(token),
    )

    # 搜 "%"：转义后只应命中标题里真的有 % 的那条，而不是全部
    hits = client.get(
        "/api/v1/conversations", params={"search": "%"}, headers=auth(token)
    ).json()
    assert [row["id"] for row in hits] == [literal]

    # "_" 同理：单字符通配不转义会把任意标题都匹上
    underscore_hits = client.get(
        "/api/v1/conversations", params={"search": "_"}, headers=auth(token)
    ).json()
    assert underscore_hits == []


def test_conversation_search_rejects_overlong_keywords(client):
    token = register(client, "d5-longsearch@gritfa-test.com")["access_token"]
    response = client.get(
        "/api/v1/conversations", params={"search": "x" * 121}, headers=auth(token)
    )
    assert response.status_code == 422


def test_conversation_count_is_capped_per_user(client, monkeypatch):
    """会话表此前无上限：一个脚本可以无限建空会话。"""

    monkeypatch.setattr(
        "app.routers.conversations.get_settings",
        lambda: Settings(max_conversations_per_user=3, _env_file=None),
    )
    token = register(client, "d5-conv-cap@gritfa-test.com")["access_token"]
    model_id = _model_id(client)

    for _ in range(3):
        created = client.post(
            "/api/v1/conversations",
            json={"robot_model_id": model_id},
            headers=auth(token),
        )
        assert created.status_code == 201, created.text

    blocked = client.post(
        "/api/v1/conversations", json={"robot_model_id": model_id}, headers=auth(token)
    )
    # 409 而不是 429：这不是「太快了」，是「你的会话太多了，删掉一些」
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"]["code"] == "CONVERSATION_LIMIT_REACHED"
    assert blocked.json()["detail"]["limit"] == 3

    # 删掉一条就应当能继续建，上限是「同时存在」而不是「累计创建」
    listed = client.get("/api/v1/conversations", headers=auth(token)).json()
    client.delete(f"/api/v1/conversations/{listed[0]['id']}", headers=auth(token))
    again = client.post(
        "/api/v1/conversations", json={"robot_model_id": model_id}, headers=auth(token)
    )
    assert again.status_code == 201, again.text


def test_device_and_diagnostic_lists_accept_bounded_limits(client):
    token = register(client, "d5-lists@gritfa-test.com")["access_token"]

    for path in ("/api/v1/devices", "/api/v1/diagnostics"):
        assert client.get(path, params={"limit": 1}, headers=auth(token)).status_code == 200
        over = client.get(path, params={"limit": 100_000}, headers=auth(token))
        assert over.status_code == 422, f"{path} 仍接受无界 limit"
        zero = client.get(path, params={"limit": 0}, headers=auth(token))
        assert zero.status_code == 422


def test_device_list_honours_limit(client):
    token = register(client, "d5-devices@gritfa-test.com")["access_token"]
    model_id = _model_id(client)
    for index in range(3):
        created = client.post(
            "/api/v1/devices",
            json={"robot_model_id": model_id, "nickname": f"设备{index}"},
            headers=auth(token),
        )
        assert created.status_code == 201, created.text

    assert len(client.get("/api/v1/devices", headers=auth(token)).json()) == 3
    limited = client.get("/api/v1/devices", params={"limit": 2}, headers=auth(token))
    assert len(limited.json()) == 2


def test_successful_logins_are_rate_limited(client, monkeypatch):
    """密码对了不等于可以无限刷：每次成功登录都签发 token 并写一行 refresh_tokens。"""

    monkeypatch.setattr(
        "app.rate_limit_service.get_settings",
        lambda: Settings(
            login_success_email_per_minute=3,
            login_success_ip_per_minute=1000,
            _env_file=None,
        ),
    )
    email = "d5-login@gritfa-test.com"
    register(client, email)
    credentials = {"email": email, "password": "StrongPass123"}

    statuses = [
        client.post("/api/v1/auth/login", json=credentials).status_code
        for _ in range(4)
    ]
    assert statuses[:3] == [200, 200, 200]
    assert statuses[3] == 429, statuses

    body = client.post("/api/v1/auth/login", json=credentials).json()
    assert body["detail"]["code"] == "RATE_LIMITED"
    assert body["detail"]["action"] == "login_success"


def test_successful_login_limit_is_per_ip_too(client, monkeypatch):
    """按 IP 的桶要独立生效——否则换一批邮箱就能绕过账号维度的上限。"""

    monkeypatch.setattr(
        "app.rate_limit_service.get_settings",
        lambda: Settings(
            login_success_email_per_minute=1000,
            login_success_ip_per_minute=2,
            _env_file=None,
        ),
    )
    for index in range(3):
        register(client, f"d5-ip-{index}@gritfa-test.com")

    statuses = [
        client.post(
            "/api/v1/auth/login",
            json={"email": f"d5-ip-{index}@gritfa-test.com", "password": "StrongPass123"},
        ).status_code
        for index in range(3)
    ]
    assert statuses == [200, 200, 429], statuses

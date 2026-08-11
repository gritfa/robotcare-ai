"""角色分级：给运营看一眼看板，不该顺带给出删知识库的权限。

问题背景：角色曾只有 user/admin 两级（models.py CheckConstraint 约束），
想让运营看内容缺口榜就得给 admin，而 admin 能删文档、回滚版本、读任意用户报告。
此外，admin_cli 需要为已有管理员提供受控的密码重置能力。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.admin_cli import AdminProvisioningError, reset_password, set_role
from app.models import AuditLog, AuthSession, User
from app.security import ROLE_CAPABILITIES, has_capability
from conftest import auth, register


def _with_role(client, email: str, role: str) -> str:
    token = register(client, email)["access_token"]
    with client.app.state.session_factory() as db:
        user = db.scalar(select(User).where(User.email == email))
        user.role = role
        db.commit()
    return token


@pytest.mark.parametrize(
    "role,capability,expected",
    [
        ("user", "read_operations", False),
        ("viewer", "read_operations", True),
        ("viewer", "manage_knowledge", False),
        ("viewer", "administer", False),
        ("operator", "manage_knowledge", True),
        ("operator", "administer", False),
        ("admin", "administer", True),
        ("nonexistent-role", "read_operations", False),
    ],
)
def test_capability_matrix(role, capability, expected):
    assert has_capability(role, capability) is expected


def test_viewer_can_read_dashboard_but_not_change_anything(client):
    token = _with_role(client, "viewer@example.com", "viewer")

    assert client.get("/api/v1/admin/overview", headers=auth(token)).status_code == 200
    assert client.get("/api/v1/admin/feedback", headers=auth(token)).status_code == 200
    assert client.get("/api/v1/admin/conversations", headers=auth(token)).status_code == 200
    assert client.get("/api/v1/admin/content-gaps", headers=auth(token)).status_code == 200
    assert client.get("/api/v1/admin/audit-logs", headers=auth(token)).status_code == 200

    # 看板可读 ≠ 能改东西
    assert client.post(
        "/api/v1/admin/models",
        json={"code": "VW-1", "name": "越权型号"},
        headers=auth(token),
    ).status_code == 403
    assert client.delete(
        "/api/v1/admin/knowledge/documents/1", headers=auth(token)
    ).status_code == 403


def test_operator_manages_content_but_cannot_delete_or_rollback(client):
    """内容运营能上传发布，但删除与回滚是不可逆操作，必须留给 admin。"""
    token = _with_role(client, "operator@example.com", "operator")

    assert client.get("/api/v1/admin/overview", headers=auth(token)).status_code == 200
    # 改标题/启停属于内容运营（404 是因为文档不存在，不是权限问题）
    assert client.patch(
        "/api/v1/admin/knowledge/documents/999999",
        json={"title": "新标题"},
        headers=auth(token),
    ).status_code == 404

    # 删除与回滚：403 而不是 404，说明是被权限挡下的
    assert client.delete(
        "/api/v1/admin/knowledge/documents/999999", headers=auth(token)
    ).status_code == 403
    assert client.post(
        "/api/v1/admin/knowledge/documents/999999/rollback",
        json={"version": 1},
        headers=auth(token),
    ).status_code == 403
    assert client.post(
        "/api/v1/admin/models",
        json={"code": "OP-1", "name": "越权型号"},
        headers=auth(token),
    ).status_code == 403


def test_plain_user_still_sees_nothing(client):
    token = register(client, "plain-tier@example.com")["access_token"]

    for path in (
        "/api/v1/admin/overview",
        "/api/v1/admin/feedback",
        "/api/v1/admin/conversations",
        "/api/v1/admin/knowledge/documents",
    ):
        assert client.get(path, headers=auth(token)).status_code == 403, path


def test_reset_password_revokes_existing_sessions(client):
    """密码要重置，通常正是因为怀疑它泄漏了——留着旧会话等于没重置。"""
    email = "reset-me@example.com"
    token = register(client, email)["access_token"]
    assert client.get("/api/v1/auth/me", headers=auth(token)).status_code == 200

    with client.app.state.session_factory() as db:
        user = reset_password(db, email=email, password="BrandNewPass123")
        assert user.password_hash

    assert client.get("/api/v1/auth/me", headers=auth(token)).status_code == 401
    login = client.post(
        "/api/v1/auth/login", json={"email": email, "password": "BrandNewPass123"}
    )
    assert login.status_code == 200


def test_reset_password_is_audited(client):
    email = "reset-audit@example.com"
    register(client, email)

    with client.app.state.session_factory() as db:
        reset_password(db, email=email, password="BrandNewPass123")
        actions = set(db.scalars(select(AuditLog.action)).all())

    assert "admin_user.password_reset" in actions


def test_reset_password_rejects_unknown_user_and_short_password(client):
    register(client, "reset-guard@example.com")

    with client.app.state.session_factory() as db:
        with pytest.raises(AdminProvisioningError):
            reset_password(db, email="nobody@example.com", password="BrandNewPass123")
        with pytest.raises(AdminProvisioningError):
            reset_password(db, email="reset-guard@example.com", password="short")
        with pytest.raises(AdminProvisioningError):
            reset_password(db, email="reset-guard@example.com", password=None)


def test_set_role_promotes_and_audits(client):
    email = "role-change@example.com"
    register(client, email)

    with client.app.state.session_factory() as db:
        user = set_role(db, email=email, role="operator")
        assert user.role == "operator"
        actions = set(db.scalars(select(AuditLog.action)).all())

    assert "admin_user.role_changed" in actions


def test_set_role_rejects_unknown_role(client):
    email = "role-guard@example.com"
    register(client, email)

    with client.app.state.session_factory() as db:
        with pytest.raises(AdminProvisioningError):
            set_role(db, email=email, role="superuser")


def test_all_roles_in_capability_table_are_accepted_by_the_database(client):
    """能力表里的角色必须都能落库，否则 CheckConstraint 与代码会分叉。"""
    with client.app.state.session_factory() as db:
        for index, role in enumerate(sorted(ROLE_CAPABILITIES)):
            user = User(
                email=f"role-{index}@example.com",
                password_hash="x" * 20,
                role=role,
            )
            db.add(user)
        db.commit()
        stored = set(db.scalars(select(User.role)).all())

    assert set(ROLE_CAPABILITIES) <= stored


def test_revoked_sessions_are_marked_with_reason(client):
    email = "reset-reason@example.com"
    register(client, email)

    with client.app.state.session_factory() as db:
        reset_password(db, email=email, password="BrandNewPass123")
        reasons = set(
            db.scalars(select(AuthSession.revoke_reason).where(AuthSession.revoked_at.isnot(None))).all()
        )

    assert reasons == {"password_reset"}


def test_auth_me_exposes_capabilities_for_the_frontend(client):
    """/auth/me 下发权限能力，供前端统一控制管理入口。"""
    for role, expected in (
        ("user", []),
        ("viewer", ["read_operations"]),
        ("operator", ["manage_knowledge", "read_operations"]),
        ("admin", ["administer", "manage_knowledge", "read_operations"]),
    ):
        email = f"caps-{role}@example.com"
        token = register(client, email)["access_token"]
        if role != "user":
            with client.app.state.session_factory() as db:
                set_role(db, email=email, role=role)
            token = client.post(
                "/api/v1/auth/login",
                json={"email": email, "password": "StrongPass123"},
            ).json()["access_token"]
        body = client.get("/api/v1/auth/me", headers=auth(token)).json()
        assert body["role"] == role
        assert body["capabilities"] == expected, f"{role} 的能力位不对"

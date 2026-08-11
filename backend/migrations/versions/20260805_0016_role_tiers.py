"""Add viewer/operator role tiers between user and admin.

Revision ID: 20260805_0016
Revises: 20260805_0015
Create Date: 2026-08-05

问题背景：角色曾只有 user/admin 两级，运营只读访问会同时获得
删知识库、回滚版本、读任意用户报告的权限。
"""

from collections.abc import Sequence

from alembic import op


revision: str = "20260805_0016"
down_revision: str | None = "20260805_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_users_role", "users", type_="check")
    op.create_check_constraint(
        "ck_users_role", "users", "role IN ('user', 'viewer', 'operator', 'admin')"
    )


def downgrade() -> None:
    # 回退前必须先把新角色降级，否则约束建不回来
    op.execute("UPDATE users SET role = 'user' WHERE role IN ('viewer', 'operator')")
    op.drop_constraint("ck_users_role", "users", type_="check")
    op.create_check_constraint("ck_users_role", "users", "role IN ('user', 'admin')")

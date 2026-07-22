"""Add an independent IP login throttle and scope-key constraints.

Revision ID: 20260722_0006
Revises: 20260722_0005
Create Date: 2026-07-22
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260722_0006"
down_revision: str | None = "20260722_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    invalid_count = connection.execute(
        sa.text(
            "SELECT COALESCE(SUM(CASE WHEN "
            "(scope = 'email' AND email_hash IS NOT NULL AND client_ip_hash IS NULL) OR "
            "(scope = 'email_ip' AND email_hash IS NOT NULL AND client_ip_hash IS NOT NULL) "
            "THEN 0 ELSE 1 END), 0) FROM login_throttles"
        )
    ).scalar_one()
    if invalid_count:
        raise RuntimeError(
            "login_throttles contains "
            f"{invalid_count} invalid legacy scope/key row(s); refusing migration"
        )

    with op.batch_alter_table("login_throttles") as batch_op:
        batch_op.drop_constraint("ck_login_throttles_scope", type_="check")
        batch_op.alter_column(
            "email_hash",
            existing_type=sa.String(length=64),
            nullable=True,
        )
        batch_op.create_check_constraint(
            "ck_login_throttles_scope",
            "scope IN ('email', 'email_ip', 'ip')",
        )
        batch_op.create_check_constraint(
            "ck_login_throttles_scope_keys",
            "(scope = 'email' AND email_hash IS NOT NULL AND client_ip_hash IS NULL) OR "
            "(scope = 'email_ip' AND email_hash IS NOT NULL AND client_ip_hash IS NOT NULL) OR "
            "(scope = 'ip' AND email_hash IS NULL AND client_ip_hash IS NOT NULL)",
        )
        batch_op.create_index(
            "ix_login_throttles_ip_scope", ["client_ip_hash", "scope"]
        )


def downgrade() -> None:
    # Older schemas cannot represent independent IP rows. Refuse to erase
    # security history silently; operators must back up and explicitly clean
    # the rows before requesting this downgrade.
    connection = op.get_bind()
    ip_count = connection.execute(
        sa.text("SELECT COUNT(*) FROM login_throttles WHERE scope = 'ip'")
    ).scalar_one()
    if ip_count:
        raise RuntimeError(
            "login_throttles contains independent IP rows; refusing destructive downgrade"
        )
    with op.batch_alter_table("login_throttles") as batch_op:
        batch_op.drop_index("ix_login_throttles_ip_scope")
        batch_op.drop_constraint("ck_login_throttles_scope_keys", type_="check")
        batch_op.drop_constraint("ck_login_throttles_scope", type_="check")
        batch_op.alter_column(
            "email_hash",
            existing_type=sa.String(length=64),
            nullable=False,
        )
        batch_op.create_check_constraint(
            "ck_login_throttles_scope", "scope IN ('email', 'email_ip')"
        )

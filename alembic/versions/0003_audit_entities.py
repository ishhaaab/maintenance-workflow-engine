"""Extend existing machine audit without fabricating historical human events."""

import sqlalchemy as sa

from alembic import op

revision = "0003_audit_entities"
down_revision = "0002_identity_sessions"
branch_labels = depends_on = None


def upgrade():
    op.alter_column("audit_records", "workflow_id", existing_type=sa.Uuid(), nullable=True)
    op.add_column(
        "audit_records",
        sa.Column("actor_type", sa.String(16), nullable=False, server_default="SYSTEM"),
    )
    op.add_column("audit_records", sa.Column("actor_membership_id", sa.Uuid()))
    op.add_column(
        "audit_records",
        sa.Column("entity_type", sa.String(32), nullable=False, server_default="execution"),
    )
    op.add_column("audit_records", sa.Column("entity_id", sa.Uuid()))
    op.add_column("audit_records", sa.Column("before_revision", sa.BigInteger()))
    op.add_column("audit_records", sa.Column("before_state", sa.String(32)))
    op.add_column("audit_records", sa.Column("after_state", sa.String(32)))
    op.create_foreign_key("fk_audit_tenant", "audit_records", "tenants", ["tenant_id"], ["id"])
    op.create_foreign_key(
        "fk_audit_membership",
        "audit_records",
        "tenant_memberships",
        ["tenant_id", "actor_membership_id"],
        ["tenant_id", "id"],
    )
    op.create_check_constraint(
        "ck_audit_actor_type", "audit_records", "actor_type IN ('USER', 'SYSTEM', 'OPERATOR')"
    )
    op.create_index(
        "ix_audit_entity_time",
        "audit_records",
        ["tenant_id", "entity_id", "occurred_at", "audit_id"],
    )


def downgrade():
    raise RuntimeError("Audit evidence must not be destructively downgraded")

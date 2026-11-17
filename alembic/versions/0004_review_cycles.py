"""Draft cycle membership, ownership, and idempotent command receipts."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "0004_review_cycles"
down_revision = "0003_audit_entities"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "review_cycles",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("creator_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("tenant_id", "id", name="uq_cycle_tenant"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "creator_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.id"],
            name="fk_cycle_creator",
        ),
        sa.CheckConstraint("state IN ('DRAFT', 'ACTIVE', 'COMPLETED')", name="ck_cycle_state"),
        sa.CheckConstraint(
            "revision >= 0 AND length(btrim(title)) BETWEEN 1 AND 160", name="ck_cycle_values"
        ),
        sa.CheckConstraint(
            "(state = 'COMPLETED') = (completed_at IS NOT NULL)", name="ck_cycle_completion"
        ),
    )
    op.create_index(
        "ix_cycle_tenant_state", "review_cycles", ["tenant_id", "state", "created_at", "id"]
    )
    op.create_table(
        "cycle_requesters",
        sa.Column("tenant_id", sa.String(128), primary_key=True),
        sa.Column("cycle_id", sa.Uuid(), primary_key=True),
        sa.Column("membership_id", sa.Uuid(), primary_key=True),
        sa.ForeignKeyConstraint(
            ["tenant_id", "cycle_id"],
            ["review_cycles.tenant_id", "review_cycles.id"],
            name="fk_requester_cycle",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "membership_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.id"],
            name="fk_cycle_requester",
        ),
    )
    op.create_table(
        "review_items",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("cycle_id", sa.Uuid(), nullable=False),
        sa.Column("requester_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("tenant_id", "id", name="uq_item_tenant"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "cycle_id"],
            ["review_cycles.tenant_id", "review_cycles.id"],
            name="fk_item_cycle",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "cycle_id", "requester_id"],
            [
                "cycle_requesters.tenant_id",
                "cycle_requesters.cycle_id",
                "cycle_requesters.membership_id",
            ],
            name="fk_item_requester",
        ),
        sa.CheckConstraint("revision >= 0", name="ck_item_revision"),
    )
    op.create_index("ix_item_cycle", "review_items", ["tenant_id", "cycle_id", "id"])
    op.create_table(
        "command_receipts",
        sa.Column("tenant_id", sa.String(128), primary_key=True),
        sa.Column("actor_id", sa.Uuid(), primary_key=True),
        sa.Column("kind", sa.String(32), primary_key=True),
        sa.Column("command_id", sa.Uuid(), primary_key=True),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("result", JSONB, nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "actor_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.id"],
            name="fk_receipt_actor",
        ),
        sa.CheckConstraint("octet_length(result::text) <= 16384", name="ck_receipt_budget"),
    )
    role = (
        op.get_bind()
        .execute(sa.text("SELECT current_setting('workflow.runtime_role')"))
        .scalar_one()
    )
    role = op.get_bind().dialect.identifier_preparer.quote(role)
    for table in ("review_cycles", "cycle_requesters", "review_items", "command_receipts"):
        op.execute(f"REVOKE ALL ON {table} FROM PUBLIC")
        op.execute(f"GRANT SELECT, INSERT ON {table} TO {role}")
    op.execute(f"GRANT UPDATE ON review_cycles, review_items TO {role}")


def downgrade():
    raise RuntimeError("Review evidence must not be destructively downgraded")

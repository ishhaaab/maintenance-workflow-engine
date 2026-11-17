"""Repeat-safe local demo provisioning journal. Contains no passwords."""

import sqlalchemy as sa

from alembic import op

revision = "0007_demo_seed_records"
down_revision = "0006_reminder_jobs"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "demo_seed_records",
        sa.Column("name", sa.String(64), primary_key=True),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("manifest", sa.dialects.postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("state IN ('STARTED', 'COMPLETE')", name="ck_demo_seed_state"),
        sa.CheckConstraint("octet_length(manifest::text) <= 131072", name="ck_demo_seed_budget"),
    )
    role = (
        op.get_bind()
        .execute(sa.text("SELECT current_setting('workflow.runtime_role')"))
        .scalar_one()
    )
    role = op.get_bind().dialect.identifier_preparer.quote(role)
    op.execute("REVOKE ALL ON demo_seed_records FROM PUBLIC")
    op.execute(f"GRANT SELECT,INSERT,UPDATE ON demo_seed_records TO {role}")


def downgrade():
    raise RuntimeError("Demo provisioning provenance must not be destructively downgraded")

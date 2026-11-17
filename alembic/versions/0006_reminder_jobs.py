"""Durable reminder intent, including existing open steps exactly once."""

import sqlalchemy as sa

from alembic import op

revision = "0006_reminder_jobs"
down_revision = "0005_review_versions"
branch_labels = depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE reminder_jobs (
        id uuid PRIMARY KEY, tenant_id varchar(128) NOT NULL, version_id uuid NOT NULL,
        level integer NOT NULL, recipient_id uuid NOT NULL, deadline_at timestamptz NOT NULL,
        state varchar(16) NOT NULL, attempts integer NOT NULL, next_attempt_at timestamptz NOT NULL,
        lease_token varchar(64), lease_until timestamptz, delivered_at timestamptz, failed_at timestamptz,
        last_error varchar(64),
        CONSTRAINT fk_reminder_step FOREIGN KEY(tenant_id,version_id,level,recipient_id) REFERENCES version_steps(tenant_id,version_id,level,reviewer_id),
        CONSTRAINT uq_reminder_step UNIQUE(tenant_id,version_id,level),
        CONSTRAINT ck_reminder_state CHECK(state IN ('PENDING','IN_FLIGHT','SENT','CANCELLED','FAILED') AND attempts BETWEEN 0 AND 5),
        CONSTRAINT ck_reminder_lease CHECK((state='IN_FLIGHT' AND lease_token IS NOT NULL AND lease_until IS NOT NULL) OR (state<>'IN_FLIGHT' AND lease_token IS NULL AND lease_until IS NULL))
    );
    CREATE INDEX ix_reminder_due ON reminder_jobs(state,next_attempt_at,lease_until);
    INSERT INTO reminder_jobs(id,tenant_id,version_id,level,recipient_id,deadline_at,state,attempts,next_attempt_at)
    SELECT gen_random_uuid(), p.tenant_id, p.version_id, p.current_level, s.reviewer_id, p.deadline_at, 'PENDING', 0, p.deadline_at
    FROM review_progress p JOIN review_items i ON i.current_version_id=p.version_id AND i.tenant_id=p.tenant_id
    JOIN review_cycles c ON c.id=i.cycle_id AND c.tenant_id=i.tenant_id
    JOIN version_steps s ON s.version_id=p.version_id AND s.tenant_id=p.tenant_id AND s.level=p.current_level
    WHERE c.state='ACTIVE' AND p.status='PENDING' AND p.deadline_at IS NOT NULL
    ON CONFLICT(tenant_id,version_id,level) DO NOTHING;
    INSERT INTO audit_records(audit_id,tenant_id,actor,event,correlation_id,revision,occurred_at,safe_metadata,entity_type,entity_id)
    SELECT gen_random_uuid(),tenant_id,'system','reminder_scheduled',id::text,0,clock_timestamp(),'{"source":"migration"}'::jsonb,'reminder',id FROM reminder_jobs;
    """)
    role = (
        op.get_bind()
        .execute(sa.text("SELECT current_setting('workflow.runtime_role')"))
        .scalar_one()
    )
    role = op.get_bind().dialect.identifier_preparer.quote(role)
    op.execute("REVOKE ALL ON reminder_jobs FROM PUBLIC")
    op.execute(f"GRANT SELECT,INSERT,UPDATE ON reminder_jobs TO {role}")


def downgrade():
    raise RuntimeError("Reminder evidence must not be destructively downgraded")

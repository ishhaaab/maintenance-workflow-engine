"""Relational execution foundation."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0001_relational_execution"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    runtime = connection.execute(
        sa.text("SELECT current_setting('workflow.runtime_role', true)")
    ).scalar_one_or_none()
    if not runtime:
        raise RuntimeError("Bootstrap must configure a distinct runtime role before migration")
    privileges = connection.execute(
        sa.text(
            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls, "
            "pg_has_role(rolname, current_user, 'MEMBER') FROM pg_roles WHERE rolname = :runtime"
        ),
        {"runtime": runtime},
    ).one_or_none()
    if privileges != (False, False, False, False, False, False):
        raise RuntimeError("Runtime role must not own or inherit the migration role")

    op.execute("""
    CREATE FUNCTION public.workflow_execution_payload_valid(value jsonb)
    RETURNS boolean LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
    SET search_path = pg_catalog AS $$
    DECLARE
        action jsonb;
        entry jsonb;
        position integer := 0;
    BEGIN
        IF jsonb_typeof(value) IS DISTINCT FROM 'object' THEN RETURN false; END IF;
        IF NOT (value ?& ARRAY['request_text', 'plan', 'clarification', 'catalog_version',
            'prompt_version', 'model_metadata', 'actions', 'planning_attempts',
            'retry_phase', 'history']) OR
            value - ARRAY['request_text', 'plan', 'clarification', 'catalog_version',
            'prompt_version', 'model_metadata', 'actions', 'planning_attempts',
            'retry_phase', 'history'] <> '{}'::jsonb THEN RETURN false; END IF;
        IF jsonb_typeof(value->'request_text') <> 'string' OR
            length(value->>'request_text') NOT BETWEEN 1 AND 2000 OR
            btrim(value->>'request_text') = '' OR
            jsonb_typeof(value->'plan') NOT IN ('object', 'null') OR
            jsonb_typeof(value->'clarification') NOT IN ('object', 'null') OR
            jsonb_typeof(value->'catalog_version') NOT IN ('string', 'null') OR
            jsonb_typeof(value->'prompt_version') NOT IN ('string', 'null') OR
            jsonb_typeof(value->'model_metadata') <> 'object' OR
            jsonb_typeof(value->'planning_attempts') <> 'number' OR
            (value->>'planning_attempts') !~ '^[0-2]$' OR
            (value->'retry_phase' <> 'null'::jsonb AND
                value->>'retry_phase' NOT IN ('planning', 'action')) THEN RETURN false; END IF;
        IF jsonb_typeof(value->'actions') <> 'array' OR
            jsonb_typeof(value->'history') <> 'array' THEN RETURN false; END IF;
        IF jsonb_array_length(value->'actions') > 2 OR
            jsonb_array_length(value->'history') > 256 THEN RETURN false; END IF;
        FOR action IN SELECT jsonb_array_elements(value->'actions') LOOP
            IF jsonb_typeof(action) IS DISTINCT FROM 'object' THEN RETURN false; END IF;
            IF NOT (action ?& ARRAY['action_id', 'arguments', 'idempotency_key', 'status',
                'submissions', 'reconciliation_count', 'has_ambiguous_submission',
                'next_retry_at', 'result', 'last_error']) OR
                action - ARRAY['action_id', 'arguments', 'idempotency_key', 'status',
                'submissions', 'reconciliation_count', 'has_ambiguous_submission',
                'next_retry_at', 'result', 'last_error'] <> '{}'::jsonb THEN RETURN false; END IF;
            IF action->>'action_id' IS DISTINCT FROM
                (CASE WHEN position = 0 THEN 'ticket' ELSE 'reservation' END) OR
                jsonb_typeof(action->'action_id') <> 'string' OR
                jsonb_typeof(action->'arguments') <> 'object' OR
                jsonb_typeof(action->'idempotency_key') <> 'string' OR
                length(action->>'idempotency_key') NOT BETWEEN 1 AND 128 OR
                jsonb_typeof(action->'status') <> 'string' OR
                action->>'status' NOT IN
                    ('PENDING', 'IN_FLIGHT', 'CONFIRMED', 'SKIPPED', 'FAILED', 'UNKNOWN') OR
                jsonb_typeof(action->'submissions') <> 'number' OR
                (action->>'submissions') !~ '^[0-5]$' OR
                jsonb_typeof(action->'reconciliation_count') <> 'number' OR
                (action->>'reconciliation_count') !~ '^[0-5]$' OR
                jsonb_typeof(action->'has_ambiguous_submission') <> 'boolean' OR
                jsonb_typeof(action->'next_retry_at') NOT IN ('string', 'null') OR
                (jsonb_typeof(action->'next_retry_at') = 'string' AND
                    action->>'next_retry_at' !~ '(Z|[+]00:00)$') OR
                jsonb_typeof(action->'result') NOT IN ('object', 'null') OR
                jsonb_typeof(action->'last_error') NOT IN ('string', 'null') OR
                (jsonb_typeof(action->'last_error') = 'string' AND
                    length(action->>'last_error') NOT BETWEEN 1 AND 2000)
                THEN RETURN false; END IF;
            IF position = 1 AND action->>'status' <> 'PENDING' AND
                value #>> '{actions,0,status}' <> 'CONFIRMED' THEN RETURN false; END IF;
            position := position + 1;
        END LOOP;
        FOR entry IN SELECT jsonb_array_elements(value->'history') LOOP
            IF jsonb_typeof(entry) IS DISTINCT FROM 'object' THEN RETURN false; END IF;
            IF NOT (entry ?& ARRAY['at', 'event', 'action_id', 'error']) OR
                entry - ARRAY['at', 'event', 'action_id', 'error'] <> '{}'::jsonb
                THEN RETURN false; END IF;
            IF jsonb_typeof(entry->'at') <> 'string' OR entry->>'at' !~ '(Z|[+]00:00)$' OR
                jsonb_typeof(entry->'event') <> 'string' OR length(entry->>'event') > 64 OR
                jsonb_typeof(entry->'action_id') NOT IN ('string', 'null') OR
                jsonb_typeof(entry->'error') NOT IN ('string', 'null') OR
                (jsonb_typeof(entry->'error') = 'string' AND
                    length(entry->>'error') NOT BETWEEN 1 AND 2000) THEN RETURN false; END IF;
        END LOOP;
        RETURN true;
    END;
    $$
    """)
    op.execute("""
    CREATE FUNCTION public.workflow_audit_metadata_valid(value jsonb)
    RETURNS boolean LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE
    SET search_path = pg_catalog AS $$
    DECLARE
        key text;
        item jsonb;
        content text;
    BEGIN
        IF jsonb_typeof(value) IS DISTINCT FROM 'object' THEN RETURN false; END IF;
        FOR key, item IN SELECT * FROM jsonb_each(value) LOOP
            content := item #>> '{}';
            IF key IN ('attempt', 'wake_seq') THEN
                IF jsonb_typeof(item) <> 'number' OR content !~ '^[0-9]{1,18}$'
                    THEN RETURN false; END IF;
                IF key = 'attempt' AND content::numeric > 5 THEN RETURN false; END IF;
            ELSE
                IF jsonb_typeof(item) <> 'string' THEN RETURN false; END IF;
                CASE key
                    WHEN 'source' THEN
                        IF content NOT IN ('api', 'runner', 'dispatcher', 'recovery',
                            'planner', 'system', 'migration') THEN RETURN false; END IF;
                    WHEN 'action_id' THEN
                        IF content NOT IN ('ticket', 'reservation') THEN RETURN false; END IF;
                    WHEN 'state' THEN
                        IF content NOT IN ('PENDING_PLAN', 'NEEDS_INPUT', 'AWAITING_APPROVAL',
                            'READY', 'RUNNING', 'RETRY_WAIT', 'RECONCILING', 'SUCCEEDED',
                            'FAILED', 'CANCELLED', 'NEEDS_REVIEW') THEN RETURN false; END IF;
                    WHEN 'reason_code' THEN
                        IF content !~ '^[a-z][a-z0-9_]{0,63}$' THEN RETURN false; END IF;
                    WHEN 'outcome' THEN
                        IF content NOT IN ('created', 'existing', 'confirmed', 'skipped',
                            'retry', 'failed', 'cancelled', 'needs_review', 'claimed',
                            'released', 'renewed', 'lease_lost') THEN RETURN false; END IF;
                    ELSE RETURN false;
                END CASE;
            END IF;
        END LOOP;
        RETURN true;
    END;
    $$
    """)
    op.create_table(
        "execution_runs",
        sa.Column("workflow_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("admission_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.SmallInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("plan_hash", sa.String(64)),
        sa.Column("approved_plan_hash", sa.String(64)),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True)),
        sa.Column("wake_seq", sa.BigInteger(), nullable=False),
        sa.Column("last_enqueued_at", sa.DateTime(timezone=True)),
        sa.Column("lease_owner", sa.String(128)),
        sa.Column("lease_token", sa.String(128)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.PrimaryKeyConstraint("workflow_id"),
        sa.UniqueConstraint("tenant_id", "workflow_id", name="uq_execution_tenant_workflow"),
        sa.UniqueConstraint("tenant_id", "admission_key", name="uq_execution_tenant_admission"),
        sa.CheckConstraint(
            "length(tenant_id) BETWEEN 1 AND 128 AND length(admission_key) BETWEEN 1 AND 128",
            name="ck_execution_identity",
        ),
        sa.CheckConstraint(
            "request_hash ~ '^[a-f0-9]{64}$' AND "
            "(plan_hash IS NULL OR plan_hash ~ '^[a-f0-9]{64}$') AND "
            "(approved_plan_hash IS NULL OR approved_plan_hash ~ '^[a-f0-9]{64}$')",
            name="ck_execution_hashes",
        ),
        sa.CheckConstraint("schema_version = 1", name="ck_execution_schema_version"),
        sa.CheckConstraint("revision >= 0", name="ck_execution_revision"),
        sa.CheckConstraint("wake_seq >= 0", name="ck_execution_wake_seq"),
        sa.CheckConstraint("updated_at >= created_at", name="ck_execution_timestamps"),
        sa.CheckConstraint(
            "state IN ('PENDING_PLAN', 'NEEDS_INPUT', 'AWAITING_APPROVAL', "
            "'READY', 'RUNNING', 'RETRY_WAIT', 'RECONCILING', 'SUCCEEDED', "
            "'FAILED', 'CANCELLED', 'NEEDS_REVIEW')",
            name="ck_execution_state",
        ),
        sa.CheckConstraint(
            "state NOT IN ('NEEDS_INPUT', 'AWAITING_APPROVAL', 'SUCCEEDED', "
            "'FAILED', 'CANCELLED', 'NEEDS_REVIEW') OR next_run_at IS NULL",
            name="ck_execution_passive_schedule",
        ),
        sa.CheckConstraint(
            "(approved_plan_hash IS NULL AND approved_at IS NULL) OR "
            "(approved_plan_hash IS NOT NULL AND approved_at IS NOT NULL AND "
            "plan_hash IS NOT NULL AND approved_plan_hash = plan_hash)",
            name="ck_execution_approval",
        ),
        sa.CheckConstraint(
            "(lease_owner IS NULL AND lease_token IS NULL AND lease_until IS NULL) OR "
            "(lease_owner IS NOT NULL AND length(lease_owner) BETWEEN 1 AND 128 AND "
            "lease_token IS NOT NULL AND length(lease_token) BETWEEN 1 AND 128 AND "
            "lease_until IS NOT NULL)",
            name="ck_execution_lease",
        ),
        sa.CheckConstraint(
            "public.workflow_execution_payload_valid(payload)", name="ck_execution_payload"
        ),
        # Defense only: 64x the compact Workflow budget covers JSONB whitespace and
        # finite binary64 exponent expansion. The authoritative full budget stays 256 KiB.
        sa.CheckConstraint("octet_length(payload::text) <= 16777216", name="ck_execution_budget"),
        sa.CheckConstraint(
            "(COALESCE(payload #>> '{actions,0,status}', 'PENDING') = 'PENDING' AND "
            "COALESCE(payload #>> '{actions,1,status}', 'PENDING') = 'PENDING') OR "
            "(plan_hash IS NOT NULL AND approved_plan_hash IS NOT NULL AND "
            "approved_plan_hash = plan_hash AND approved_at IS NOT NULL)",
            name="ck_execution_action_approval",
        ),
    )
    op.create_index("ix_execution_tenant_created", "execution_runs", ["tenant_id", "created_at"])
    op.create_index("ix_execution_due", "execution_runs", ["state", "next_run_at", "lease_until"])
    op.create_table(
        "audit_records",
        sa.Column("audit_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.String(128), nullable=False),
        sa.Column("workflow_id", sa.Uuid(), nullable=False),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("event", sa.String(64), nullable=False),
        sa.Column("correlation_id", sa.String(128), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("safe_metadata", postgresql.JSONB(), nullable=False),
        sa.PrimaryKeyConstraint("audit_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "workflow_id"],
            ["execution_runs.tenant_id", "execution_runs.workflow_id"],
            name="fk_audit_execution",
        ),
        sa.CheckConstraint("length(actor) BETWEEN 1 AND 128", name="ck_audit_actor"),
        sa.CheckConstraint(
            "event ~ '^[a-z][a-z0-9_]{0,63}$' AND length(correlation_id) BETWEEN 1 AND 128",
            name="ck_audit_identity",
        ),
        sa.CheckConstraint("revision >= 0", name="ck_audit_revision"),
        sa.CheckConstraint(
            "public.workflow_audit_metadata_valid(safe_metadata)", name="ck_audit_metadata"
        ),
        sa.CheckConstraint("octet_length(safe_metadata::text) <= 4096", name="ck_audit_budget"),
    )
    op.create_index(
        "ix_audit_tenant_workflow_time",
        "audit_records",
        ["tenant_id", "workflow_id", "occurred_at"],
    )
    role = connection.dialect.identifier_preparer.quote(runtime)
    op.execute("REVOKE ALL ON execution_runs, audit_records, alembic_version FROM PUBLIC")
    op.execute(f"REVOKE ALL ON execution_runs, audit_records, alembic_version FROM {role}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON execution_runs TO {role}")
    op.execute(f"GRANT SELECT, INSERT ON audit_records TO {role}")
    op.execute(f"GRANT SELECT ON alembic_version TO {role}")
    for function in ("workflow_execution_payload_valid", "workflow_audit_metadata_valid"):
        op.execute(f"REVOKE ALL ON FUNCTION public.{function}(jsonb) FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION public.{function}(jsonb) TO {role}")


def downgrade() -> None:
    op.drop_table("audit_records")
    op.drop_table("execution_runs")
    op.execute("DROP FUNCTION public.workflow_audit_metadata_valid(jsonb)")
    op.execute("DROP FUNCTION public.workflow_execution_payload_valid(jsonb)")

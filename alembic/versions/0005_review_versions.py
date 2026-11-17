"""Immutable version-bound sequential review and exact-plan execution links."""

import sqlalchemy as sa

from alembic import op

revision = "0005_review_versions"
down_revision = "0004_review_cycles"
branch_labels = depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE approval_definitions (
        id uuid PRIMARY KEY, tenant_id varchar(128) NOT NULL, item_id uuid NOT NULL,
        level integer NOT NULL, reviewer_id uuid NOT NULL, due_after_s integer NOT NULL,
        CONSTRAINT fk_definition_item FOREIGN KEY (tenant_id,item_id) REFERENCES review_items(tenant_id,id),
        CONSTRAINT fk_definition_reviewer FOREIGN KEY (tenant_id,reviewer_id) REFERENCES tenant_memberships(tenant_id,id),
        CONSTRAINT uq_definition_level UNIQUE (tenant_id,item_id,level),
        CONSTRAINT uq_definition_reviewer UNIQUE (tenant_id,item_id,reviewer_id),
        CONSTRAINT ck_definition_bounds CHECK (level BETWEEN 1 AND 5 AND due_after_s BETWEEN 1 AND 2592000)
    );
    CREATE TABLE item_versions (
        id uuid PRIMARY KEY, tenant_id varchar(128) NOT NULL, item_id uuid NOT NULL,
        number integer NOT NULL, submitted_by uuid NOT NULL, predecessor_id uuid,
        request_text varchar(2000) NOT NULL, request_hash varchar(64) NOT NULL,
        definition_hash varchar(64) NOT NULL, catalog jsonb NOT NULL, created_at timestamptz NOT NULL,
        CONSTRAINT uq_version_tenant UNIQUE (tenant_id,id),
        CONSTRAINT uq_version_item UNIQUE (tenant_id,item_id,id),
        CONSTRAINT uq_version_number UNIQUE (tenant_id,item_id,number),
        CONSTRAINT fk_version_item FOREIGN KEY (tenant_id,item_id) REFERENCES review_items(tenant_id,id),
        CONSTRAINT fk_version_submitter FOREIGN KEY (tenant_id,submitted_by) REFERENCES tenant_memberships(tenant_id,id),
        CONSTRAINT fk_version_predecessor FOREIGN KEY (tenant_id,item_id,predecessor_id) REFERENCES item_versions(tenant_id,item_id,id),
        CONSTRAINT ck_version_content CHECK (number > 0 AND length(btrim(request_text)) BETWEEN 1 AND 2000),
        CONSTRAINT ck_version_hashes CHECK (request_hash ~ '^[a-f0-9]{64}$' AND definition_hash ~ '^[a-f0-9]{64}$'),
        CONSTRAINT ck_version_catalog CHECK (octet_length(catalog::text) <= 131072)
    );
    CREATE TABLE version_steps (
        id uuid PRIMARY KEY, tenant_id varchar(128) NOT NULL, version_id uuid NOT NULL,
        level integer NOT NULL, reviewer_id uuid NOT NULL, due_after_s integer NOT NULL,
        CONSTRAINT fk_step_version FOREIGN KEY (tenant_id,version_id) REFERENCES item_versions(tenant_id,id),
        CONSTRAINT fk_step_reviewer FOREIGN KEY (tenant_id,reviewer_id) REFERENCES tenant_memberships(tenant_id,id),
        CONSTRAINT uq_step_level UNIQUE (tenant_id,version_id,level),
        CONSTRAINT uq_step_reviewer UNIQUE (tenant_id,version_id,reviewer_id),
        CONSTRAINT uq_step_identity UNIQUE (tenant_id,version_id,level,reviewer_id),
        CONSTRAINT ck_step_bounds CHECK (level BETWEEN 1 AND 5 AND due_after_s BETWEEN 1 AND 2592000)
    );
    CREATE TABLE review_progress (
        version_id uuid PRIMARY KEY, tenant_id varchar(128) NOT NULL, status varchar(32) NOT NULL,
        revision bigint NOT NULL, current_level integer, opened_at timestamptz, deadline_at timestamptz,
        CONSTRAINT uq_progress_tenant UNIQUE (tenant_id,version_id),
        CONSTRAINT fk_progress_version FOREIGN KEY (tenant_id,version_id) REFERENCES item_versions(tenant_id,id),
        CONSTRAINT ck_progress_status CHECK (status IN ('PREPARING','NEEDS_INPUT','PLAN_FAILED','PENDING','CHANGES_REQUESTED','APPROVED','REJECTED','SUPERSEDED')),
        CONSTRAINT ck_progress_revision CHECK (revision >= 0 AND (current_level IS NULL OR current_level BETWEEN 1 AND 5)),
        CONSTRAINT ck_progress_deadline CHECK ((current_level IS NULL AND opened_at IS NULL AND deadline_at IS NULL) OR (status='PENDING' AND current_level IS NOT NULL AND opened_at IS NOT NULL AND deadline_at > opened_at))
    );
    CREATE INDEX ix_progress_deadline ON review_progress(tenant_id,status,deadline_at);
    ALTER TABLE review_items ADD COLUMN current_version_id uuid;
    ALTER TABLE review_items ADD CONSTRAINT fk_item_current_version FOREIGN KEY (tenant_id,id,current_version_id)
        REFERENCES item_versions(tenant_id,item_id,id) DEFERRABLE INITIALLY DEFERRED;
    ALTER TABLE execution_runs ADD COLUMN item_version_id uuid;
    ALTER TABLE execution_runs ADD COLUMN approved_plan_id uuid;
    ALTER TABLE execution_runs ADD CONSTRAINT uq_execution_version UNIQUE(tenant_id,item_version_id);
    ALTER TABLE execution_runs ADD CONSTRAINT uq_execution_review_identity UNIQUE(tenant_id,item_version_id,workflow_id);
    ALTER TABLE execution_runs ADD CONSTRAINT fk_execution_version FOREIGN KEY (tenant_id,item_version_id) REFERENCES item_versions(tenant_id,id);
    CREATE TABLE validated_plans (
        id uuid PRIMARY KEY, tenant_id varchar(128) NOT NULL, version_id uuid NOT NULL, run_id uuid NOT NULL,
        plan_hash varchar(64) NOT NULL, schema_version integer NOT NULL, catalog_version varchar(128) NOT NULL,
        plan jsonb NOT NULL, actions jsonb NOT NULL, created_at timestamptz NOT NULL,
        CONSTRAINT uq_plan_version UNIQUE(tenant_id,version_id), CONSTRAINT uq_plan_run UNIQUE(tenant_id,run_id),
        CONSTRAINT uq_plan_identity UNIQUE(tenant_id,version_id,id), CONSTRAINT uq_plan_hash UNIQUE(tenant_id,version_id,plan_hash),
        CONSTRAINT fk_plan_run FOREIGN KEY (tenant_id,version_id,run_id) REFERENCES execution_runs(tenant_id,item_version_id,workflow_id),
        CONSTRAINT ck_plan_hash CHECK (plan_hash ~ '^[a-f0-9]{64}$' AND schema_version=1),
        CONSTRAINT ck_plan_budget CHECK (octet_length(plan::text)+octet_length(actions::text) <= 262144)
    );
    ALTER TABLE execution_runs ADD CONSTRAINT fk_execution_plan FOREIGN KEY (tenant_id,item_version_id,approved_plan_id) REFERENCES validated_plans(tenant_id,version_id,id);
    ALTER TABLE execution_runs ADD CONSTRAINT ck_execution_review_approval CHECK (item_version_id IS NULL OR approved_plan_hash IS NULL OR approved_plan_id IS NOT NULL);
    CREATE TABLE decisions (
        id uuid PRIMARY KEY, tenant_id varchar(128) NOT NULL, version_id uuid NOT NULL, level integer NOT NULL,
        actor_id uuid NOT NULL, plan_hash varchar(64) NOT NULL, kind varchar(24) NOT NULL,
        comment varchar(2000) NOT NULL, created_at timestamptz NOT NULL, command_id uuid NOT NULL, payload_hash varchar(64) NOT NULL,
        CONSTRAINT fk_decision_step FOREIGN KEY (tenant_id,version_id,level,actor_id) REFERENCES version_steps(tenant_id,version_id,level,reviewer_id),
        CONSTRAINT fk_decision_plan FOREIGN KEY (tenant_id,version_id,plan_hash) REFERENCES validated_plans(tenant_id,version_id,plan_hash),
        CONSTRAINT uq_decision_level UNIQUE(tenant_id,version_id,level), CONSTRAINT uq_decision_command UNIQUE(tenant_id,actor_id,command_id),
        CONSTRAINT ck_decision_comment CHECK (kind IN ('APPROVE','REJECT','REQUEST_CHANGES') AND length(comment)<=2000 AND (kind='APPROVE' OR comment ~ '[^[:space:]]'))
    );
    """)
    op.execute("""
    CREATE FUNCTION public.workflow_immutable_review() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
    BEGIN RAISE EXCEPTION 'immutable review evidence' USING ERRCODE='23514'; END $$;
    CREATE FUNCTION public.workflow_draft_definition() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
    DECLARE target uuid; state text;
    BEGIN
        IF TG_OP='DELETE' THEN target := OLD.item_id; ELSE target := NEW.item_id; END IF;
        SELECT c.state INTO state FROM public.review_items i JOIN public.review_cycles c ON c.id=i.cycle_id AND c.tenant_id=i.tenant_id WHERE i.id=target FOR UPDATE OF c;
        IF state IS DISTINCT FROM 'DRAFT' THEN RAISE EXCEPTION 'cycle configuration frozen' USING ERRCODE='23514'; END IF;
        IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
    END $$;
    CREATE TRIGGER freeze_approval_definition BEFORE INSERT OR UPDATE OR DELETE ON approval_definitions
        FOR EACH ROW EXECUTE FUNCTION public.workflow_draft_definition();
    CREATE FUNCTION public.workflow_review_identity() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $$
    BEGIN
        IF (NEW.id, NEW.tenant_id, NEW.cycle_id, NEW.requester_id) IS DISTINCT FROM
           (OLD.id, OLD.tenant_id, OLD.cycle_id, OLD.requester_id) THEN
           RAISE EXCEPTION 'immutable item ownership' USING ERRCODE='23514'; END IF;
        RETURN NEW;
    END $$;
    CREATE TRIGGER immutable_item_identity BEFORE UPDATE ON review_items FOR EACH ROW EXECUTE FUNCTION public.workflow_review_identity();
    """)
    for table in (
        "item_versions",
        "version_steps",
        "validated_plans",
        "decisions",
        "command_receipts",
    ):
        op.execute(
            f"CREATE TRIGGER immutable_{table} BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION public.workflow_immutable_review()"
        )
    role = (
        op.get_bind()
        .execute(sa.text("SELECT current_setting('workflow.runtime_role')"))
        .scalar_one()
    )
    role = op.get_bind().dialect.identifier_preparer.quote(role)
    for table in (
        "approval_definitions",
        "item_versions",
        "version_steps",
        "validated_plans",
        "review_progress",
        "decisions",
    ):
        op.execute(f"REVOKE ALL ON {table} FROM PUBLIC")
        op.execute(f"GRANT SELECT, INSERT ON {table} TO {role}")
    op.execute(f"GRANT DELETE ON approval_definitions TO {role}")
    op.execute(f"GRANT UPDATE ON review_progress TO {role}")
    for function in (
        "workflow_immutable_review",
        "workflow_draft_definition",
        "workflow_review_identity",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION public.{function}() FROM PUBLIC")


def downgrade():
    raise RuntimeError("Review evidence and execution links must not be destructively downgraded")

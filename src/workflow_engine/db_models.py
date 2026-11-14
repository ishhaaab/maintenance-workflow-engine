"""Relational row layout, not a repository or a second authority.

ExecutionRun columns map 1:1 to Workflow's identity, version, scheduling and lease
fields. `payload` contains exactly request_text, plan, clarification,
catalog_version, prompt_version, model_metadata, actions, planning_attempts,
retry_phase and history. Nested values use Workflow.model_dump(mode="json").
The future store must reconstruct/validate the full Workflow and its 256 KiB
projection budget, not just validate the JSONB portion in isolation.
The 16 MiB SQL text guard is only defense in depth: 64x accommodates JSONB's
whitespace and finite binary64 exponent expansion, not a larger Workflow budget.

AuditRecord is append-only for the runtime role. Its rows are not the bounded
256-entry execution history, and must never be trimmed with that history.
Its actor is an opaque machine identity, including the current lease owner or
the fallback "system", not a human actor/entity reference.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ExecutionRun(Base):
    __tablename__ = "execution_runs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "workflow_id", name="uq_execution_tenant_workflow"),
        UniqueConstraint("tenant_id", "admission_key", name="uq_execution_tenant_admission"),
        CheckConstraint(
            "length(tenant_id) BETWEEN 1 AND 128 AND length(admission_key) BETWEEN 1 AND 128",
            name="ck_execution_identity",
        ),
        CheckConstraint(
            "request_hash ~ '^[a-f0-9]{64}$' AND "
            "(plan_hash IS NULL OR plan_hash ~ '^[a-f0-9]{64}$') AND "
            "(approved_plan_hash IS NULL OR approved_plan_hash ~ '^[a-f0-9]{64}$')",
            name="ck_execution_hashes",
        ),
        CheckConstraint("schema_version = 1", name="ck_execution_schema_version"),
        CheckConstraint("revision >= 0", name="ck_execution_revision"),
        CheckConstraint("wake_seq >= 0", name="ck_execution_wake_seq"),
        CheckConstraint("updated_at >= created_at", name="ck_execution_timestamps"),
        CheckConstraint(
            "state IN ('PENDING_PLAN', 'NEEDS_INPUT', 'AWAITING_APPROVAL', "
            "'READY', 'RUNNING', 'RETRY_WAIT', 'RECONCILING', 'SUCCEEDED', "
            "'FAILED', 'CANCELLED', 'NEEDS_REVIEW')",
            name="ck_execution_state",
        ),
        CheckConstraint(
            "state NOT IN ('NEEDS_INPUT', 'AWAITING_APPROVAL', 'SUCCEEDED', "
            "'FAILED', 'CANCELLED', 'NEEDS_REVIEW') OR next_run_at IS NULL",
            name="ck_execution_passive_schedule",
        ),
        CheckConstraint(
            "(approved_plan_hash IS NULL AND approved_at IS NULL) OR "
            "(approved_plan_hash IS NOT NULL AND approved_at IS NOT NULL AND "
            "plan_hash IS NOT NULL AND approved_plan_hash = plan_hash)",
            name="ck_execution_approval",
        ),
        CheckConstraint(
            "(lease_owner IS NULL AND lease_token IS NULL AND lease_until IS NULL) OR "
            "(lease_owner IS NOT NULL AND length(lease_owner) BETWEEN 1 AND 128 AND "
            "lease_token IS NOT NULL AND length(lease_token) BETWEEN 1 AND 128 AND "
            "lease_until IS NOT NULL)",
            name="ck_execution_lease",
        ),
        CheckConstraint(
            "public.workflow_execution_payload_valid(payload)", name="ck_execution_payload"
        ),
        CheckConstraint("octet_length(payload::text) <= 16777216", name="ck_execution_budget"),
        CheckConstraint(
            "(COALESCE(payload #>> '{actions,0,status}', 'PENDING') = 'PENDING' AND "
            "COALESCE(payload #>> '{actions,1,status}', 'PENDING') = 'PENDING') OR "
            "(plan_hash IS NOT NULL AND approved_plan_hash IS NOT NULL AND "
            "approved_plan_hash = plan_hash AND approved_at IS NOT NULL)",
            name="ck_execution_action_approval",
        ),
        Index("ix_execution_tenant_created", "tenant_id", "created_at"),
        Index("ix_execution_due", "state", "next_run_at", "lease_until"),
    )

    workflow_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128))
    admission_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    schema_version: Mapped[int] = mapped_column(SmallInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revision: Mapped[int] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(String(32))
    plan_hash: Mapped[str | None] = mapped_column(String(64))
    approved_plan_hash: Mapped[str | None] = mapped_column(String(64))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payload: Mapped[dict] = mapped_column(JSONB)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    wake_seq: Mapped[int] = mapped_column(BigInteger)
    last_enqueued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_token: Mapped[str | None] = mapped_column(String(128))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditRecord(Base):
    __tablename__ = "audit_records"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "workflow_id"],
            ["execution_runs.tenant_id", "execution_runs.workflow_id"],
            name="fk_audit_execution",
        ),
        CheckConstraint("length(actor) BETWEEN 1 AND 128", name="ck_audit_actor"),
        CheckConstraint(
            "event ~ '^[a-z][a-z0-9_]{0,63}$' AND length(correlation_id) BETWEEN 1 AND 128",
            name="ck_audit_identity",
        ),
        CheckConstraint("revision >= 0", name="ck_audit_revision"),
        CheckConstraint(
            "public.workflow_audit_metadata_valid(safe_metadata)", name="ck_audit_metadata"
        ),
        CheckConstraint("octet_length(safe_metadata::text) <= 4096", name="ck_audit_budget"),
        Index("ix_audit_tenant_workflow_time", "tenant_id", "workflow_id", "occurred_at"),
    )

    audit_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128))
    workflow_id: Mapped[UUID] = mapped_column(Uuid)
    actor: Mapped[str] = mapped_column(String(128))
    event: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str] = mapped_column(String(128))
    revision: Mapped[int] = mapped_column(BigInteger)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    safe_metadata: Mapped[dict] = mapped_column(JSONB)

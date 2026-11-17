"""Immutable submissions, review definitions, sealed plans and named decisions."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .db_models import Base


class ApprovalDefinition(Base):
    __tablename__ = "approval_definitions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "item_id"],
            ["review_items.tenant_id", "review_items.id"],
            name="fk_definition_item",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "reviewer_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.id"],
            name="fk_definition_reviewer",
        ),
        UniqueConstraint("tenant_id", "item_id", "level", name="uq_definition_level"),
        UniqueConstraint("tenant_id", "item_id", "reviewer_id", name="uq_definition_reviewer"),
        CheckConstraint(
            "level BETWEEN 1 AND 5 AND due_after_s BETWEEN 1 AND 2592000",
            name="ck_definition_bounds",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128))
    item_id: Mapped[UUID] = mapped_column(Uuid)
    level: Mapped[int] = mapped_column(Integer)
    reviewer_id: Mapped[UUID] = mapped_column(Uuid)
    due_after_s: Mapped[int] = mapped_column(Integer)


class ItemVersion(Base):
    __tablename__ = "item_versions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_version_tenant"),
        UniqueConstraint("tenant_id", "item_id", "id", name="uq_version_item"),
        UniqueConstraint("tenant_id", "item_id", "number", name="uq_version_number"),
        ForeignKeyConstraint(
            ["tenant_id", "item_id"],
            ["review_items.tenant_id", "review_items.id"],
            name="fk_version_item",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "submitted_by"],
            ["tenant_memberships.tenant_id", "tenant_memberships.id"],
            name="fk_version_submitter",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "item_id", "predecessor_id"],
            ["item_versions.tenant_id", "item_versions.item_id", "item_versions.id"],
            name="fk_version_predecessor",
        ),
        CheckConstraint(
            "number > 0 AND length(btrim(request_text)) BETWEEN 1 AND 2000",
            name="ck_version_content",
        ),
        CheckConstraint(
            "request_hash ~ '^[a-f0-9]{64}$' AND definition_hash ~ '^[a-f0-9]{64}$'",
            name="ck_version_hashes",
        ),
        CheckConstraint("octet_length(catalog::text) <= 131072", name="ck_version_catalog"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128))
    item_id: Mapped[UUID] = mapped_column(Uuid)
    number: Mapped[int] = mapped_column(Integer)
    submitted_by: Mapped[UUID] = mapped_column(Uuid)
    predecessor_id: Mapped[UUID | None] = mapped_column(Uuid)
    request_text: Mapped[str] = mapped_column(String(2000))
    request_hash: Mapped[str] = mapped_column(String(64))
    definition_hash: Mapped[str] = mapped_column(String(64))
    catalog: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class VersionStep(Base):
    __tablename__ = "version_steps"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "version_id"],
            ["item_versions.tenant_id", "item_versions.id"],
            name="fk_step_version",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "reviewer_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.id"],
            name="fk_step_reviewer",
        ),
        UniqueConstraint("tenant_id", "version_id", "level", name="uq_step_level"),
        UniqueConstraint("tenant_id", "version_id", "reviewer_id", name="uq_step_reviewer"),
        UniqueConstraint(
            "tenant_id", "version_id", "level", "reviewer_id", name="uq_step_identity"
        ),
        CheckConstraint(
            "level BETWEEN 1 AND 5 AND due_after_s BETWEEN 1 AND 2592000", name="ck_step_bounds"
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128))
    version_id: Mapped[UUID] = mapped_column(Uuid)
    level: Mapped[int] = mapped_column(Integer)
    reviewer_id: Mapped[UUID] = mapped_column(Uuid)
    due_after_s: Mapped[int] = mapped_column(Integer)


class ReviewProgress(Base):
    __tablename__ = "review_progress"
    __table_args__ = (
        UniqueConstraint("tenant_id", "version_id", name="uq_progress_tenant"),
        ForeignKeyConstraint(
            ["tenant_id", "version_id"],
            ["item_versions.tenant_id", "item_versions.id"],
            name="fk_progress_version",
        ),
        CheckConstraint(
            "status IN ('PREPARING', 'NEEDS_INPUT', 'PLAN_FAILED', 'PENDING', 'CHANGES_REQUESTED', 'APPROVED', 'REJECTED', 'SUPERSEDED')",
            name="ck_progress_status",
        ),
        CheckConstraint(
            "revision >= 0 AND (current_level IS NULL OR current_level BETWEEN 1 AND 5)",
            name="ck_progress_revision",
        ),
        CheckConstraint(
            "(current_level IS NULL AND opened_at IS NULL AND deadline_at IS NULL) OR (status = 'PENDING' AND current_level IS NOT NULL AND opened_at IS NOT NULL AND deadline_at > opened_at)",
            name="ck_progress_deadline",
        ),
        Index("ix_progress_deadline", "tenant_id", "status", "deadline_at"),
    )
    version_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32))
    revision: Mapped[int] = mapped_column(BigInteger)
    current_level: Mapped[int | None] = mapped_column(Integer)
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ValidatedPlan(Base):
    __tablename__ = "validated_plans"
    __table_args__ = (
        UniqueConstraint("tenant_id", "version_id", name="uq_plan_version"),
        UniqueConstraint("tenant_id", "run_id", name="uq_plan_run"),
        UniqueConstraint("tenant_id", "version_id", "id", name="uq_plan_identity"),
        UniqueConstraint("tenant_id", "version_id", "plan_hash", name="uq_plan_hash"),
        ForeignKeyConstraint(
            ["tenant_id", "version_id", "run_id"],
            [
                "execution_runs.tenant_id",
                "execution_runs.item_version_id",
                "execution_runs.workflow_id",
            ],
            name="fk_plan_run",
            use_alter=True,
        ),
        CheckConstraint("plan_hash ~ '^[a-f0-9]{64}$' AND schema_version = 1", name="ck_plan_hash"),
        CheckConstraint(
            "octet_length(plan::text) + octet_length(actions::text) <= 262144",
            name="ck_plan_budget",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128))
    version_id: Mapped[UUID] = mapped_column(Uuid)
    run_id: Mapped[UUID] = mapped_column(Uuid)
    plan_hash: Mapped[str] = mapped_column(String(64))
    schema_version: Mapped[int] = mapped_column(Integer)
    catalog_version: Mapped[str] = mapped_column(String(128))
    plan: Mapped[dict] = mapped_column(JSONB)
    actions: Mapped[list] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Decision(Base):
    __tablename__ = "decisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "version_id", "level", "actor_id"],
            [
                "version_steps.tenant_id",
                "version_steps.version_id",
                "version_steps.level",
                "version_steps.reviewer_id",
            ],
            name="fk_decision_step",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "version_id", "plan_hash"],
            [
                "validated_plans.tenant_id",
                "validated_plans.version_id",
                "validated_plans.plan_hash",
            ],
            name="fk_decision_plan",
        ),
        UniqueConstraint("tenant_id", "version_id", "level", name="uq_decision_level"),
        UniqueConstraint("tenant_id", "actor_id", "command_id", name="uq_decision_command"),
        CheckConstraint(
            "kind IN ('APPROVE', 'REJECT', 'REQUEST_CHANGES') AND length(comment) <= 2000 AND (kind = 'APPROVE' OR comment ~ '[^[:space:]]')",
            name="ck_decision_comment",
        ),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128))
    version_id: Mapped[UUID] = mapped_column(Uuid)
    level: Mapped[int] = mapped_column(Integer)
    actor_id: Mapped[UUID] = mapped_column(Uuid)
    plan_hash: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(24))
    comment: Mapped[str] = mapped_column(String(2000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    command_id: Mapped[UUID] = mapped_column(Uuid)
    payload_hash: Mapped[str] = mapped_column(String(64))

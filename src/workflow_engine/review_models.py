"""Tenant-consistent review records. Execution state remains in execution_runs."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .db_models import Base


class ReviewCycle(Base):
    __tablename__ = "review_cycles"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_cycle_tenant"),
        ForeignKeyConstraint(
            ["tenant_id", "creator_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.id"],
            name="fk_cycle_creator",
        ),
        CheckConstraint("state IN ('DRAFT', 'ACTIVE', 'COMPLETED')", name="ck_cycle_state"),
        CheckConstraint(
            "revision >= 0 AND length(btrim(title)) BETWEEN 1 AND 160", name="ck_cycle_values"
        ),
        CheckConstraint(
            "(state = 'COMPLETED') = (completed_at IS NOT NULL)", name="ck_cycle_completion"
        ),
        Index("ix_cycle_tenant_state", "tenant_id", "state", "created_at", "id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128))
    creator_id: Mapped[UUID] = mapped_column(Uuid)
    title: Mapped[str] = mapped_column(String(160))
    state: Mapped[str] = mapped_column(String(16))
    revision: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CycleRequester(Base):
    __tablename__ = "cycle_requesters"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "cycle_id"],
            ["review_cycles.tenant_id", "review_cycles.id"],
            name="fk_requester_cycle",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "membership_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.id"],
            name="fk_cycle_requester",
        ),
    )
    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    cycle_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    membership_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)


class ReviewItem(Base):
    __tablename__ = "review_items"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "id", "current_version_id"],
            ["item_versions.tenant_id", "item_versions.item_id", "item_versions.id"],
            name="fk_item_current_version",
            use_alter=True,
            deferrable=True,
            initially="DEFERRED",
        ),
        UniqueConstraint("tenant_id", "id", name="uq_item_tenant"),
        ForeignKeyConstraint(
            ["tenant_id", "cycle_id"],
            ["review_cycles.tenant_id", "review_cycles.id"],
            name="fk_item_cycle",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "cycle_id", "requester_id"],
            [
                "cycle_requesters.tenant_id",
                "cycle_requesters.cycle_id",
                "cycle_requesters.membership_id",
            ],
            name="fk_item_requester",
        ),
        CheckConstraint("revision >= 0", name="ck_item_revision"),
        Index("ix_item_cycle", "tenant_id", "cycle_id", "id"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128))
    cycle_id: Mapped[UUID] = mapped_column(Uuid)
    requester_id: Mapped[UUID] = mapped_column(Uuid)
    current_version_id: Mapped[UUID | None] = mapped_column(Uuid)
    revision: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CommandReceipt(Base):
    __tablename__ = "command_receipts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "actor_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.id"],
            name="fk_receipt_actor",
        ),
        CheckConstraint("octet_length(result::text) <= 16384", name="ck_receipt_budget"),
    )
    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    actor_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    command_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    payload_hash: Mapped[str] = mapped_column(String(64))
    result: Mapped[dict] = mapped_column(JSONB)

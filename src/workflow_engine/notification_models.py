"""One durable overdue job per immutable version and assigned review step."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db_models import Base


class ReminderJob(Base):
    __tablename__ = "reminder_jobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "version_id", "level", "recipient_id"],
            [
                "version_steps.tenant_id",
                "version_steps.version_id",
                "version_steps.level",
                "version_steps.reviewer_id",
            ],
            name="fk_reminder_step",
        ),
        UniqueConstraint("tenant_id", "version_id", "level", name="uq_reminder_step"),
        CheckConstraint(
            "state IN ('PENDING', 'IN_FLIGHT', 'SENT', 'CANCELLED', 'FAILED') AND attempts BETWEEN 0 AND 5",
            name="ck_reminder_state",
        ),
        CheckConstraint(
            "(state='IN_FLIGHT' AND lease_token IS NOT NULL AND lease_until IS NOT NULL) OR (state<>'IN_FLIGHT' AND lease_token IS NULL AND lease_until IS NULL)",
            name="ck_reminder_lease",
        ),
        Index("ix_reminder_due", "state", "next_attempt_at", "lease_until"),
    )
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(128))
    version_id: Mapped[UUID] = mapped_column(Uuid)
    level: Mapped[int] = mapped_column(Integer)
    recipient_id: Mapped[UUID] = mapped_column(Uuid)
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    state: Mapped[str] = mapped_column(String(16))
    attempts: Mapped[int] = mapped_column(Integer)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(64))

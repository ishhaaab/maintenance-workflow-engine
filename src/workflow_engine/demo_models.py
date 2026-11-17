"""Operator-only demo provisioning journal, not review or execution authority."""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .db_models import Base


class DemoSeedRecord(Base):
    __tablename__ = "demo_seed_records"
    __table_args__ = (
        CheckConstraint("state IN ('STARTED', 'COMPLETE')", name="ck_demo_seed_state"),
        CheckConstraint("octet_length(manifest::text) <= 131072", name="ck_demo_seed_budget"),
    )
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    state: Mapped[str] = mapped_column(String(16))
    manifest: Mapped[dict] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

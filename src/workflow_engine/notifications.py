"""Leased SMTP delivery. A committed job is not exactly one delivered email."""

import asyncio
import smtplib
import ssl
from datetime import UTC, timedelta
from email.message import EmailMessage
from uuid import uuid4

from sqlalchemy import func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from .audit import append_event
from .domain import RETRY_DELAYS
from .identity_models import Membership, User
from .notification_models import ReminderJob
from .review_models import ReviewCycle, ReviewItem
from .version_models import ItemVersion, ReviewProgress, VersionStep


def aware(value):
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timezone-aware reminder timestamps required")
    return value.astimezone(UTC)


def eligible(
    *, cycle_state, status, current_version, version, current_level, level, deadline, enabled, at
):
    at, deadline = aware(at), aware(deadline)
    return (
        cycle_state == "ACTIVE"
        and status == "PENDING"
        and current_version == version
        and current_level == level
        and enabled
        and deadline <= at
    )


async def schedule_reminder(session, progress):
    if progress.status != "PENDING" or progress.current_level is None:
        return
    step = (
        await session.execute(
            select(VersionStep).where(
                VersionStep.tenant_id == progress.tenant_id,
                VersionStep.version_id == progress.version_id,
                VersionStep.level == progress.current_level,
            )
        )
    ).scalar_one()
    identity = (
        await session.execute(
            insert(ReminderJob)
            .values(
                id=uuid4(),
                tenant_id=progress.tenant_id,
                version_id=progress.version_id,
                level=progress.current_level,
                recipient_id=step.reviewer_id,
                deadline_at=progress.deadline_at,
                state="PENDING",
                attempts=0,
                next_attempt_at=progress.deadline_at,
            )
            .on_conflict_do_nothing(constraint="uq_reminder_step")
            .returning(ReminderJob.id)
        )
    ).scalar_one_or_none()
    if identity:
        await append_event(
            session,
            None,
            "reminder",
            identity,
            "reminder_scheduled",
            0,
            tenant_id=progress.tenant_id,
        )


async def cancel_reminders(session, version_id):
    await session.execute(
        update(ReminderJob)
        .where(
            ReminderJob.version_id == version_id, ReminderJob.state.in_(["PENDING", "IN_FLIGHT"])
        )
        .values(state="CANCELLED", lease_token=None, lease_until=None)
    )


class SmtpSender:
    def __init__(self, settings):
        settings.notification_security()
        self.settings = settings

    async def send(self, message):
        def deliver():
            settings = self.settings
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
                if settings.smtp_starttls:
                    smtp.starttls(context=ssl.create_default_context())
                if settings.smtp_username:
                    smtp.login(settings.smtp_username, settings.smtp_password.get_secret_value())
                smtp.send_message(message)

        await asyncio.to_thread(deliver)


class RecipientUnavailable(Exception):
    """Current review is live, but its disabled identity may be re-enabled."""


class NotificationWorker:
    def __init__(self, database, sender, origin, *, clock=None):
        self.database, self.sender, self.origin, self.clock = (
            database,
            sender,
            origin.rstrip("/"),
            clock,
        )

    async def _now(self, session):
        return (
            aware(self.clock())
            if self.clock
            else (await session.execute(select(func.clock_timestamp()))).scalar_one()
        )

    async def claim(self, worker_id):
        async with self.database.sessions.begin() as session:
            at = await self._now(session)
            job = (
                await session.execute(
                    select(ReminderJob)
                    .where(
                        ReminderJob.next_attempt_at <= at,
                        or_(
                            ReminderJob.state == "PENDING",
                            (ReminderJob.state == "IN_FLIGHT") & (ReminderJob.lease_until <= at),
                        ),
                    )
                    .order_by(ReminderJob.next_attempt_at, ReminderJob.id)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if job is None:
                return None
            if job.attempts >= 5:
                job.state, job.failed_at = "FAILED", at
                job.lease_token = job.lease_until = None
                job.last_error = "acknowledgement_budget_exhausted"
                await append_event(
                    session,
                    None,
                    "reminder",
                    job.id,
                    "reminder_delivery_failed",
                    job.attempts,
                    tenant_id=job.tenant_id,
                    after_state="FAILED",
                )
                return None
            job.attempts += 1
            job.state, job.lease_token, job.lease_until = (
                "IN_FLIGHT",
                uuid4().hex,
                at + timedelta(seconds=30),
            )
            await session.flush()
            return {column.name: getattr(job, column.name) for column in job.__table__.columns}

    async def message_for(self, claimed):
        async with self.database.sessions() as session:
            at = await self._now(session)
            job = await session.get(ReminderJob, claimed["id"])
            if (
                not job
                or job.state != "IN_FLIGHT"
                or job.lease_token != claimed["lease_token"]
                or job.lease_until <= at
            ):
                return None
            row = (
                await session.execute(
                    select(ReviewCycle, ReviewItem, ReviewProgress, Membership, User)
                    .select_from(ItemVersion)
                    .join(ReviewItem, ReviewItem.id == ItemVersion.item_id)
                    .join(ReviewCycle, ReviewCycle.id == ReviewItem.cycle_id)
                    .join(ReviewProgress, ReviewProgress.version_id == ItemVersion.id)
                    .join(
                        VersionStep,
                        (VersionStep.version_id == ItemVersion.id)
                        & (VersionStep.level == job.level),
                    )
                    .join(Membership, Membership.id == VersionStep.reviewer_id)
                    .join(User)
                    .where(ItemVersion.id == job.version_id, ItemVersion.tenant_id == job.tenant_id)
                )
            ).one_or_none()
            if not row:
                return None
            cycle, item, progress, membership, user = row
            if membership.id != job.recipient_id or not eligible(
                cycle_state=cycle.state,
                status=progress.status,
                current_version=item.current_version_id,
                version=job.version_id,
                current_level=progress.current_level,
                level=job.level,
                deadline=job.deadline_at,
                enabled=True,
                at=at,
            ):
                return None
            if not membership.enabled or not user.enabled:
                raise RecipientUnavailable
            message = EmailMessage()
            message["From"] = "maintenance@workflow.invalid"
            message["To"] = user.login if "@" in user.login else user.login + "@workflow.invalid"
            message["Subject"] = "Maintenance review is overdue"
            message["Message-ID"] = f"<{job.id}@workflow.invalid>"
            message.set_content(
                "A maintenance review is waiting for your decision.\n"
                "Sign in to see the current version and permissions:\n"
                + self.origin
                + "/items/"
                + str(item.id)
                + "\n"
                "This message may arrive after the review changes. It does not authorize an action.\n"
            )
            return message

    async def acknowledge(self, claimed, *, sent=False, cancelled=False, deferred=False):
        async with self.database.sessions.begin() as session:
            at = await self._now(session)
            state = (
                "PENDING"
                if deferred
                else "CANCELLED"
                if cancelled
                else "SENT"
                if sent
                else "FAILED"
                if claimed["attempts"] >= 5
                else "PENDING"
            )
            values = dict(
                state=state,
                lease_token=None,
                lease_until=None,
                delivered_at=at if sent else None,
                failed_at=at if state == "FAILED" else None,
                last_error="recipient_disabled"
                if deferred
                else None
                if sent or cancelled
                else "smtp_unavailable",
            )
            if deferred:
                # Claims count possible SMTP submissions so crashes are bounded.
                # Here no send was attempted; keep the same unique job and budget.
                values["attempts"] = claimed["attempts"] - 1
            if state == "PENDING":
                values["next_attempt_at"] = at + timedelta(
                    seconds=60 if deferred else RETRY_DELAYS[claimed["attempts"] - 1]
                )
            result = (
                await session.execute(
                    update(ReminderJob)
                    .where(
                        ReminderJob.id == claimed["id"],
                        ReminderJob.tenant_id == claimed["tenant_id"],
                        ReminderJob.state == "IN_FLIGHT",
                        ReminderJob.lease_token == claimed["lease_token"],
                        ReminderJob.lease_until > at,
                    )
                    .values(**values)
                    .returning(ReminderJob.id)
                )
            ).scalar_one_or_none()
            if not result:
                return "lease_lost"
            await append_event(
                session,
                None,
                "reminder",
                result,
                "reminder_deferred"
                if deferred
                else "reminder_sent"
                if sent
                else "reminder_cancelled"
                if cancelled
                else "reminder_delivery_failed",
                claimed["attempts"] - 1 if deferred else claimed["attempts"],
                tenant_id=claimed["tenant_id"],
                after_state=state,
            )
            return "deferred" if deferred else "retry" if state == "PENDING" else state.lower()

    async def dispatch_once(self, worker_id):
        claimed = await self.claim(worker_id)
        if claimed is None:
            return "idle"
        try:
            message = await self.message_for(claimed)
        except RecipientUnavailable:
            return await self.acknowledge(claimed, deferred=True)
        if message is None:
            return await self.acknowledge(claimed, cancelled=True)
        try:
            await self.sender.send(message)
        except (OSError, smtplib.SMTPException):
            return await self.acknowledge(claimed)
        return await self.acknowledge(claimed, sent=True)

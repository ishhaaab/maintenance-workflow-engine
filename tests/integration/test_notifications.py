import asyncio
import os
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select, text

from tests.integration.test_approval_execution import approval as approval
from tests.integration.test_approval_execution import decision
from tests.postgres_support import owner_session

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


class Sender:
    def __init__(self, fail=False):
        self.messages, self.fail = [], fail

    async def send(self, message):
        if self.fail:
            raise OSError("synthetic SMTP failure")
        self.messages.append(message)


async def test_unique_jobs_competing_workers_obsolete_step_and_bounded_retries(approval):
    from workflow_engine.notification_models import ReminderJob
    from workflow_engine.notifications import NotificationWorker, schedule_reminder
    from workflow_engine.version_models import ReviewProgress

    commands, _, _, reviewers, item_id, _ = approval
    item = await commands.get_item(reviewers[0], item_id)
    deadline = item["progress"]["deadline_at"]
    sender = Sender()
    workers = [
        NotificationWorker(commands.database, sender, "http://localhost", clock=lambda: deadline)
        for _ in range(2)
    ]
    async with commands.database.sessions.begin() as session:
        progress = await session.get(
            ReviewProgress, __import__("uuid").UUID(item["current_version_id"])
        )
        await schedule_reminder(session, progress)
        await schedule_reminder(session, progress)
    results = await asyncio.gather(
        *(w.dispatch_once("notify-" + str(i)) for i, w in enumerate(workers))
    )
    assert sorted(results) == ["idle", "sent"]
    assert len(sender.messages) == 1
    assert "http://localhost/items/" in sender.messages[0].get_content()
    assert "pump P-17" not in sender.messages[0].get_content()
    await decision(commands, reviewers[0], item_id)
    current = await commands.get_item(reviewers[1], item_id)
    clock = [current["progress"]["deadline_at"]]
    failing = NotificationWorker(
        commands.database, Sender(fail=True), "http://localhost", clock=lambda: clock[0]
    )
    for delay in (1, 2, 4, 8):
        assert await failing.dispatch_once("retry") == "retry"
        assert await failing.dispatch_once("too-early") == "idle"
        clock[0] += timedelta(seconds=delay)
    assert await failing.dispatch_once("retry") == "failed"
    assert await failing.dispatch_once("exhausted") == "idle"
    async with commands.database.sessions() as session:
        jobs = (
            (await session.execute(select(ReminderJob).order_by(ReminderJob.level))).scalars().all()
        )
        assert len(jobs) == 2
        assert [job.attempts for job in jobs] == [1, 5]


async def test_decision_before_send_cancels_job_and_accepted_unacked_mail_can_repeat(approval):
    from workflow_engine.notifications import NotificationWorker

    commands, _, _, reviewers, item_id, _ = approval
    item = await commands.get_item(reviewers[0], item_id)
    sender = Sender()
    worker = NotificationWorker(
        commands.database, sender, "http://localhost", clock=lambda: item["progress"]["deadline_at"]
    )
    claimed = await worker.claim("crash-before-ack")
    assert claimed is not None
    # The SMTP server accepted, but the process died before its database acknowledgement.
    message = await worker.message_for(claimed)
    await sender.send(message)
    async with commands.database.sessions.begin() as session:
        await session.execute(
            text("UPDATE reminder_jobs SET lease_until=:past WHERE id=:id"),
            {"past": item["progress"]["deadline_at"] - timedelta(seconds=1), "id": claimed["id"]},
        )
    assert await worker.dispatch_once("recovered") == "sent"
    assert len(sender.messages) == 2
    assert sender.messages[0]["Message-ID"] == sender.messages[1]["Message-ID"]
    await decision(commands, reviewers[0], item_id, "REQUEST_CHANGES", "Please revise.")
    assert await worker.dispatch_once("obsolete") == "idle"


async def test_real_smtp_message_appears_in_local_mailpit(approval):
    from workflow_engine.notifications import NotificationWorker, SmtpSender
    from workflow_engine.settings import Settings

    commands, _, _, reviewers, item_id, h = approval
    inbox = os.environ.get("TEST_MAILPIT_URL")
    if not inbox:
        pytest.skip("Set TEST_MAILPIT_URL and TEST_SMTP_PORT for real SMTP capture acceptance")
    item = await commands.get_item(reviewers[0], item_id)
    async with owner_session(h.store) as session:
        await session.execute(
            text(
                "UPDATE review_progress SET opened_at=clock_timestamp()-interval '2 seconds', "
                "deadline_at=clock_timestamp()-interval '1 second' WHERE version_id=:id"
            ),
            {"id": item["current_version_id"]},
        )
        await session.execute(
            text(
                "UPDATE reminder_jobs SET deadline_at=clock_timestamp()-interval '1 second', "
                "next_attempt_at=clock_timestamp()-interval '1 second' WHERE version_id=:id"
            ),
            {"id": item["current_version_id"]},
        )
    settings = Settings(
        env="local",
        origin="http://localhost",
        smtp_host="127.0.0.1",
        smtp_port=int(os.environ["TEST_SMTP_PORT"]),
    )
    worker = NotificationWorker(commands.database, SmtpSender(settings), settings.origin)
    assert await worker.dispatch_once("real-smtp") == "sent"
    async with httpx.AsyncClient(base_url=inbox, trust_env=False) as client:
        messages = (await client.get("/api/v1/messages")).json()["messages"]
        recipient = "approval-reviewer-0@workflow.invalid"
        assert any(
            any(address["Address"] == recipient for address in message["To"])
            and message["Subject"] == "Maintenance review is overdue"
            for message in messages
        )


async def test_last_attempt_acknowledgement_loss_is_failed_and_audited(approval):
    from workflow_engine.db_models import AuditRecord
    from workflow_engine.notification_models import ReminderJob
    from workflow_engine.notifications import NotificationWorker

    commands, admin, _, reviewers, item_id, h = approval
    item = await commands.get_item(reviewers[0], item_id)
    deadline = item["progress"]["deadline_at"]
    async with owner_session(h.store) as session:
        await session.execute(
            text("UPDATE reminder_jobs SET attempts=4 WHERE version_id=:id"),
            {"id": item["current_version_id"]},
        )
    worker = NotificationWorker(
        commands.database, Sender(), "http://localhost", clock=lambda: deadline
    )
    claimed = await worker.claim("last-attempt")
    async with owner_session(h.store) as session:
        await session.execute(
            text("UPDATE reminder_jobs SET lease_until=:past WHERE id=:id"),
            {"past": deadline - timedelta(seconds=1), "id": claimed["id"]},
        )
    assert await worker.dispatch_once("recover-last-attempt") == "idle"
    async with commands.database.sessions() as session:
        row = await session.get(ReminderJob, claimed["id"])
        assert row.state == "FAILED" and row.attempts == 5
        events = (
            (
                await session.execute(
                    select(AuditRecord).where(
                        AuditRecord.entity_id == row.id,
                        AuditRecord.event == "reminder_delivery_failed",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
    visible = await commands.get_item(admin, item_id)
    assert visible["reminders"][0]["state"] == "FAILED"
    assert "lease_token" not in visible["reminders"][0]


async def test_decision_after_claim_but_before_send_suppresses_unsent_message(approval):
    from workflow_engine.notifications import NotificationWorker

    commands, _, _, reviewers, item_id, _ = approval
    item = await commands.get_item(reviewers[0], item_id)
    sender = Sender()
    worker = NotificationWorker(
        commands.database, sender, "http://localhost", clock=lambda: item["progress"]["deadline_at"]
    )
    claimed = await worker.claim("claimed-before-decision")
    assert claimed is not None
    await decision(commands, reviewers[0], item_id, "REQUEST_CHANGES", "Please clarify.")
    assert await worker.message_for(claimed) is None
    assert await worker.dispatch_once("after-decision") == "idle"
    assert sender.messages == []


async def test_disabled_current_reviewer_defers_without_spending_smtp_budget_and_resumes(approval):
    from workflow_engine.auth import AuthService
    from workflow_engine.notification_models import ReminderJob
    from workflow_engine.notifications import NotificationWorker
    from workflow_engine.settings import Settings

    commands, admin, _, reviewers, item_id, _ = approval
    auth = AuthService(commands.database, Settings(env="local", origin="http://localhost"))
    item = await commands.get_item(admin, item_id)
    at = item["progress"]["deadline_at"]
    sender = Sender()
    worker = NotificationWorker(commands.database, sender, "http://localhost", clock=lambda: at)
    await auth.set_account_enabled(admin, reviewers[0].membership_id, False)
    for _ in range(6):
        assert await worker.dispatch_once("temporarily-disabled") == "deferred"
        at += timedelta(seconds=61)
    assert sender.messages == []
    async with commands.database.sessions() as session:
        jobs = (
            (
                await session.execute(
                    select(ReminderJob).where(ReminderJob.version_id == item["current_version_id"])
                )
            )
            .scalars()
            .all()
        )
        assert len(jobs) == 1 and jobs[0].attempts == 0 and jobs[0].state == "PENDING"
    await auth.set_account_enabled(admin, reviewers[0].membership_id, True)
    assert await worker.dispatch_once("reenabled") == "sent"
    assert len(sender.messages) == 1
    async with commands.database.sessions() as session:
        job = (
            await session.execute(
                select(ReminderJob).where(ReminderJob.version_id == item["current_version_id"])
            )
        ).scalar_one()
        assert job.attempts == 1 and job.state == "SENT"

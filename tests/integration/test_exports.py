import asyncio
import csv
import io

import pytest

from tests.integration.test_approval_execution import approval as approval
from tests.integration.test_approval_execution import decision

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


async def test_admin_only_tenant_scoped_complete_report_from_one_model(approval):
    from workflow_engine.authorization import Denied
    from workflow_engine.exports import csv_report, pdf_report, query_report

    commands, admin, requester, reviewers, item_id, _ = approval
    await decision(commands, reviewers[0], item_id, comment='Café, "quoted"\nΔοκιμή')
    for actor in (requester, *reviewers):
        with pytest.raises(Denied):
            await query_report(
                commands.database, actor, (await commands.get_item(actor, item_id))["cycle_id"]
            )
    item = await commands.get_item(admin, item_id)
    report = await query_report(commands.database, admin, item["cycle_id"])
    assert len(report.rows) == 2
    assert report.rows[0]["comment"] == 'Café, "quoted"\nΔοκιμή'
    assert all(row["execution_outcome"] == "AWAITING_APPROVAL" for row in report.rows)
    parsed = list(csv.DictReader(io.StringIO(csv_report(report).decode("utf-8-sig"))))
    assert len(parsed) == 2
    import json

    audit_events = json.loads(parsed[0]["audit_events"])
    assert audit_events and all(
        event["at"] and event["id"] in parsed[0]["audit_ids"] for event in audit_events
    )
    assert pdf_report(report).startswith(b"%PDF-")
    assert report.audit


async def test_report_during_decision_is_internally_consistent_and_empty_cycle_exports(approval):
    from workflow_engine.exports import csv_report, pdf_report, query_report

    commands, admin, _, reviewers, item_id, _ = approval
    await decision(commands, reviewers[0], item_id)
    item = await commands.get_item(admin, item_id)
    report, _ = await asyncio.gather(
        query_report(commands.database, admin, item["cycle_id"]),
        decision(commands, reviewers[1], item_id),
    )
    outcomes = {row["review_outcome"] for row in report.rows}
    assert outcomes in ({"PENDING"}, {"APPROVED"})
    assert all(
        (row["review_outcome"] == "APPROVED") == (row["execution_outcome"] == "READY")
        for row in report.rows
    )
    empty = await commands.create_cycle(admin, "Empty cycle")
    snapshot = await query_report(commands.database, admin, empty["id"])
    assert snapshot.rows == ()
    empty_rows = list(csv.DictReader(io.StringIO(csv_report(snapshot).decode("utf-8-sig"))))
    assert len(empty_rows) == 1 and empty_rows[0]["item_id"] == ""
    assert empty_rows[0]["cycle_title"] == "Empty cycle" and empty_rows[0]["audit_events"]
    assert pdf_report(snapshot).startswith(b"%PDF-")


async def test_export_limit_counts_final_rows_including_repeated_audit_ids(approval, monkeypatch):
    import json
    from uuid import UUID

    from workflow_engine import exports
    from workflow_engine.audit import append_event
    from workflow_engine.authorization import Denied

    commands, admin, _, _, item_id, _ = approval
    item = await commands.get_item(admin, item_id)
    async with commands.database.sessions.begin() as session:
        for number in range(80):
            await append_event(
                session, admin, "cycle", UUID(item["cycle_id"]), "cycle_inspected", number
            )
    report = await exports.query_report(commands.database, admin, item["cycle_id"])
    size = len(
        json.dumps(
            {
                "cycle": report.cycle,
                "generated_at": report.generated_at.isoformat(),
                "rows": report.rows,
                "audit": report.audit,
            },
            ensure_ascii=False,
        ).encode()
    )
    monkeypatch.setattr(exports, "MAX_REPORT_BYTES", size - 1)
    with pytest.raises(Denied) as error:
        await exports.query_report(commands.database, admin, item["cycle_id"])
    assert error.value.status == 413


async def test_report_includes_more_than_a_ui_page_and_refuses_over_limit(approval, monkeypatch):
    from uuid import uuid4

    from workflow_engine import exports
    from workflow_engine.authorization import Denied

    commands, admin, requester, _, _, _ = approval
    cycle = await commands.create_cycle(admin, "Complete row coverage")
    cycle = await commands.admit_requester(
        admin, cycle["id"], requester.membership_id, cycle["revision"]
    )
    for index in range(51):
        await commands.create_item(requester, cycle["id"], cycle["revision"] + index, uuid4())
    page = await commands.get_cycle(admin, cycle["id"])
    assert len(page["items"]) == 50 and page["next_offset"] == 50
    report = await exports.query_report(commands.database, admin, cycle["id"])
    assert len(report.rows) == 51
    monkeypatch.setattr(exports, "MAX_VERSION_ROWS", 50)
    with pytest.raises(Denied) as error:
        await exports.query_report(commands.database, admin, cycle["id"])
    assert error.value.status == 413


async def test_csv_includes_decision_execution_and_reminder_audit_and_empty_cycle_facts(approval):
    import json

    from tests.integration.test_notifications import Sender
    from workflow_engine.exports import csv_report, query_report
    from workflow_engine.notifications import NotificationWorker

    commands, admin, _, reviewers, item_id, h = approval
    item = await commands.get_item(admin, item_id)
    worker = NotificationWorker(
        commands.database,
        Sender(),
        "http://localhost",
        clock=lambda: item["progress"]["deadline_at"],
    )
    assert await worker.dispatch_once("report-reminder") == "sent"
    for reviewer in reviewers:
        await decision(commands, reviewer, item_id)
    for _ in range(4):
        await h.runner.tick(item["execution"]["workflow_id"], "report-executor")
    report = await query_report(commands.database, admin, item["cycle_id"])
    records = list(csv.DictReader(io.StringIO(csv_report(report).decode("utf-8-sig"))))
    events = {event["event"] for record in records for event in json.loads(record["audit_events"])}
    assert {
        "review_decided",
        "execution_authorized",
        "reminder_scheduled",
        "reminder_sent",
        "transitioned",
    } <= events
    assert "reminder_sent" in {event["event"] for event in report.audit}
    empty = await commands.create_cycle(admin, "Empty audit coverage")
    empty_report = await query_report(commands.database, admin, empty["id"])
    empty_records = list(csv.DictReader(io.StringIO(csv_report(empty_report).decode("utf-8-sig"))))
    assert len(empty_records) == 1
    assert "cycle_created" in {
        event["event"] for event in json.loads(empty_records[0]["audit_events"])
    }


async def test_draft_report_retains_configured_assignments_before_first_submission(approval):
    from uuid import uuid4

    from workflow_engine.exports import query_report

    commands, admin, requester, reviewers, _, _ = approval
    cycle = await commands.create_cycle(admin, "Draft assignments")
    cycle = await commands.admit_requester(
        admin, cycle["id"], requester.membership_id, cycle["revision"]
    )
    item = await commands.create_item(requester, cycle["id"], cycle["revision"], uuid4())
    await commands.configure_chain(
        admin,
        item["id"],
        [
            {"reviewer_id": str(reviewer.membership_id), "due_after_s": 3600}
            for reviewer in reviewers
        ],
        item["revision"],
    )
    report = await query_report(commands.database, admin, cycle["id"])
    assert [row["level"] for row in report.rows] == [1, 2]
    assert all(
        row["reviewer"] and row["review_outcome"] == "UNSUBMITTED" and not row["version_id"]
        for row in report.rows
    )

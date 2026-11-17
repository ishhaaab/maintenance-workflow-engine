"""Fresh production-image Compose demo, real browser, SMTP capture and file downloads."""

import csv
import io
import json
import os
import time
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from tests.compose_support import ComposeProject
from tests.e2e.test_review_browser import login, wait_for
from tests.processes import free_port
from tests.support import REQUEST
from workflow_engine.seed_demo import DemoCredential

pytestmark = [pytest.mark.compose, pytest.mark.browser]


def seed(project):
    # One-time passwords are operator output, not application logs or test artifacts.
    result = project.run(
        "exec",
        "-T",
        "-e",
        "WORKFLOW_ALLOW_DEMO_SEED=true",
        "api",
        "workflow-engine",
        "seed-demo",
        timeout=180,
        check=False,
    )
    if result.returncode:
        raise AssertionError("Local seed command failed; no private output was copied to artifacts")
    value = json.loads(result.stdout.strip().splitlines()[-1])
    credentials = [
        DemoCredential(row["tenant"], row["login"], row["role"], SecretStr(row["password"]))
        for row in value.pop("credentials")
    ]
    return value, credentials


def captured_message(inbox, item_id):
    deadline = time.monotonic() + 30
    with httpx.Client(base_url=inbox, timeout=5, trust_env=False) as client:
        while time.monotonic() < deadline:
            messages = client.get("/api/v1/messages").json()["messages"]
            for message in messages:
                detail = client.get("/api/v1/message/" + message["ID"]).json()
                if "/items/" + item_id in detail.get("Text", ""):
                    assert REQUEST not in detail["Text"]
                    assert message["Subject"] == "Maintenance review is overdue"
                    return message
            time.sleep(0.1)
    raise AssertionError("Current item reminder did not appear in the owned capture inbox")


def browse_walkthrough(project, origin, inbox, credentials, directory):
    from playwright.sync_api import Error, expect, sync_playwright
    from pypdf import PdfReader

    by_login = {row.login: row for row in credentials}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        try:

            def signed_in(name):
                credential = by_login[name]
                try:
                    return login(
                        browser,
                        origin,
                        name,
                        tenant=credential.tenant,
                        password=credential.password.get_secret_value(),
                    )
                except Error:
                    # Locator logs can repeat fill arguments. Never print a password.
                    raise AssertionError("Named demo browser sign-in failed") from None

            admin_context, admin = signed_in("tenant-a.admin")
            requester_context, requester = signed_in("tenant-a.requester")
            first_context, first = signed_in("tenant-a.reviewer-1")
            second_context, second = signed_in("tenant-a.reviewer-2")
            foreign_context, _ = signed_in("tenant-b.admin")
            admin.get_by_label("Cycle title", exact=True).fill("Walkthrough maintenance – café")
            admin.get_by_role("button", name="Create cycle", exact=True).click()
            cycle_url, cycle_id = admin.url, admin.url.rsplit("/", 1)[1]
            admin.get_by_label("Requester account").select_option(label="tenant-a.requester")
            admin.get_by_role("button", name="Admit requester", exact=True).click()
            requester.goto(cycle_url)
            requester.get_by_role("button", name="Create item", exact=True).click()
            item_url, item_id = requester.url, requester.url.rsplit("/", 1)[1]
            admin.goto(item_url)
            for level in (1, 2):
                admin.locator(f"#reviewer_{level}").select_option(
                    label=f"tenant-a.reviewer-{level}"
                )
                admin.locator(f"#duration_{level}").fill("1" if level == 1 else "3600")
            admin.get_by_role("button", name="Save approval chain", exact=True).click()
            requester.reload()
            requester.get_by_label("Maintenance request", exact=True).fill(REQUEST)
            requester.get_by_role("button", name="Submit version", exact=True).click()
            wait_for(
                requester,
                lambda: requester.get_by_role(
                    "heading", name="Validated maintenance plan"
                ).is_visible(),
            )
            admin.goto(cycle_url)
            admin.get_by_role("button", name="Activate cycle", exact=True).click()
            message = captured_message(inbox, item_id)
            assert [address["Address"] for address in message["To"]] == [
                "tenant-a.reviewer-1@workflow.invalid"
            ]
            assert foreign_context.request.get(origin + "/api/cycles/" + cycle_id).status == 404
            assert (
                foreign_context.request.get(origin + f"/api/cycles/{cycle_id}/export.pdf").status
                == 404
            )
            for context in (requester_context, first_context, second_context):
                assert (
                    context.request.get(origin + f"/api/cycles/{cycle_id}/export.csv").status == 403
                )
            second.goto(item_url)
            expect(second.get_by_role("button", name="Approve", exact=True)).to_have_count(0)
            first.goto(item_url)
            first.get_by_role("button", name="Approve", exact=True).click()
            second.reload()
            second.get_by_label("Decision comment").fill("Confirm the maintenance window.")
            second.get_by_role("button", name="Request changes", exact=True).click()
            requester.reload()
            expect(requester.get_by_role("heading", name="Submit a new version")).to_be_visible()
            requester.get_by_role("button", name="Submit version", exact=True).click()
            wait_for(
                requester,
                lambda: requester.get_by_role(
                    "heading", name="Validated maintenance plan"
                ).is_visible(),
            )
            first.goto(item_url)
            first.get_by_role("button", name="Approve", exact=True).click()
            second.goto(item_url)
            second.get_by_role("button", name="Approve", exact=True).click()
            requester.reload()
            wait_for(requester, lambda: requester.get_by_text("Succeeded", exact=True).count() > 0)
            admin.goto(cycle_url)
            expect(admin.locator("main").get_by_text("Completed", exact=True)).to_be_visible()
            files = {}
            for kind, label in (("csv", "Download CSV"), ("pdf", "Download PDF")):
                with admin.expect_download() as pending:
                    admin.get_by_role("link", name=label, exact=True).click()
                download = pending.value
                assert download.suggested_filename.endswith("." + kind)
                filename = directory / ("verified-cycle." + kind)
                download.save_as(filename)
                files[kind] = filename
            csv_text = files["csv"].read_text(encoding="utf-8-sig")
            rows = list(csv.DictReader(io.StringIO(csv_text)))
            assert len(rows) == 4 and len({row["version_id"] for row in rows}) == 2
            assert {row["review_outcome"] for row in rows} == {"SUPERSEDED", "APPROVED"}
            assert {row["execution_outcome"] for row in rows if row["current"] == "yes"} == {
                "SUCCEEDED"
            }
            assert "lease_token" not in csv_text and "password" not in csv_text
            pdf = PdfReader(files["pdf"])
            text = "".join(page.extract_text() for page in pdf.pages)
            assert "café" in text and "SUCCEEDED" in text and "SUPERSEDED" in text
            admin.set_viewport_size({"width": 360, "height": 800})
            admin.goto(item_url)
            assert admin.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            # Rerunning the seed cannot rewrite this new user-created cycle or sessions.
            rerun, repeated_credentials = seed(project)
            assert not rerun["created"] and repeated_credentials == []
            assert admin_context.request.get(origin + "/api/me").status == 200
            project.run("restart", "api", "worker-a", "worker-b", "notifications", timeout=120)
            deadline = time.monotonic() + 30
            while True:
                try:
                    if admin_context.request.get(origin + "/health/ready").status == 200:
                        break
                except Error:
                    pass
                if time.monotonic() >= deadline:
                    raise AssertionError("Owned demo did not recover after process restart")
                time.sleep(0.1)
            assert admin_context.request.get(origin + "/api/me").status == 200
            assert (
                admin_context.request.get(origin + "/api/cycles/" + cycle_id).json()["state"]
                == "COMPLETED"
            )
            return {
                "rows": len(rows),
                "pdf_pages": len(pdf.pages),
                "smtp_capture": True,
                "restart": True,
            }
        finally:
            browser.close()


def test_fresh_seeded_compose_walkthrough_browser_smtp_exports_restart(tmp_path):
    project = ComposeProject(tmp_path)
    if os.environ.get("RUN_BROWSER") != "1":
        pytest.skip("Seeded Compose walkthrough requires RUN_BROWSER=1 and installed Chromium")
    port = free_port()
    origin = f"http://127.0.0.1:{port}"
    # This test owns its explicit loopback port and origin; no wildcard Origin exception.
    configuration = project.envfile.read_text(encoding="utf-8").replace(
        "WORKFLOW_PG_API_PORT=0", f"WORKFLOW_PG_API_PORT={port}"
    )
    project.envfile.write_text(configuration + f"WORKFLOW_PG_ORIGIN={origin}\n", encoding="utf-8")
    try:
        project.run("build", "api", timeout=900)
        project.created = True
        project.run("run", "--rm", "--no-deps", "prepare", "admin")
        project.run("up", "-d", "--wait", "postgres", "redis", timeout=180)
        project.run("run", "--rm", "--no-deps", "prepare", "roles")
        project.run("run", "--rm", "--no-deps", "migrate")
        project.run("up", "-d", "--wait", "--wait-timeout", "120", timeout=240)
        seeded, credentials = seed(project)
        assert seeded["created"] and len(credentials) == 8
        inbox = project.url("mailpit", 8025)
        result = browse_walkthrough(project, origin, inbox, credentials, tmp_path)
        result.update(project=project.project, seeded_tenants=2, passed=True)
        (tmp_path / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        if output := os.environ.get("REVIEW_REPORT_DIR"):
            destination = Path(output)
            destination.mkdir(parents=True, exist_ok=False)
            for kind in ("csv", "pdf"):
                (destination / ("verified-cycle." + kind)).write_bytes(
                    (tmp_path / ("verified-cycle." + kind)).read_bytes()
                )
    finally:
        try:
            project.cleanup()
        finally:
            project.save_artifacts()

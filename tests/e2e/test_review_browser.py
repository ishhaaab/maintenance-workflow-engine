"""Real Chromium forms, real PostgreSQL/Redis and two runtime-only workers."""

import asyncio
import os
import time

import pytest

from tests.review_server import review_server
from tests.support import REQUEST

pytestmark = [pytest.mark.integration, pytest.mark.postgres, pytest.mark.browser]
PASSWORD = "browser-fixture-only-123"


def login(browser, origin, name, *, tenant="tenant-a", password=PASSWORD):
    context = browser.new_context(viewport={"width": 1366, "height": 900})
    page = context.new_page()
    page.goto(origin + "/login")
    page.get_by_label("Tenant", exact=True).fill(tenant)
    page.get_by_label("Login", exact=True).fill(name)
    page.get_by_label("Password", exact=True).fill(password)
    page.get_by_role("button", name="Sign in", exact=True).click()
    page.wait_for_url("**/cycles")
    return context, page


def wait_for(page, predicate, timeout=25):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        page.reload()
        if predicate():
            return
        page.wait_for_timeout(150)
    raise AssertionError("Expected current workflow projection did not appear")


def browser_journey(origin):
    from playwright.sync_api import expect, sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            admin_context, admin = login(browser, origin, "browser-admin")
            for name, role in (
                ("browser-requester", "REQUESTER"),
                ("browser-reviewer-1", "REVIEWER"),
                ("browser-reviewer-2", "REVIEWER"),
            ):
                admin.goto(origin + "/admin")
                admin.get_by_label("Login", exact=True).fill(name)
                admin.get_by_label("Role", exact=True).select_option(role)
                admin.get_by_label("Initial password").fill(PASSWORD)
                admin.get_by_role("button", name="Create account", exact=True).click()
                expect(admin.get_by_role("cell", name=name, exact=True)).to_be_visible()
            admin.goto(origin + "/cycles")
            admin.get_by_label("Cycle title").fill("Browser maintenance acceptance")
            admin.get_by_role("button", name="Create cycle", exact=True).click()
            cycle_url = admin.url
            admin.get_by_label("Requester account").select_option(label="browser-requester")
            admin.get_by_role("button", name="Admit requester", exact=True).click()
            requester_context, requester = login(browser, origin, "browser-requester")
            requester.goto(cycle_url)
            requester.get_by_role("button", name="Create item", exact=True).click()
            item_url = requester.url
            admin.goto(item_url)
            for level in (1, 2):
                admin.locator(f"#reviewer_{level}").select_option(label=f"browser-reviewer-{level}")
                admin.locator(f"#duration_{level}").fill("3600")
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
            expect(admin.locator("main").get_by_text("Active", exact=True)).to_be_visible()
            reviewer1_context, reviewer1 = login(browser, origin, "browser-reviewer-1")
            reviewer2_context, reviewer2 = login(browser, origin, "browser-reviewer-2")
            reviewer2.goto(origin + "/pending")
            expect(reviewer2.get_by_role("heading", name="No pending reviews")).to_be_visible()
            reviewer1.goto(item_url)
            stale = reviewer1_context.new_page()
            stale.goto(item_url)
            reviewer1.get_by_role("button", name="Approve", exact=True).click()
            stale.get_by_role("button", name="Approve", exact=True).click()
            expect(stale.get_by_role("alert")).to_contain_text("not applied")
            expect(stale.get_by_role("button", name="Approve", exact=True)).to_have_count(0)
            reviewer2.goto(item_url)
            reviewer2.get_by_label("Decision comment").fill("Confirm the maintenance window.")
            reviewer2.get_by_role("button", name="Request changes", exact=True).click()
            requester.reload()
            expect(requester.get_by_role("heading", name="Submit a new version")).to_be_visible()
            requester.get_by_role("button", name="Submit version", exact=True).click()
            wait_for(
                requester,
                lambda: (
                    requester.get_by_text("Version 2", exact=False).count() > 0
                    and requester.get_by_role(
                        "heading", name="Validated maintenance plan"
                    ).is_visible()
                ),
            )
            reviewer2.goto(item_url)
            expect(reviewer2.get_by_role("button", name="Approve", exact=True)).to_have_count(0)
            reviewer1.goto(item_url)
            reviewer1.get_by_role("button", name="Approve", exact=True).click()
            reviewer2.reload()
            reviewer2.get_by_role("button", name="Approve", exact=True).click()
            requester.goto(item_url)
            wait_for(requester, lambda: requester.get_by_text("Succeeded", exact=True).count() > 0)
            expect(requester.locator("main")).to_contain_text("Superseded")
            admin.goto(cycle_url)
            expect(admin.locator("main").get_by_text("Completed", exact=True)).to_be_visible()
            admin.set_viewport_size({"width": 360, "height": 800})
            admin.goto(item_url)
            assert admin.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
            admin.emulate_media(color_scheme="dark")
            assert admin.locator("h1").is_visible()
            before_logout = next(
                c["value"] for c in admin_context.cookies() if c["name"] == "workflow_local_session"
            )
            admin.get_by_role("button", name="Sign out", exact=True).click()
            admin.wait_for_url("**/login")
            assert all(c["value"] != before_logout for c in admin_context.cookies())
            assert admin_context.request.get(origin + "/api/me").status == 401
            requester_context.close()
            reviewer1_context.close()
            reviewer2_context.close()
        finally:
            browser.close()


async def test_authenticated_two_level_changes_journey_desktop_mobile(postgres_store, tmp_path):
    if os.environ.get("RUN_BROWSER") != "1":
        pytest.skip("Set RUN_BROWSER=1 and install Playwright Chromium for real browser acceptance")
    from workflow_engine.auth import AuthService
    from workflow_engine.settings import Settings

    auth = AuthService(postgres_store.database, Settings(env="local", origin="http://localhost"))
    # Use the real catalog namespace with an opaque tenant ID, not a test approval switch.
    async with postgres_store.database.sessions.begin() as session:
        from sqlalchemy import text

        await session.execute(
            text("UPDATE tenants SET slug='executor-fixture-a' WHERE id='tenant-a'")
        )
    await auth.bootstrap("tenant-a", "browser-admin", PASSWORD)
    with review_server(postgres_store, tmp_path / "browser-runtime") as (origin, _):
        await asyncio.to_thread(browser_journey, origin)

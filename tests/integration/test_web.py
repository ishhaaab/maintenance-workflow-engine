import re

import httpx
import pytest

from tests.integration.test_auth import identity as identity

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


async def test_html_login_forms_csrf_escaping_and_role_navigation(identity, postgres_store):
    from workflow_engine.api import create_app

    auth, admin, settings = identity
    cycle = (
        await __import__("workflow_engine.review_commands", fromlist=["ReviewCommands"])
        .ReviewCommands(auth.database)
        .create_cycle(admin, '<script>alert("unsafe")</script>')
    )
    app = create_app(settings, postgres_store, None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=settings.origin
    ) as client:
        assert (await client.get("/cycles")).status_code == 303
        page = await client.get("/login")
        assert page.status_code == 200
        csrf = re.search(r'name="csrf" value="([a-f0-9]+)"', page.text)[1]
        assert 'type="password"' in page.text
        assert (
            await client.post(
                "/login",
                data={
                    "tenant": "alpha",
                    "login": "admin-alpha",
                    "password": "a-safe-password-123",
                    "csrf": csrf,
                },
            )
        ).status_code == 403
        result = await client.post(
            "/login",
            data={
                "tenant": "alpha",
                "login": "admin-alpha",
                "password": "a-safe-password-123",
                "csrf": csrf,
            },
            headers={"Origin": settings.origin},
        )
        assert result.status_code == 303
        page = await client.get("/cycles")
        assert "Accounts" in page.text
        assert "&lt;script&gt;" in page.text and '<script>alert("unsafe")' not in page.text
        detail = await client.get("/cycles/" + cycle["id"])
        assert "Admit requester" in detail.text
        assert "Review" in detail.text


async def test_form_stale_revision_refreshes_current_state_without_success(
    identity, postgres_store
):
    from workflow_engine.api import create_app
    from workflow_engine.review_commands import ReviewCommands

    auth, admin, settings = identity
    commands = ReviewCommands(auth.database)
    cycle = await commands.create_cycle(admin, "Original")
    context = await auth.pre_session()
    context = await auth.login(
        context.token, context.csrf, "alpha", "admin-alpha", "a-safe-password-123", "local"
    )
    await commands.edit_draft_cycle(admin, cycle["id"], "Changed elsewhere", 0)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings, postgres_store, None)),
        base_url=settings.origin,
        cookies={auth.cookie_name: context.token},
    ) as client:
        result = await client.post(
            f"/cycles/{cycle['id']}/edit",
            data={"csrf": context.csrf, "title": "Stale title", "expected_revision": "0"},
            headers={"Origin": settings.origin},
        )
        assert result.status_code == 409
        assert "Changed elsewhere" in result.text
        assert "not applied" in result.text
        assert (await commands.get_cycle(admin, cycle["id"]))["title"] == "Changed elsewhere"


async def test_html_chain_gaps_are_rejected_without_silently_renumbering(identity, postgres_store):
    from uuid import uuid4

    from workflow_engine.api import create_app
    from workflow_engine.review_commands import ReviewCommands

    auth, admin, settings = identity
    requester = await auth.create_account(
        admin, "gap-requester", "a-safe-password-123", "REQUESTER"
    )
    reviewers = [
        await auth.create_account(admin, f"gap-reviewer-{i}", "a-safe-password-123", "REVIEWER")
        for i in range(2)
    ]
    commands = ReviewCommands(auth.database)
    cycle = await commands.create_cycle(admin, "Gapped chain")
    cycle = await commands.admit_requester(
        admin, cycle["id"], requester.membership_id, cycle["revision"]
    )
    item = await commands.create_item(requester, cycle["id"], cycle["revision"], uuid4())
    pre = await auth.pre_session()
    context = await auth.login(
        pre.token, pre.csrf, "alpha", "admin-alpha", "a-safe-password-123", "local"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings, postgres_store, None)),
        base_url=settings.origin,
        cookies={auth.cookie_name: context.token},
    ) as client:
        result = await client.post(
            f"/items/{item['id']}/chain",
            data={
                "csrf": context.csrf,
                "expected_revision": str(item["revision"]),
                "reviewer_1": str(reviewers[0].membership_id),
                "duration_1": "3600",
                "reviewer_2": "",
                "reviewer_3": str(reviewers[1].membership_id),
                "duration_3": "3600",
            },
            headers={"Origin": settings.origin},
        )
        assert result.status_code == 422
    assert (await commands.get_item(admin, item["id"]))["chain"] == []

from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select, text

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


@pytest.fixture
async def identity(postgres_store):
    from workflow_engine.auth import AuthService
    from workflow_engine.settings import Settings

    # Use an explicit loopback origin; Host alone never changes security mode.
    settings = Settings(env="local", origin="http://127.0.0.1:8000")
    auth = AuthService(postgres_store.database, settings)
    admin = await auth.bootstrap("alpha", "admin-alpha", "a-safe-password-123")
    return auth, admin, settings


async def test_login_uses_hashed_rotated_durable_sessions_and_logout(identity):
    from workflow_engine.auth import AuthenticationFailed, token_hash
    from workflow_engine.identity_models import AuthSession

    auth, admin, _ = identity
    before = await auth.pre_session()
    logged = await auth.login(
        before.token, before.csrf, "alpha", "admin-alpha", "a-safe-password-123", "127.0.0.1"
    )
    assert logged.token != before.token
    assert (await auth.resolve(logged.token)).actor == admin
    async with auth.database.sessions() as session:
        row = (
            await session.execute(
                select(AuthSession).where(AuthSession.token_hash == token_hash(logged.token))
            )
        ).scalar_one()
        assert row.token_hash != logged.token
        assert logged.token not in str(row.__dict__)
    with pytest.raises(AuthenticationFailed):
        await auth.resolve(before.token)
    from workflow_engine.auth import AuthService

    restarted = AuthService(auth.database, auth.settings)
    assert (await restarted.resolve(logged.token)).actor == admin
    await restarted.logout(logged.token, logged.csrf)
    with pytest.raises(AuthenticationFailed):
        await auth.resolve(logged.token)


async def test_login_failures_csrf_expiry_disable_and_no_role_elevation(identity):
    from workflow_engine.auth import AuthenticationFailed
    from workflow_engine.authorization import Denied

    auth, admin, _ = identity
    pre = await auth.pre_session()
    with pytest.raises(Denied):
        await auth.login(pre.token, "wrong", "alpha", "admin-alpha", "wrong", "local")
    with pytest.raises(AuthenticationFailed):
        await auth.login(pre.token, pre.csrf, "alpha", "admin-alpha", "wrong", "local")
    with pytest.raises(Denied):
        await auth.create_account(admin, "another-admin", "a-safe-password-123", "ADMIN")
    logged = await auth.login(
        pre.token, pre.csrf, "alpha", "admin-alpha", "a-safe-password-123", "local"
    )
    with pytest.raises(Denied):
        await auth.logout(logged.token, "wrong")
    async with auth.database.sessions.begin() as session:
        await session.execute(
            text(
                "UPDATE auth_sessions SET idle_expires_at = clock_timestamp() - interval '1 second'"
            )
        )
    with pytest.raises(AuthenticationFailed):
        await auth.resolve(logged.token)


async def test_http_csrf_cookie_flags_origin_spoofing_and_legacy_denial(identity, postgres_store):
    from workflow_engine.api import create_app

    auth, _, settings = identity
    app = create_app(settings, postgres_store, None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=settings.origin
    ) as client:
        assert (await client.get("/api/me")).status_code == 401
        pre = await client.get("/api/session")
        assert "HttpOnly" in pre.headers["set-cookie"]
        assert "SameSite=lax" in pre.headers["set-cookie"]
        payload = {"tenant": "alpha", "login": "admin-alpha", "password": "a-safe-password-123"}
        assert (await client.post("/api/login", json=payload)).status_code == 403
        headers = {"Origin": settings.origin, "X-CSRF-Token": pre.json()["csrf"]}
        assert (
            await client.post("/api/login", json={**payload, "role": "ADMIN"}, headers=headers)
        ).status_code == 422
        assert (
            await client.post(
                "/api/login", json=payload, headers={**headers, "Origin": "https://foreign.example"}
            )
        ).status_code == 403
        login = await client.post("/api/login", json=payload, headers=headers)
        assert login.status_code == 200
        assert (await client.get("/api/me")).json()["role"] == "ADMIN"
        assert (await client.post(f"/workflows/{uuid4()}/approve", json={})).status_code == 410
        assert (
            await client.post(
                "/api/logout",
                headers={"Origin": settings.origin, "X-CSRF-Token": login.json()["csrf"]},
            )
        ).status_code == 204
        assert (await client.get("/api/me")).status_code == 401


async def test_live_membership_and_actor_spoofing_are_rechecked(identity):
    from dataclasses import replace

    from workflow_engine.auth import AuthenticationFailed
    from workflow_engine.authorization import Denied

    auth, admin, _ = identity
    member = await auth.create_account(admin, "requester-alpha", "a-safe-password-123", "REQUESTER")
    with pytest.raises(AuthenticationFailed):
        await auth.create_account(
            replace(member, role="ADMIN"), "spoofed-account", "a-safe-password-123", "REVIEWER"
        )
    pre = await auth.pre_session()
    logged = await auth.login(
        pre.token, pre.csrf, "alpha", "requester-alpha", "a-safe-password-123", "local"
    )
    with pytest.raises(Denied):
        await auth.create_account(member, "forbidden-account", "a-safe-password-123", "REVIEWER")
    async with auth.database.sessions.begin() as session:
        await session.execute(
            text("UPDATE tenant_memberships SET enabled=false WHERE id=:id"),
            {"id": member.membership_id},
        )
    with pytest.raises(AuthenticationFailed):
        await auth.resolve(logged.token)


async def test_absolute_expiry_and_persisted_per_account_throttle(identity):
    from workflow_engine.auth import AuthenticationFailed, AuthService
    from workflow_engine.authorization import Denied

    auth, _, settings = identity
    pre = await auth.pre_session()
    for _ in range(10):
        with pytest.raises(AuthenticationFailed):
            await auth.login(pre.token, pre.csrf, "alpha", "admin-alpha", "incorrect", "local")
    with pytest.raises(Denied) as error:
        await AuthService(auth.database, settings).login(
            pre.token, pre.csrf, "alpha", "admin-alpha", "a-safe-password-123", "different-source"
        )
    assert error.value.status == 429
    async with auth.database.sessions.begin() as session:
        await session.execute(text("UPDATE login_throttles SET attempts=0"))
    logged = await auth.login(
        pre.token, pre.csrf, "alpha", "admin-alpha", "a-safe-password-123", "local"
    )
    async with auth.database.sessions.begin() as session:
        await session.execute(
            text(
                "UPDATE auth_sessions SET absolute_expires_at = "
                "clock_timestamp() - interval '1 second'"
            )
        )
    with pytest.raises(AuthenticationFailed):
        await auth.resolve(logged.token)


async def test_validation_errors_never_echo_passwords(identity, postgres_store):
    from workflow_engine.api import create_app

    _, _, settings = identity
    app = create_app(settings, postgres_store, None)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=settings.origin
    ) as client:
        password = "do-not-echo-this-password"
        result = await client.post(
            "/api/login", json={"password": password, "tenant": [], "login": {}}
        )
        assert result.status_code == 422
        assert password not in result.text
        assert '"input"' not in result.text
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.origin,
        headers={"X-API-Key": "old-key"},
    ) as client:
        assert (await client.get("/api/me")).status_code == 401


async def test_authenticated_cookie_cannot_be_reused_as_login_presession(identity):
    from workflow_engine.auth import AuthenticationFailed

    auth, _, _ = identity
    pre = await auth.pre_session()
    logged = await auth.login(
        pre.token, pre.csrf, "alpha", "admin-alpha", "a-safe-password-123", "local"
    )
    with pytest.raises(AuthenticationFailed):
        await auth.login(
            logged.token, logged.csrf, "alpha", "admin-alpha", "a-safe-password-123", "local"
        )


async def test_account_disable_revokes_sessions_and_does_not_elevate_or_change_role(identity):
    from workflow_engine.auth import AuthenticationFailed
    from workflow_engine.authorization import Denied

    auth, admin, _ = identity
    reviewer = await auth.create_account(
        admin, "disable-reviewer", "a-safe-password-123", "REVIEWER"
    )
    pre = await auth.pre_session()
    logged = await auth.login(
        pre.token, pre.csrf, "alpha", "disable-reviewer", "a-safe-password-123", "local"
    )
    await auth.set_account_enabled(admin, reviewer.membership_id, False)
    with pytest.raises(AuthenticationFailed):
        await auth.resolve(logged.token)
    with pytest.raises(Denied):
        await auth.set_account_enabled(reviewer, admin.membership_id, False)
    with pytest.raises(Denied):
        await auth.set_account_enabled(admin, admin.membership_id, False)
    await auth.set_account_enabled(admin, reviewer.membership_id, True)
    with pytest.raises(AuthenticationFailed):
        await auth.resolve(logged.token)
    pre = await auth.pre_session()
    again = await auth.login(
        pre.token, pre.csrf, "alpha", "disable-reviewer", "a-safe-password-123", "local"
    )
    assert (await auth.resolve(again.token)).actor.role == "REVIEWER"


async def test_interactive_bootstrap_cli_on_fresh_database(postgres_store, monkeypatch, capsys):
    # Windows getpass reads the physical console, not redirected stdin. Substitute
    # that human-input boundary only; execute the real CLI/parser/database lifecycle.
    import asyncio
    import logging
    import sys

    from workflow_engine import bootstrap, cli

    password = "cli-fixture-only-123"
    monkeypatch.setenv(
        "WORKFLOW_DATABASE_URL",
        postgres_store.database.engine.url.render_as_string(hide_password=False),
    )
    monkeypatch.setenv("WORKFLOW_ENV", "local")
    monkeypatch.setenv("WORKFLOW_ORIGIN", "http://localhost")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "workflow-engine",
            "bootstrap-admin",
            "--tenant",
            "cli-bootstrap",
            "--login",
            "cli-administrator",
        ],
    )
    prompts = []

    def read_password(prompt):
        prompts.append(prompt)
        return password

    monkeypatch.setattr(bootstrap.getpass, "getpass", read_password)
    root = logging.getLogger()
    handlers, level = root.handlers[:], root.level
    try:
        await asyncio.to_thread(cli.main)
    finally:
        for handler in root.handlers[:]:
            if handler not in handlers:
                root.removeHandler(handler)
                handler.close()
        root.handlers, root.level = handlers, level
    output = capsys.readouterr()
    assert '"status": "administrator_created"' in output.out
    assert password not in output.out + output.err
    assert len(prompts) == 2

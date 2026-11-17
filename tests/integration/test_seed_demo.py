import pytest
from sqlalchemy import func, select

from tests.postgres_support import PostgresTests

pytestmark = [pytest.mark.integration, pytest.mark.postgres]


@pytest.fixture
async def fresh_review_database():
    from workflow_engine.db import create_database

    with PostgresTests.from_environment() as postgres:
        allocation = postgres.allocate(migrate=True)
        database = create_database(allocation.runtime_url)
        try:
            yield database
        finally:
            await database.close()


async def test_local_seed_creates_two_tenants_stories_and_rerun_preserves_activity(
    fresh_review_database,
):
    from workflow_engine.auth import AuthService
    from workflow_engine.identity_models import Tenant, User
    from workflow_engine.review_commands import ReviewCommands
    from workflow_engine.review_models import ReviewCycle
    from workflow_engine.seed_demo import seed_demo
    from workflow_engine.settings import Settings

    settings = Settings(env="local", origin="http://localhost", allow_demo_seed=True)
    database = fresh_review_database
    seeded = await seed_demo(database, settings)
    assert seeded["created"] is True
    assert len(seeded["credentials"]) == 8
    assert len(seeded["manifest"]["tenants"]) == 2
    async with database.sessions() as session:
        assert (await session.execute(select(func.count()).select_from(User))).scalar_one() == 8
        assert (await session.execute(select(func.count()).select_from(Tenant))).scalar_one() == 2
        assert (
            await session.execute(select(func.count()).select_from(ReviewCycle))
        ).scalar_one() == 14
    credential = next(row for row in seeded["credentials"] if row.login == "tenant-a.admin")
    auth = AuthService(database, settings)
    pre = await auth.pre_session()
    context = await auth.login(
        pre.token,
        pre.csrf,
        credential.tenant,
        credential.login,
        credential.password.get_secret_value(),
        "local",
    )
    commands = ReviewCommands(database)
    tenant = seeded["manifest"]["tenants"][0]
    draft = tenant["cycles"]["draft"]
    cycle = await commands.get_cycle(context.actor, draft)
    await commands.edit_draft_cycle(
        context.actor, draft, "User changed this title", cycle["revision"]
    )
    rerun = await seed_demo(database, settings)
    assert not rerun["created"] and rerun["credentials"] == []
    assert rerun["manifest"] == seeded["manifest"]
    assert (await commands.get_cycle(context.actor, draft))["title"] == "User changed this title"
    assert (await auth.resolve(context.token)).actor == context.actor
    pre = await auth.pre_session()
    await auth.login(
        pre.token,
        pre.csrf,
        credential.tenant,
        credential.login,
        credential.password.get_secret_value(),
        "local",
    )


async def test_seed_refuses_production_and_existing_real_accounts(fresh_review_database):
    from workflow_engine.auth import AuthService
    from workflow_engine.authorization import Denied
    from workflow_engine.domain import Conflict
    from workflow_engine.seed_demo import seed_demo
    from workflow_engine.settings import Settings

    database = fresh_review_database
    with pytest.raises(Denied):
        await seed_demo(database, Settings(origin="https://workflow.example", allow_demo_seed=True))
    local = Settings(env="local", origin="http://localhost", allow_demo_seed=True)
    await AuthService(database, local).bootstrap(
        "real", "real-administrator", "a-safe-password-123"
    )
    with pytest.raises(Conflict):
        await seed_demo(database, local)


async def test_seed_waits_for_planning_claimed_by_another_worker(
    fresh_review_database, monkeypatch
):
    import asyncio

    from workflow_engine.domain import TickResult
    from workflow_engine.review_commands import ReviewCommands
    from workflow_engine.runner import Runner
    from workflow_engine.seed_demo import seed_demo
    from workflow_engine.settings import Settings

    original_tick, original_get = Runner.tick, ReviewCommands.get_item
    release, tasks = asyncio.Event(), []

    async def claimed_elsewhere(runner, *args):
        if tasks:
            return await original_tick(runner, *args)

        async def finish_other_worker():
            await release.wait()
            return await original_tick(runner, *args)

        tasks.append(asyncio.create_task(finish_other_worker()))
        return TickResult(status="not_claimed")

    async def observe_progress(commands, *args, **kwargs):
        item = await original_get(commands, *args, **kwargs)
        if item.get("progress") and item["progress"]["status"] == "PREPARING":
            release.set()
        return item

    monkeypatch.setattr(Runner, "tick", claimed_elsewhere)
    monkeypatch.setattr(ReviewCommands, "get_item", observe_progress)
    try:
        result = await seed_demo(
            fresh_review_database,
            Settings(env="local", origin="http://localhost", allow_demo_seed=True),
        )
        assert result["created"]
    finally:
        release.set()
        await asyncio.gather(*tasks)

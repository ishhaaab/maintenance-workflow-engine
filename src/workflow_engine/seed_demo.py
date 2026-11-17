"""Explicit local-only stories through real commands, never a fixture approval switch."""

import asyncio
import secrets
import time
from dataclasses import dataclass
from uuid import UUID, uuid4

from pydantic import SecretStr
from sqlalchemy import func, select, text

from .audit import append_event
from .auth import AuthService
from .authorization import Denied
from .demo_models import DemoSeedRecord
from .domain import Conflict
from .identity_models import Tenant, User
from .planner import FixturePlanner
from .policy import CatalogProvider
from .postgres_store import PostgresWorkflowStore
from .provenance import demo_activity
from .review_commands import ReviewCommands
from .review_models import ReviewCycle, ReviewItem
from .runner import Runner
from .store import _live
from .version_models import ReviewProgress

SEED_NAME = "maintenance-review-demo-v1"
_LOCK = 679418501209
STORIES = ("draft", "level_one", "level_two", "changes", "rejected", "overdue", "completed")


@dataclass(frozen=True)
class DemoCredential:
    tenant: str
    login: str
    role: str
    password: SecretStr


async def _validate_manifest(session, manifest):
    try:
        tenants = manifest["tenants"]
        if manifest["version"] != 1 or len(tenants) != 2:
            raise ValueError
        for entry in tenants:
            tenant = await session.get(Tenant, entry["id"])
            if (
                tenant is None
                or tenant.slug != entry["slug"]
                or set(entry["cycles"]) != set(STORIES)
            ):
                raise ValueError
            for cycle_id in entry["cycles"].values():
                cycle = await session.get(ReviewCycle, UUID(cycle_id))
                if not cycle or cycle.tenant_id != tenant.id:
                    raise ValueError
    except (KeyError, TypeError, ValueError):
        raise Conflict("demo_seed_manifest_conflict") from None


async def seed_demo(database, settings):
    if settings.env != "local" or not settings.allow_demo_seed:
        raise Denied("explicit_local_demo_seed_required")
    settings.browser_security()
    await database.check_schema()
    async with database.engine.connect() as connection:
        await connection.execution_options(isolation_level="AUTOCOMMIT")
        acquired = (
            await connection.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": _LOCK})
        ).scalar_one()
        if not acquired:
            raise Conflict("demo_seed_busy")
        try:
            async with database.sessions.begin() as session:
                record = await session.get(DemoSeedRecord, SEED_NAME)
                if record:
                    if record.state != "COMPLETE":
                        raise Conflict("demo_seed_incomplete_inspect_without_resetting")
                    await _validate_manifest(session, record.manifest)
                    return {"created": False, "manifest": record.manifest, "credentials": []}
                if (await session.execute(select(User.id).limit(1))).first() or (
                    await session.execute(select(Tenant.id).limit(1))
                ).first():
                    raise Conflict("demo_seed_requires_fresh_identity_database")
                session.add(
                    DemoSeedRecord(
                        name=SEED_NAME,
                        state="STARTED",
                        manifest={},
                        created_at=func.clock_timestamp(),
                    )
                )
            with demo_activity():
                result = await _create_stories(database, settings)
            async with database.sessions.begin() as session:
                record = await session.get(DemoSeedRecord, SEED_NAME)
                record.state, record.manifest = "COMPLETE", result["manifest"]
            return result
        finally:
            await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _LOCK})


async def _create_stories(database, settings):
    auth = AuthService(database, settings)
    catalog = CatalogProvider.from_file(settings.catalog_path)
    commands = ReviewCommands(database, catalog)
    store = PostgresWorkflowStore(database)
    # Planning is read-only. No mutation gateways or model credentials are constructed.
    runner = Runner(store, FixturePlanner.from_file(settings.fixtures_path), catalog, {})
    credentials, tenants = [], []
    for slug, request in (
        (
            "tenant-a",
            "Create an inspection ticket for pump P-17 and reserve one seal kit if available.",
        ),
        (
            "tenant-b",
            "Create an inspection ticket for blower B-22 with normal priority and reserve 1 bearing kit if available",
        ),
    ):
        password = secrets.token_urlsafe(24)
        admin = await auth.bootstrap(slug, slug + ".admin", password)
        credentials.append(DemoCredential(slug, slug + ".admin", "ADMIN", SecretStr(password)))
        actors = {"admin": admin}
        for suffix, role in (
            ("requester", "REQUESTER"),
            ("reviewer-1", "REVIEWER"),
            ("reviewer-2", "REVIEWER"),
        ):
            password = secrets.token_urlsafe(24)
            actors[suffix] = await auth.create_account(admin, slug + "." + suffix, password, role)
            credentials.append(DemoCredential(slug, slug + "." + suffix, role, SecretStr(password)))
        requester, reviewers = actors["requester"], [actors["reviewer-1"], actors["reviewer-2"]]
        cycles = {}
        for story in STORIES:
            title = {
                "draft": "Draft setup",
                "level_one": "First-level review",
                "level_two": "Second-level review",
                "changes": "Changes and resubmission",
                "rejected": "Rejected request",
                "overdue": "Overdue review",
                "completed": "Completed approvals",
            }[story]
            cycle = await commands.create_cycle(admin, title)
            cycle = await commands.admit_requester(
                admin, cycle["id"], requester.membership_id, cycle["revision"]
            )
            item = await commands.create_item(requester, cycle["id"], cycle["revision"], uuid4())
            cycles[story] = cycle["id"]
            if story == "draft":
                continue
            item = await commands.configure_chain(
                admin,
                item["id"],
                [
                    {
                        "reviewer_id": str(r.membership_id),
                        "due_after_s": 1 if story == "overdue" else 172800,
                    }
                    for r in reviewers
                ],
                item["revision"],
            )
            submitted = await commands.submit_version(
                requester, item["id"], request, item["revision"], uuid4()
            )
            item = await _plan_ready(commands, runner, requester, submitted)
            cycle = await commands.get_cycle(admin, cycle["id"])
            await commands.activate_cycle(admin, cycle["id"], cycle["revision"])
            if story in {"level_two", "changes", "completed"}:
                await _decide(commands, reviewers[0], item["id"], "APPROVE")
            if story == "changes":
                await _decide(
                    commands,
                    reviewers[1],
                    item["id"],
                    "REQUEST_CHANGES",
                    "Confirm the maintenance window.",
                )
                current = await commands.get_item(requester, item["id"])
                submitted = await commands.submit_version(
                    requester, item["id"], request, current["revision"], uuid4()
                )
                await _plan_ready(commands, runner, requester, submitted)
            elif story == "rejected":
                await _decide(
                    commands,
                    reviewers[0],
                    item["id"],
                    "REJECT",
                    "This request belongs in a later cycle.",
                )
            elif story == "completed":
                await _decide(commands, reviewers[1], item["id"], "APPROVE")
        # Let the one-second example deadline actually pass, using database time.
        # No backdated rows, fake decisions, or test clock setting enters the CLI.
        while True:
            async with database.sessions() as session:
                deadline = (
                    await session.execute(
                        select(ReviewProgress.deadline_at)
                        .join(
                            ReviewItem, ReviewItem.current_version_id == ReviewProgress.version_id
                        )
                        .where(
                            ReviewItem.tenant_id == admin.tenant_id,
                            ReviewItem.cycle_id == UUID(cycles["overdue"]),
                        )
                    )
                ).scalar_one()
                at = (await session.execute(select(func.clock_timestamp()))).scalar_one()
            if at >= deadline:
                break
            await asyncio.sleep(min(0.2, (deadline - at).total_seconds()))
        async with database.sessions.begin() as session:
            await append_event(session, admin, "tenant", UUID(admin.tenant_id), "demo_seeded", 0)
        tenants.append({"id": admin.tenant_id, "slug": slug, "cycles": cycles})
    return {
        "created": True,
        "manifest": {"version": 1, "tenants": tenants},
        "credentials": credentials,
    }


async def _plan_ready(commands, runner, requester, submitted):
    await runner.tick(submitted["run_id"], "seed-demo-planner")
    timeout = time.monotonic() + 45
    while True:
        item = await commands.get_item(requester, submitted["id"])
        if item["progress"]["status"] == "PENDING":
            run = await runner.store.get(submitted["run_id"], requester.tenant_id)
            async with commands.database.sessions() as session:
                at = (await session.execute(select(func.clock_timestamp()))).scalar_one()
            if not _live(run, at):
                return item
        elif item["progress"]["status"] == "PREPARING":
            # A normal worker may have won the planning claim. Wait for its durable
            # result, or recover the original run after lease expiry; never replan a seal.
            await runner.tick(submitted["run_id"], "seed-demo-planner")
        else:
            raise Conflict("demo_fixture_plan_not_ready")
        if time.monotonic() >= timeout:
            raise Conflict("demo_planning_not_ready_inspect_seed_journal")
        await asyncio.sleep(0.1)


async def _decide(commands, actor, item_id, kind, comment=""):
    item = await commands.get_item(actor, item_id)
    return await commands.decide(
        actor,
        item_id,
        version_id=item["current_version_id"],
        level=item["progress"]["current_level"],
        plan_hash=item["plan_hash"],
        decision=kind,
        comment=comment,
        expected_revision=item["revision"],
        expected_progress_revision=item["progress"]["revision"],
        command_id=uuid4(),
    )

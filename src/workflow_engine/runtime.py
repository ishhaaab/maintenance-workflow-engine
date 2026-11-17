"""Dependency construction and process lifecycle, outside domain logic."""

import asyncio
import logging
from contextlib import AsyncExitStack, asynccontextmanager

import httpx
from redis.asyncio import Redis

from .broker import RedisWakeupBroker, dispatch_once
from .db import SchemaNotCurrent, create_database
from .gateways import HttpEffectGateway
from .planner import FixturePlanner
from .policy import CatalogProvider
from .postgres_store import PostgresWorkflowStore
from .runner import Runner
from .settings import Settings

log = logging.getLogger(__name__)


def _redis(settings):
    return Redis.from_url(
        settings.redis_url.get_secret_value(),
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=3,
    )


def _broker(settings, redis):
    return RedisWakeupBroker(redis, stream=settings.redis_stream, group=settings.redis_group)


def api_application():
    from .api import create_app

    settings = Settings()
    settings.browser_security()
    database = create_database(settings.runtime_database_url())
    store = PostgresWorkflowStore(database)
    # Construct Redis inside the lifespan so failures still close the database.
    broker = _broker(settings, None)
    app = create_app(settings, store, CatalogProvider.from_file(settings.catalog_path), broker)

    @asynccontextmanager
    async def lifespan(_app):
        async with AsyncExitStack() as stack:
            stack.push_async_callback(database.close)
            redis = _redis(settings)
            stack.push_async_callback(redis.aclose)
            broker.redis = redis
            try:
                await store.initialize()
            except SchemaNotCurrent:
                # Keep public liveness/readiness available. No command route can
                # read or mutate executions while the schema is unavailable.
                log.warning("postgres_schema_unavailable")
            yield

    app.router.lifespan_context = lifespan
    return app


@asynccontextmanager
async def resources(settings=None):
    settings = settings or Settings()
    database = create_database(settings.runtime_database_url())
    async with AsyncExitStack() as stack:
        stack.push_async_callback(database.close)
        redis = _redis(settings)
        stack.push_async_callback(redis.aclose)
        store = PostgresWorkflowStore(database)
        await store.initialize()
        gateways = {}
        for kind, url in (
            ("ticket", settings.ticketing_url),
            ("reservation", settings.inventory_url),
        ):
            client = await stack.enter_async_context(
                httpx.AsyncClient(
                    base_url=url,
                    transport=httpx.AsyncHTTPTransport(retries=0),
                    timeout=settings.external_timeout_s,
                    trust_env=False,
                )
            )
            gateways[kind] = HttpEffectGateway(client, kind, settings.external_timeout_s)
        if settings.planner == "fixture":
            planner = FixturePlanner.from_file(settings.fixtures_path)
        else:
            from .planner import ModelPlanner

            client = await stack.enter_async_context(
                httpx.AsyncClient(
                    base_url=settings.model_url.rstrip("/") + "/",
                    transport=httpx.AsyncHTTPTransport(retries=0),
                    timeout=settings.planner_timeout_s,
                    trust_env=False,
                )
            )
            planner = ModelPlanner(client, settings)
        runner = Runner(
            store,
            planner,
            CatalogProvider.from_file(settings.catalog_path),
            gateways,
            settings.lease_s,
            settings.heartbeat_s,
            settings.planner_timeout_s,
        )
        yield store, _broker(settings, redis), runner


async def run_process(command, worker_id, workflow_id=None):
    if command == "notifications":
        from .notifications import NotificationWorker, SmtpSender

        settings = Settings()
        database = create_database(settings.runtime_database_url())
        try:
            await database.check_schema()
            worker = NotificationWorker(database, SmtpSender(settings), settings.origin)
            while True:
                try:
                    result = await worker.dispatch_once(worker_id)
                    if result == "idle":
                        await asyncio.sleep(1)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.warning("notification_dependency_unavailable")
                    await asyncio.sleep(1)
        finally:
            await database.close()
    if command == "init":
        database = create_database(Settings().runtime_database_url())
        try:
            await database.check_schema()
            return {"status": "schema_current"}
        finally:
            await database.close()
    async with resources() as (store, broker, runner):
        if command == "tick":
            return (await runner.tick(workflow_id, worker_id)).model_dump()
        delay = 1
        while True:
            try:
                if command == "dispatcher":
                    await dispatch_once(store, broker)
                    await asyncio.sleep(1)
                else:
                    message = await broker.receive(worker_id)
                    if message:
                        result = await runner.tick(message.wakeup.workflow_id, worker_id)
                        await broker.ack(message.message_id)
                        log.info(
                            "workflow_tick",
                            extra={
                                "workflow_id": message.wakeup.workflow_id,
                                "outcome": result.status,
                            },
                        )
                delay = 1
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("process_dependency_unavailable", extra={"process_role": command})
                await asyncio.sleep(delay)
                delay = min(delay * 2, 8)

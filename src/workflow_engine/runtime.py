"""Dependency construction and process lifecycle, outside domain logic."""
import asyncio
import logging
from contextlib import asynccontextmanager, AsyncExitStack

import httpx
from pymongo import AsyncMongoClient
from redis.asyncio import Redis

from .broker import RedisWakeupBroker, dispatch_once
from .gateways import HttpEffectGateway
from .planner import FixturePlanner
from .policy import CatalogProvider
from .runner import Runner
from .settings import Settings
from .store import MongoWorkflowStore

log = logging.getLogger(__name__)


def _clients(settings):
    mongo = AsyncMongoClient(settings.mongo_url.get_secret_value(), tz_aware=True, serverSelectionTimeoutMS=3000, connectTimeoutMS=3000, socketTimeoutMS=5000, retryWrites=False, retryReads=False)
    redis = Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=True, socket_connect_timeout=3, socket_timeout=3)
    return mongo, redis, MongoWorkflowStore(mongo[settings.mongo_database])


def api_application():
    from .api import create_app
    settings = Settings()
    mongo, redis, store = _clients(settings)
    app = create_app(settings, store, CatalogProvider.from_file(settings.catalog_path), RedisWakeupBroker(redis))
    @asynccontextmanager
    async def lifespan(_app):
        try:
            await store.initialize()
            yield
        finally:
            await redis.aclose()
            await mongo.close()
    app.router.lifespan_context = lifespan
    return app


@asynccontextmanager
async def resources(settings=None):
    settings = settings or Settings()
    mongo, redis, store = _clients(settings)
    try:
        await store.initialize()
        async with AsyncExitStack() as stack:
            gateways = {}
            for kind, url in (("ticket", settings.ticketing_url), ("reservation", settings.inventory_url)):
                client = await stack.enter_async_context(httpx.AsyncClient(base_url=url, transport=httpx.AsyncHTTPTransport(retries=0), timeout=settings.external_timeout_s, trust_env=False))
                gateways[kind] = HttpEffectGateway(client, kind, settings.external_timeout_s)
            if settings.planner == "fixture":
                planner = FixturePlanner.from_file(settings.fixtures_path)
            else:
                from .planner import ModelPlanner
                client = await stack.enter_async_context(httpx.AsyncClient(base_url=settings.model_url.rstrip("/") + "/", transport=httpx.AsyncHTTPTransport(retries=0), timeout=settings.planner_timeout_s, trust_env=False))
                planner = ModelPlanner(client, settings)
            runner = Runner(store, planner, CatalogProvider.from_file(settings.catalog_path), gateways, settings.lease_s, settings.heartbeat_s, settings.planner_timeout_s)
            yield store, RedisWakeupBroker(redis), runner
    finally:
        await redis.aclose()
        await mongo.close()


async def run_process(command, worker_id, workflow_id=None):
    async with resources() as (store, broker, runner):
        if command == "init":
            return {"status": "initialized"}
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
                        log.info("workflow_tick", extra={"workflow_id": message.wakeup.workflow_id, "outcome": result.status})
                delay = 1
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("process_dependency_unavailable", extra={"process": command})
                await asyncio.sleep(delay)
                delay = min(delay * 2, 8)

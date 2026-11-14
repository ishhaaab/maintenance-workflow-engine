import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest


def pytest_asyncio_loop_factories(config, item):
    # Psycopg requires a selector on Windows; leave other tests on their normal loop.
    if sys.platform == "win32" and item.get_closest_marker("postgres"):
        return {"postgres": asyncio.SelectorEventLoop}
    return {"default": asyncio.new_event_loop}


class Clock:
    def __init__(self):
        self.now = datetime.now(UTC)

    def __call__(self):
        return self.now

    def advance(self, seconds=31):
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
async def postgres_store():
    from tests.postgres_support import PostgresTests, runtime_store

    with PostgresTests.from_environment() as postgres:
        async with runtime_store(postgres) as store:
            allocations = list(postgres._allocations.values())
            yield store
    # Check only capabilities returned in this fixture, never a test-name prefix.
    with postgres.connect_admin() as connection:
        for allocation in allocations:
            assert connection.execute(
                "SELECT count(*) FROM pg_database WHERE datname = %s", (allocation.name,)
            ).fetchone() == (0,)
            assert connection.execute(
                "SELECT count(*) FROM pg_roles WHERE rolname IN (%s, %s)",
                (allocation.owner_role, allocation.runtime_role),
            ).fetchone() == (0,)
    if output := os.environ.get("FAULT_ARTIFACT_DIR"):
        directory = Path(output)
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / "cleanup.jsonl").open("a", encoding="utf-8") as file:
            for allocation in allocations:
                file.write(
                    json.dumps({"database": allocation.name, "verified_absent": True}) + "\n"
                )


@pytest.fixture
async def mongo_store():
    from pymongo import AsyncMongoClient

    from workflow_engine.store import MongoWorkflowStore

    url = os.environ.get("TEST_MONGO_URL")
    if not url:
        pytest.skip(
            "Historical MongoDB check; set TEST_MONGO_URL explicitly, not PostgreSQL evidence"
        )
    client = AsyncMongoClient(url, tz_aware=True, serverSelectionTimeoutMS=3000)
    database = "workflow_test_" + uuid4().hex
    store = MongoWorkflowStore(client[database])
    await store.initialize()
    try:
        yield store
    finally:
        # Only this fixture's random, newly allocated database is removed.
        await client.drop_database(database)
        await client.close()

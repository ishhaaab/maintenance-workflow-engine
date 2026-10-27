import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest


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
async def mongo_store():
    from pymongo import AsyncMongoClient

    from workflow_engine.store import MongoWorkflowStore

    url = os.environ.get("TEST_MONGO_URL")
    if not url:
        pytest.skip("Set TEST_MONGO_URL to run real MongoDB checks")
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

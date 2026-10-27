import os
from uuid import uuid4

import pytest
from redis.asyncio import Redis

from workflow_engine.broker import RedisWakeupBroker
from workflow_engine.domain import Wakeup

pytestmark = pytest.mark.integration


async def test_real_stream_duplicate_pending_reclaim_and_recreation():
    url = os.environ.get("TEST_REDIS_URL")
    if not url:
        pytest.skip("Set TEST_REDIS_URL to run Redis Streams checks")
    redis = Redis.from_url(url, decode_responses=True)
    stream = "workflow-test-" + uuid4().hex
    broker = RedisWakeupBroker(redis, stream=stream, reclaim_idle_ms=0)
    try:
        await broker.publish(Wakeup(workflow_id="w", wake_seq=1))
        message = await broker.receive("dead-worker")
        reclaimed = await broker.receive("replacement")
        assert reclaimed.message_id == message.message_id
        await broker.ack(reclaimed.message_id)
        assert await redis.xlen(stream) == 0
        await redis.delete(stream)
        await broker.publish(Wakeup(workflow_id="w", wake_seq=0))
        assert (await broker.receive("worker")).wakeup.wake_seq == 0
    finally:
        await redis.delete(stream)
        await redis.aclose()

"""Replaceable Redis wakeups. MongoDB due intent repairs every publication gap."""

from typing import Protocol

from pydantic import ValidationError
from redis.exceptions import ResponseError

from .domain import Model, Wakeup
from .store import WorkflowStore


class BrokerMessage(Model):
    message_id: str
    wakeup: Wakeup


class WakeupBroker(Protocol):
    async def publish(self, wakeup: Wakeup) -> str: ...
    async def receive(self, consumer: str) -> BrokerMessage | None: ...
    async def ack(self, message_id: str) -> None: ...


async def dispatch_once(store: WorkflowStore, broker: WakeupBroker) -> int:
    wakeups = await store.due_wakeups()
    for wakeup in wakeups:
        await broker.publish(wakeup)
    return len(wakeups)


class RedisWakeupBroker:
    def __init__(
        self, redis, stream="workflow-wakeups", group="workflow-workers", reclaim_idle_ms=60000
    ):
        self.redis, self.stream, self.group = redis, stream, group
        self.reclaim_idle_ms = reclaim_idle_ms
        self._cursor = "0-0"

    async def initialize(self):
        try:
            await self.redis.xgroup_create(self.stream, self.group, id="0", mkstream=True)
        except ResponseError as error:
            if "BUSYGROUP" not in str(error):
                raise

    async def publish(self, wakeup: Wakeup) -> str:
        return await self.redis.xadd(self.stream, wakeup.model_dump())

    async def receive(self, consumer: str) -> BrokerMessage | None:
        await self.initialize()
        try:
            claimed = await self.redis.xautoclaim(
                self.stream, self.group, consumer, self.reclaim_idle_ms, self._cursor, count=1
            )
            self._cursor, messages = claimed[0], claimed[1]
            if not messages:
                response = await self.redis.xreadgroup(
                    self.group, consumer, {self.stream: ">"}, count=1, block=1000
                )
                messages = response[0][1] if response else []
        except ResponseError as error:
            if "NOGROUP" in str(error):
                return None  # A Redis reset raced group creation. Next receive recreates it.
            raise
        if not messages:
            return None
        message_id, data = messages[0]
        try:
            return BrokerMessage(message_id=message_id, wakeup=Wakeup.model_validate(data))
        except ValidationError:
            # No action arguments are accepted, including in malformed messages.
            await self.ack(message_id)
            return None

    async def ack(self, message_id: str):
        async with self.redis.pipeline(transaction=False) as pipeline:
            pipeline.xack(self.stream, self.group, message_id)
            pipeline.xdel(self.stream, message_id)
            await pipeline.execute()

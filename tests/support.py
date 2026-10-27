from contextlib import AsyncExitStack
from dataclasses import dataclass

import httpx

from external_simulators.inventory import create_app as inventory_app
from external_simulators.ticketing import create_app as ticket_app
from workflow_engine.domain import DomainEvent, Workflow, WorkflowCreate, transition
from workflow_engine.gateways import HttpEffectGateway
from workflow_engine.planner import FixturePlanner
from workflow_engine.policy import CatalogProvider
from workflow_engine.runner import Runner
from workflow_engine.store import MemoryWorkflowStore

REQUEST = "Create an inspection ticket for pump P-17 and reserve one seal kit if available."


@dataclass
class Harness:
    store: object
    runner: Runner
    ticketing: httpx.AsyncClient
    inventory: httpx.AsyncClient
    clock: object

    async def admit(self, key="key"):
        return (
            await self.store.insert_or_get(
                Workflow.new("tenant-a", key, WorkflowCreate(text=REQUEST))
            )
        ).workflow

    async def get(self, workflow):
        return await self.store.get(workflow.workflow_id, workflow.tenant_id)

    async def tick(self, workflow):
        result = await self.runner.tick(workflow.workflow_id, "worker")
        return result, await self.get(workflow)

    async def approve(self, workflow):
        workflow = await self.get(workflow)
        return await self.store.save(
            transition(workflow, DomainEvent(kind="approved", plan_hash=workflow.plan_hash)),
            workflow.revision,
            None,
        )


async def harness(tmp_path, clock, stack: AsyncExitStack, store=None, stock=100):
    store = store or MemoryWorkflowStore(clock)
    ticketing = await stack.enter_async_context(
        httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=ticket_app(tmp_path / "tickets.sqlite", enable_faults=True)
            ),
            base_url="http://tickets",
        )
    )
    inventory = await stack.enter_async_context(
        httpx.AsyncClient(
            transport=httpx.ASGITransport(
                app=inventory_app(
                    tmp_path / "inventory.sqlite",
                    enable_faults=True,
                    initial_stock={"SEAL-1": stock},
                )
            ),
            base_url="http://inventory",
        )
    )
    runner = Runner(
        store,
        FixturePlanner.from_file("fixtures/planner_dev.jsonl"),
        CatalogProvider.from_file("fixtures/catalog.jsonl"),
        {
            "ticket": HttpEffectGateway(ticketing, "ticket"),
            "reservation": HttpEffectGateway(inventory, "reservation"),
        },
    )
    return Harness(store, runner, ticketing, inventory, clock)

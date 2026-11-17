"""Thin authenticated JSON adapters for review commands."""

from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import Field

from .auth_api import authenticated
from .domain import Model


class CycleCreate(Model):
    title: str = Field(min_length=1, max_length=160)


class RevisionBody(Model):
    expected_revision: int = Field(ge=0, strict=True)


class CycleEdit(RevisionBody):
    title: str = Field(min_length=1, max_length=160)


class RequesterBody(RevisionBody):
    requester_id: UUID


class ItemCreate(RevisionBody):
    command_id: UUID


class ChainStep(Model):
    reviewer_id: UUID
    due_after_s: int = Field(default=172800, ge=1, le=2592000, strict=True)


class ChainBody(RevisionBody):
    steps: list[ChainStep] = Field(min_length=1, max_length=5)


class VersionBody(RevisionBody):
    text: str = Field(min_length=1, max_length=2000)
    command_id: UUID


class DecisionBody(RevisionBody):
    version_id: UUID
    level: int = Field(ge=1, le=5, strict=True)
    plan_hash: str = Field(pattern="^[a-f0-9]{64}$")
    decision: str = Field(pattern="^(APPROVE|REJECT|REQUEST_CHANGES)$")
    comment: str = Field(default="", max_length=2000)
    expected_progress_revision: int = Field(ge=0, strict=True)
    command_id: UUID


def routes(commands):
    router = APIRouter(prefix="/api")

    @router.get("/cycles")
    async def cycles(request: Request, limit: int = 50, offset: int = 0, state: str | None = None):
        actor = (await authenticated(request)).actor
        return await commands.list_cycles(actor, limit=limit, offset=offset, state=state)

    @router.post("/cycles", status_code=201)
    async def create_cycle(body: CycleCreate, request: Request):
        actor = (await authenticated(request, mutation=True)).actor
        return await commands.create_cycle(actor, body.title)

    @router.get("/cycles/{cycle_id}")
    async def cycle(cycle_id: UUID, request: Request, limit: int = 50, offset: int = 0):
        return await commands.get_cycle(
            (await authenticated(request)).actor, cycle_id, limit=limit, offset=offset
        )

    @router.patch("/cycles/{cycle_id}")
    async def edit_cycle(cycle_id: UUID, body: CycleEdit, request: Request):
        return await commands.edit_draft_cycle(
            (await authenticated(request, mutation=True)).actor,
            cycle_id,
            body.title,
            body.expected_revision,
        )

    @router.post("/cycles/{cycle_id}/requesters")
    async def admit(cycle_id: UUID, body: RequesterBody, request: Request):
        return await commands.admit_requester(
            (await authenticated(request, mutation=True)).actor,
            cycle_id,
            body.requester_id,
            body.expected_revision,
        )

    @router.post("/cycles/{cycle_id}/activate")
    async def activate(cycle_id: UUID, body: RevisionBody, request: Request):
        return await commands.activate_cycle(
            (await authenticated(request, mutation=True)).actor, cycle_id, body.expected_revision
        )

    @router.post("/cycles/{cycle_id}/items", status_code=201)
    async def create_item(cycle_id: UUID, body: ItemCreate, request: Request):
        return await commands.create_item(
            (await authenticated(request, mutation=True)).actor,
            cycle_id,
            body.expected_revision,
            body.command_id,
        )

    @router.get("/items/{item_id}")
    async def item(item_id: UUID, request: Request):
        return await commands.get_item((await authenticated(request)).actor, item_id)

    @router.get("/pending-reviews")
    async def pending(request: Request, limit: int = 50, offset: int = 0):
        return await commands.pending_reviews(
            (await authenticated(request)).actor, limit=limit, offset=offset
        )

    @router.get("/members")
    async def members(request: Request):
        return await commands.list_members((await authenticated(request)).actor)

    @router.put("/items/{item_id}/chain")
    async def chain(item_id: UUID, body: ChainBody, request: Request):
        return await commands.configure_chain(
            (await authenticated(request, mutation=True)).actor,
            item_id,
            [step.model_dump() for step in body.steps],
            body.expected_revision,
        )

    @router.post("/items/{item_id}/versions", status_code=201)
    async def version(item_id: UUID, body: VersionBody, request: Request):
        return await commands.submit_version(
            (await authenticated(request, mutation=True)).actor,
            item_id,
            body.text,
            body.expected_revision,
            body.command_id,
        )

    @router.post("/items/{item_id}/decisions")
    async def decision(item_id: UUID, body: DecisionBody, request: Request):
        return await commands.decide(
            (await authenticated(request, mutation=True)).actor, item_id, **body.model_dump()
        )

    return router

"""Tenant-scoped durable commands. Admission never calls the planner or gateways."""

import secrets

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pymongo.errors import PyMongoError

from .domain import (
    ApprovalRequest,
    CancelRequest,
    Conflict,
    DomainEvent,
    NotFound,
    Workflow,
    WorkflowCreate,
    transition,
)


class BodyLimit:
    def __init__(self, app, limit=16384):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        messages, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            size += len(message.get("body", b""))
            if size > self.limit:
                return await JSONResponse(
                    {"detail": "Request body exceeds 16 KiB"}, status_code=413
                )(scope, receive, send)
            messages.append(message)
            if not message.get("more_body", False):
                break

        async def replay():
            return messages.pop(0) if messages else await receive()

        return await self.app(scope, replay, send)


def _public(workflow):
    return workflow.model_dump(mode="json", exclude={"lease_token", "lease_owner", "admission_key"})


def create_app(settings, store, catalog, broker=None):
    app = FastAPI(title="Maintenance workflow engine", version="0.1.0")
    app.add_middleware(BodyLimit)

    async def tenant(x_api_key: str | None = Header(default=None)):
        if x_api_key:
            for key, tenant_id in settings.api_keys.items():
                if secrets.compare_digest(key.encode(), x_api_key.encode()):
                    return tenant_id
        raise HTTPException(401, "Invalid API key")

    @app.exception_handler(Conflict)
    async def conflict(_request, _error):
        return JSONResponse(
            {"detail": "Revision, state, approval or lease conflict"}, status_code=409
        )

    @app.exception_handler(NotFound)
    async def missing(_request, _error):
        return JSONResponse({"detail": "Workflow not found"}, status_code=404)

    @app.exception_handler(PyMongoError)
    async def unavailable(_request, _error):
        return JSONResponse({"detail": "Durable store unavailable"}, status_code=503)

    @app.post("/workflows")
    async def admit(
        request: WorkflowCreate,
        tenant_id: str = Depends(tenant),
        idempotency_key: str | None = Header(default=None),
    ):
        if (
            not idempotency_key
            or not 1 <= len(idempotency_key) <= 128
            or not idempotency_key.strip()
        ):
            raise HTTPException(400, "Idempotency-Key must contain 1..128 characters")
        catalog.get(tenant_id)
        result = await store.insert_or_get(Workflow.new(tenant_id, idempotency_key, request))
        return JSONResponse(_public(result.workflow), status_code=202 if result.created else 200)

    @app.get("/workflows/{workflow_id}")
    async def get(workflow_id: str, tenant_id: str = Depends(tenant)):
        return JSONResponse(_public(await store.get(workflow_id, tenant_id)))

    @app.post("/workflows/{workflow_id}/approve")
    async def approve(workflow_id: str, request: ApprovalRequest, tenant_id: str = Depends(tenant)):
        workflow = await store.get(workflow_id, tenant_id)
        if workflow.approved_plan_hash == request.plan_hash:
            return JSONResponse(_public(workflow), status_code=200)
        if workflow.revision != request.expected_revision:
            raise Conflict("Stale revision")
        proposed = transition(workflow, DomainEvent(kind="approved", plan_hash=request.plan_hash))
        try:
            saved = await store.save(proposed, request.expected_revision, None)
        except Conflict:
            latest = await store.get(workflow_id, tenant_id)
            if latest.approved_plan_hash == request.plan_hash:
                return JSONResponse(_public(latest), status_code=200)
            raise
        return JSONResponse(_public(saved), status_code=202)

    @app.post("/workflows/{workflow_id}/cancel")
    async def cancel(workflow_id: str, request: CancelRequest, tenant_id: str = Depends(tenant)):
        workflow = await store.get(workflow_id, tenant_id)
        if workflow.state == "CANCELLED":
            return _public(workflow)
        if workflow.revision != request.expected_revision:
            raise Conflict("Stale revision")
        return _public(
            await store.save(
                transition(workflow, DomainEvent(kind="cancelled")), request.expected_revision, None
            )
        )

    @app.get("/health/live")
    async def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready():
        try:
            await store.ping()
        except Exception:
            return JSONResponse(
                {"status": "unavailable", "mongo": False, "broker": False}, status_code=503
            )
        available = False
        if broker:
            try:
                available = bool(await broker.redis.ping())
            except Exception:
                pass
        return {"status": "ready" if available else "degraded", "mongo": True, "broker": available}

    return app

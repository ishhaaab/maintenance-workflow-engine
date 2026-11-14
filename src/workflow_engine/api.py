"""Phase 1 health endpoints and closed standalone workflow commands."""

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse


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


def create_app(settings, store, catalog, broker=None):
    # Keep the embedding call signature, but neither settings nor catalog supplies
    # identity or an alternate route mode. Legacy keys and cookies grant nothing.
    app = FastAPI(title="Maintenance workflow engine", version="0.1.0")
    app.add_middleware(BodyLimit)

    @app.post("/workflows")
    async def admit():
        raise HTTPException(410, "Standalone admission is unavailable pending identity and review")

    @app.get("/workflows/{workflow_id}")
    async def get(workflow_id: str):
        raise HTTPException(401, "Workflow access requires identity and review")

    @app.post("/workflows/{workflow_id}/approve")
    async def approve(workflow_id: str):
        raise HTTPException(410, "Direct approval is unavailable pending identity and review")

    @app.post("/workflows/{workflow_id}/cancel")
    async def cancel(workflow_id: str):
        raise HTTPException(401, "Workflow access requires identity and review")

    @app.get("/health/live")
    async def live():
        return {"status": "ok"}

    @app.get("/health/ready")
    async def ready():
        try:
            if not await store.ping():
                raise ConnectionError
        except Exception:
            return JSONResponse(
                {"status": "unavailable", "postgres": False, "broker": False}, status_code=503
            )
        available = False
        if broker:
            try:
                available = bool(await broker.redis.ping())
            except Exception:
                pass
        return {
            "status": "ready" if available else "degraded",
            "postgres": True,
            "broker": available,
        }

    return app

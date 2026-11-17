"""Same-origin review application, fail-closed schema gate and retired bypass routes."""

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import SQLAlchemyError


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

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request, error):
        # FastAPI's default includes offending inputs, including passwords/tokens.
        return JSONResponse(
            {"detail": [{"loc": item["loc"], "type": item["type"]} for item in error.errors()]},
            status_code=422,
        )

    @app.exception_handler(SQLAlchemyError)
    async def database_unavailable(_request, _error):
        return JSONResponse({"detail": "database_unavailable"}, status_code=503)

    from .authorization import Denied
    from .domain import Conflict, NotFound

    @app.exception_handler(Denied)
    async def denied(_request, error):
        if _request.url.path.startswith(
            ("/cycles", "/items", "/pending", "/admin", "/audit", "/login", "/logout")
        ):
            from .web import error_response

            return await error_response(_request, error)
        return JSONResponse({"detail": error.code}, status_code=error.status)

    @app.exception_handler(Conflict)
    async def conflict(_request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.exception_handler(NotFound)
    async def not_found(_request, error):
        return JSONResponse({"detail": "not_found"}, status_code=404)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        from .db import SchemaNotCurrent

        database = getattr(store, "database", None)
        if database is not None and not request.url.path.startswith(("/health/", "/static/")):
            try:
                await database.check_schema()
            except SchemaNotCurrent:
                response = JSONResponse({"detail": "database_schema_unavailable"}, status_code=503)
            else:
                response = await call_next(request)
        else:
            response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'none'; style-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    if getattr(store, "database", None) is not None and settings.origin:
        from .auth import AuthService
        from .auth_api import routes

        app.state.auth = AuthService(store.database, settings)
        app.include_router(routes(app.state.auth))
        from .review_api import routes as review_routes
        from .review_commands import ReviewCommands

        app.state.review = ReviewCommands(store.database, catalog)
        app.include_router(review_routes(app.state.review))
        from .export_api import routes as export_routes

        app.include_router(export_routes(store.database))
        from .web import register

        register(app)

    @app.post("/workflows")
    async def admit():
        raise HTTPException(410, "Standalone admission is unavailable pending identity and review")

    @app.get("/workflows/{workflow_id}")
    async def get(workflow_id: str, request: Request):
        if hasattr(app.state, "review"):
            from .auth_api import authenticated

            return await app.state.review.get_execution(
                (await authenticated(request)).actor, workflow_id
            )
        raise HTTPException(401, "Workflow access requires identity and review")

    @app.post("/workflows/{workflow_id}/approve")
    async def approve(workflow_id: str):
        raise HTTPException(410, "Direct approval is unavailable pending identity and review")

    @app.post("/workflows/{workflow_id}/cancel")
    async def cancel(workflow_id: str, request: Request):
        if hasattr(app.state, "review"):
            from .auth_api import authenticated
            from .domain import CancelRequest

            context = await authenticated(request, mutation=True)
            try:
                body = CancelRequest.model_validate(await request.json())
            except (ValueError, TypeError):
                raise HTTPException(422, "invalid_cancellation_request") from None
            return await app.state.review.cancel_execution(
                context.actor, workflow_id, body.expected_revision
            )
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

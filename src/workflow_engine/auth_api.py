"""Same-origin JSON authentication. Browser forms call the same AuthService."""

from dataclasses import asdict
from urllib.parse import urlsplit
from uuid import UUID

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from pydantic import Field

from .auth import AuthenticationFailed
from .authorization import Denied
from .domain import Model


class LoginBody(Model):
    tenant: str = Field(min_length=1, max_length=80)
    login: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=256, repr=False)


class AccountBody(Model):
    login: str = Field(min_length=3, max_length=100)
    password: str = Field(min_length=12, max_length=256, repr=False)
    role: str = Field(pattern="^(REQUESTER|REVIEWER)$")


class AccountEnabledBody(Model):
    enabled: bool = Field(strict=True)


def check_origin(request: Request, settings):
    origin = request.headers.get("origin")
    if origin != settings.origin.rstrip("/"):
        raise Denied("origin_failed")
    referer = request.headers.get("referer")
    if referer:
        parsed = urlsplit(referer)
        if f"{parsed.scheme}://{parsed.netloc}" != settings.origin.rstrip("/"):
            raise Denied("origin_failed")


def set_cookie(response, auth, context):
    response.set_cookie(
        auth.cookie_name,
        context.token,
        secure=auth.secure,
        httponly=True,
        samesite="lax",
        path="/",
        max_age=12 * 3600,
    )


async def authenticated(request: Request, *, mutation=False, csrf=None):
    auth = request.app.state.auth
    if mutation:
        csrf = csrf if csrf is not None else request.headers.get("x-csrf-token", "")
    context = await auth.resolve(request.cookies.get(auth.cookie_name), csrf if mutation else None)
    if mutation:
        check_origin(request, auth.settings)
    return context


def routes(auth):
    router = APIRouter(prefix="/api")

    @router.get("/session")
    async def pre_session(request: Request):
        try:
            context = await authenticated(request)
        except AuthenticationFailed:
            context = await auth.pre_session()
        response = JSONResponse({"csrf": context.csrf}, headers={"Cache-Control": "no-store"})
        set_cookie(response, auth, context)
        return response

    @router.post("/login")
    async def login(body: LoginBody, request: Request):
        check_origin(request, auth.settings)
        context = await auth.login(
            request.cookies.get(auth.cookie_name),
            request.headers.get("x-csrf-token", ""),
            body.tenant,
            body.login,
            body.password,
            request.client.host if request.client else "unknown",
        )
        response = JSONResponse(
            {"csrf": context.csrf, "role": context.actor.role},
            headers={"Cache-Control": "no-store"},
        )
        set_cookie(response, auth, context)
        return response

    @router.get("/me")
    async def me(request: Request):
        context = await authenticated(request)
        return {**asdict(context.actor), "csrf": context.csrf}

    @router.post("/logout", status_code=204)
    async def logout(request: Request):
        context = await authenticated(request, mutation=True)
        await auth.logout(context.token, context.csrf)
        response = Response(status_code=204)
        response.delete_cookie(
            auth.cookie_name, secure=auth.secure, httponly=True, samesite="lax", path="/"
        )
        return response

    @router.post("/accounts", status_code=201)
    async def create_account(body: AccountBody, request: Request):
        context = await authenticated(request, mutation=True)
        return asdict(
            await auth.create_account(context.actor, body.login, body.password, body.role)
        )

    @router.get("/audit")
    async def audit(request: Request, limit: int = 50, cursor: str | None = None):
        from .audit import list_events

        context = await authenticated(request)
        return await list_events(auth.database, context.actor, limit=limit, cursor=cursor)

    @router.post("/accounts/{membership_id}/enabled")
    async def account_enabled(membership_id: UUID, body: AccountEnabledBody, request: Request):
        context = await authenticated(request, mutation=True)
        return await auth.set_account_enabled(context.actor, membership_id, body.enabled)

    return router

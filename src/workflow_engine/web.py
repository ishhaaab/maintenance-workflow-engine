"""Same-origin, script-free HTML forms over the authenticated command interfaces."""

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .auth import AuthenticationFailed
from .auth_api import authenticated, check_origin, set_cookie
from .authorization import Denied
from .domain import Conflict
from .identity_models import Tenant, User

ROOT = Path(__file__).parent
templates = Jinja2Templates(directory=str(ROOT / "templates"))
templates.env.filters["utc"] = lambda value: (
    value.astimezone(UTC).strftime("%d %b %Y, %H:%M UTC") if value else "Not set"
)
templates.env.filters["label"] = lambda value: (
    value.replace("_", " ").title() if value else "Not submitted"
)
templates.env.filters["pretty"] = lambda value: json.dumps(
    value, ensure_ascii=False, indent=2, default=str
)


async def page(request, name, context=None, *, status=200, **values):
    profile = None
    if context and context.actor:
        async with request.app.state.auth.database.sessions() as session:
            user = await session.get(User, context.actor.user_id)
            tenant = await session.get(Tenant, context.actor.tenant_id)
            profile = {"login": user.login, "tenant": tenant.slug}
    return templates.TemplateResponse(
        request=request,
        name=name,
        context={
            "actor": context.actor if context else None,
            "csrf": context.csrf if context else None,
            "profile": profile,
            "command_id": str(uuid4()),
            "clock": datetime.now(UTC),
            **values,
        },
        status_code=status,
    )


async def error_response(request, error):
    status = getattr(error, "status", 409)
    if status == 401 and request.url.path != "/login":
        return RedirectResponse("/login", status_code=303)
    try:
        context = await authenticated(request)
    except Denied:
        context = None
    return await page(
        request,
        "error.html",
        context,
        status=status,
        title="Request not applied",
        error="The request was not applied. "
        + (
            "This resource is unavailable."
            if status == 404
            else "The report exceeds the documented export limit. Nothing was truncated."
            if status == 413
            else "The PDF font cannot render this content. Download CSV instead."
            if getattr(error, "code", "") == "pdf_unsupported_characters_use_csv"
            else "Approval levels must be consecutive. Leave only trailing levels unassigned."
            if getattr(error, "code", "") == "approval_levels_must_be_contiguous"
            else "Check your permissions and form values, then try again."
        ),
    )


def integer(form, key):
    value = form.get(key, "")
    if not value.isascii() or not value.isdigit() or len(value) > 12:
        raise Denied("invalid_form", 422)
    return int(value)


def register(app):
    router = APIRouter()
    auth, commands = app.state.auth, app.state.review
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")

    async def render_cycles(request, context, *, error=None, status=200):
        offset = int(request.query_params.get("offset", "0"))
        result = await commands.list_cycles(
            context.actor, offset=offset, state=request.query_params.get("state") or None
        )
        return await page(
            request,
            "cycles.html",
            context,
            title="Review cycles",
            error=error,
            status=status,
            result=result,
        )

    async def render_cycle(request, context, cycle_id, *, error=None, status=200):
        result = await commands.get_cycle(
            context.actor, cycle_id, offset=int(request.query_params.get("offset", "0"))
        )
        members = (
            await commands.list_members(context.actor) if context.actor.role == "ADMIN" else []
        )
        return await page(
            request,
            "cycle.html",
            context,
            title=result["title"],
            cycle=result,
            members=members,
            error=error,
            status=status,
        )

    async def render_item(request, context, item_id, *, error=None, status=200):
        item = await commands.get_item(context.actor, item_id)
        members = (
            await commands.list_members(context.actor) if context.actor.role == "ADMIN" else []
        )
        return await page(
            request,
            "item.html",
            context,
            title="Maintenance item",
            item=item,
            members=members,
            error=error,
            status=status,
        )

    async def render_admin(request, context, *, error=None, status=200):
        members = await commands.list_members(context.actor)
        return await page(
            request,
            "admin.html",
            context,
            title="Accounts",
            members=members,
            error=error,
            status=status,
        )

    async def action(request, operation, destination, refresh):
        form = await request.form(max_fields=40, max_files=0)
        context = await authenticated(request, mutation=True, csrf=form.get("csrf", ""))
        try:
            result = await operation(context.actor, form)
        except Conflict:
            # Show CURRENT data and new revision/version fields, never a success redirect.
            return await refresh(
                context,
                "This page changed or the action is unavailable. Your action was not applied. Review the refreshed state before trying again.",
                409,
            )
        except (ValueError, KeyError, TypeError):
            return await refresh(
                context, "Your action was not applied. Check the form values.", 422
            )
        return RedirectResponse(destination(result), status_code=303)

    @router.get("/")
    async def home():
        return RedirectResponse("/cycles", status_code=303)

    @router.get("/login")
    async def login_page(request: Request):
        try:
            await authenticated(request)
            return RedirectResponse("/cycles", status_code=303)
        except AuthenticationFailed:
            context = await auth.pre_session()
            response = await page(request, "login.html", context, title="Sign in")
            set_cookie(response, auth, context)
            return response

    @router.post("/login")
    async def login(request: Request):
        check_origin(request, auth.settings)
        form = await request.form(max_fields=8, max_files=0)
        try:
            context = await auth.login(
                request.cookies.get(auth.cookie_name),
                form.get("csrf", ""),
                form.get("tenant", "")[:80],
                form.get("login", "")[:100],
                form.get("password", "")[:257],
                request.client.host if request.client else "unknown",
            )
        except AuthenticationFailed:
            context = await auth.pre_session()
            response = await page(
                request,
                "login.html",
                context,
                status=401,
                title="Sign in",
                error="Unable to sign in. Check your tenant, login and password.",
            )
        else:
            response = RedirectResponse("/cycles", status_code=303)
        set_cookie(response, auth, context)
        return response

    @router.post("/logout")
    async def logout(request: Request):
        form = await request.form(max_fields=2, max_files=0)
        context = await authenticated(request, mutation=True, csrf=form.get("csrf", ""))
        await auth.logout(context.token, context.csrf)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(
            auth.cookie_name, path="/", secure=auth.secure, httponly=True, samesite="lax"
        )
        return response

    @router.get("/cycles")
    async def cycles(request: Request):
        return await render_cycles(request, await authenticated(request))

    @router.post("/cycles")
    async def create_cycle(request: Request):
        return await action(
            request,
            lambda actor, form: commands.create_cycle(actor, form["title"]),
            lambda result: "/cycles/" + result["id"],
            lambda context, error, status: render_cycles(
                request, context, error=error, status=status
            ),
        )

    @router.get("/cycles/{cycle_id}")
    async def cycle(cycle_id: str, request: Request):
        return await render_cycle(request, await authenticated(request), cycle_id)

    async def cycle_action(request, cycle_id, operation):
        return await action(
            request,
            operation,
            lambda _: "/cycles/" + cycle_id,
            lambda context, error, status: render_cycle(
                request, context, cycle_id, error=error, status=status
            ),
        )

    @router.post("/cycles/{cycle_id}/edit")
    async def edit(cycle_id: str, request: Request):
        return await cycle_action(
            request,
            cycle_id,
            lambda actor, form: commands.edit_draft_cycle(
                actor, cycle_id, form["title"], integer(form, "expected_revision")
            ),
        )

    @router.post("/cycles/{cycle_id}/requesters")
    async def admit(cycle_id: str, request: Request):
        return await cycle_action(
            request,
            cycle_id,
            lambda actor, form: commands.admit_requester(
                actor, cycle_id, form["requester_id"], integer(form, "expected_revision")
            ),
        )

    @router.post("/cycles/{cycle_id}/activate")
    async def activate(cycle_id: str, request: Request):
        return await cycle_action(
            request,
            cycle_id,
            lambda actor, form: commands.activate_cycle(
                actor, cycle_id, integer(form, "expected_revision")
            ),
        )

    @router.post("/cycles/{cycle_id}/items")
    async def create_item(cycle_id: str, request: Request):
        return await action(
            request,
            lambda actor, form: commands.create_item(
                actor, cycle_id, integer(form, "expected_revision"), form["command_id"]
            ),
            lambda result: "/items/" + result["id"],
            lambda context, error, status: render_cycle(
                request, context, cycle_id, error=error, status=status
            ),
        )

    @router.get("/items/{item_id}")
    async def item(item_id: str, request: Request):
        return await render_item(request, await authenticated(request), item_id)

    async def item_action(request, item_id, operation):
        return await action(
            request,
            operation,
            lambda _: "/items/" + item_id,
            lambda context, error, status: render_item(
                request, context, item_id, error=error, status=status
            ),
        )

    @router.post("/items/{item_id}/chain")
    async def chain(item_id: str, request: Request):
        async def configure(actor, form):
            gap = False
            for level in range(1, 6):
                if not form.get(f"reviewer_{level}"):
                    gap = True
                elif gap:
                    raise Denied("approval_levels_must_be_contiguous", 422)
            steps = [
                {
                    "reviewer_id": form[f"reviewer_{level}"],
                    "due_after_s": integer(form, f"duration_{level}"),
                }
                for level in range(1, 6)
                if form.get(f"reviewer_{level}")
            ]
            return await commands.configure_chain(
                actor, item_id, steps, integer(form, "expected_revision")
            )

        return await item_action(request, item_id, configure)

    @router.post("/items/{item_id}/versions")
    async def submit(item_id: str, request: Request):
        return await item_action(
            request,
            item_id,
            lambda actor, form: commands.submit_version(
                actor, item_id, form["text"], integer(form, "expected_revision"), form["command_id"]
            ),
        )

    @router.post("/items/{item_id}/decisions")
    async def decision(item_id: str, request: Request):
        return await item_action(
            request,
            item_id,
            lambda actor, form: commands.decide(
                actor,
                item_id,
                version_id=form["version_id"],
                level=integer(form, "level"),
                plan_hash=form["plan_hash"],
                decision=form["decision"],
                comment=form.get("comment", ""),
                expected_revision=integer(form, "expected_revision"),
                expected_progress_revision=integer(form, "expected_progress_revision"),
                command_id=form["command_id"],
            ),
        )

    @router.get("/pending")
    async def pending(request: Request):
        context = await authenticated(request)
        result = await commands.pending_reviews(
            context.actor, offset=int(request.query_params.get("offset", "0"))
        )
        return await page(
            request, "pending.html", context, title="My pending reviews", result=result
        )

    @router.get("/admin")
    async def admin(request: Request):
        return await render_admin(request, await authenticated(request))

    @router.post("/admin/accounts")
    async def account(request: Request):
        return await action(
            request,
            lambda actor, form: auth.create_account(
                actor, form["login"], form["password"], form["role"]
            ),
            lambda _: "/admin",
            lambda context, error, status: render_admin(
                request, context, error=error, status=status
            ),
        )

    @router.post("/admin/accounts/{membership_id}/enabled")
    async def account_enabled(membership_id: str, request: Request):
        async def change(actor, form):
            if form.get("enabled") not in {"true", "false"}:
                raise Denied("invalid_form", 422)
            return await auth.set_account_enabled(actor, membership_id, form["enabled"] == "true")

        return await action(
            request,
            change,
            lambda _: "/admin",
            lambda context, error, status: render_admin(
                request, context, error=error, status=status
            ),
        )

    @router.get("/audit")
    async def audit(request: Request):
        from .audit import list_events

        context = await authenticated(request)
        result = await list_events(
            auth.database, context.actor, cursor=request.query_params.get("cursor")
        )
        return await page(request, "audit.html", context, title="Activity history", result=result)

    app.include_router(router)

"""Local password verification and database-backed opaque sessions.

No API keys, caller-provided role claims, browser token storage or public signup.
"""

import asyncio
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import UUID, uuid4

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError

from .authorization import Actor, Denied, authorize
from .domain import Conflict
from .identity_models import AuthSession, LoginThrottle, Membership, Tenant, User

_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)
_dummy_hash = _hasher.hash(secrets.token_urlsafe(32))


class AuthenticationFailed(Denied):
    def __init__(self):
        super().__init__("authentication_required", 401)


def token_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def csrf_token(token: str) -> str:
    return token_hash("csrf:" + token)


def normalized(value: str, *, slug=False) -> str:
    value = value.strip().casefold()
    pattern = r"[a-z0-9][a-z0-9-]{0,79}" if slug else r"[a-z0-9][a-z0-9_.@+-]{2,99}"
    if not re.fullmatch(pattern, value):
        raise Denied("invalid_identity", 422)
    return value


async def lock_actor(session, actor: Actor, *, memberships=()) -> Actor:
    """Lock all relevant principals in stable order, before any cycle/item/run lock."""
    wanted = {actor.membership_id, *memberships}
    users = select(Membership.user_id).where(
        Membership.id.in_(wanted), Membership.tenant_id == actor.tenant_id
    )
    await session.execute(
        select(User)
        .where(User.id.in_(users))
        .order_by(User.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    rows = (
        await session.execute(
            select(Membership, User)
            .join(User)
            .where(Membership.id.in_(wanted), Membership.tenant_id == actor.tenant_id)
            .order_by(Membership.id)
            .with_for_update(of=Membership)
            .execution_options(populate_existing=True)
        )
    ).all()
    row = next(
        (
            row
            for row in rows
            if row[0].id == actor.membership_id and row[0].user_id == actor.user_id
        ),
        None,
    )
    if row is None or not row[0].enabled or not row[1].enabled:
        raise AuthenticationFailed()
    current = Actor(row[0].id, row[1].id, row[0].tenant_id, row[0].role)
    if current != actor:
        raise AuthenticationFailed()
    return current


@dataclass(frozen=True)
class SessionContext:
    actor: Actor | None
    token: str = field(repr=False)
    csrf: str = field(repr=False)


class AuthService:
    def __init__(self, database, settings):
        self.database, self.settings = database, settings
        self.cookie_name, self.secure = settings.browser_security()
        self._password_slots = asyncio.Semaphore(2)

    async def password_hash(self, password):
        if not 12 <= len(password) <= 256 or "\0" in password:
            raise Denied("password_must_be_12_to_256_characters", 422)
        async with self._password_slots:
            return await asyncio.to_thread(_hasher.hash, password)

    async def _verify(self, hashed, password):
        def verify():
            try:
                return _hasher.verify(hashed, password)
            except (VerificationError, InvalidHashError):
                return False

        async with self._password_slots:
            return await asyncio.to_thread(verify)

    async def bootstrap(self, tenant_slug, login, password):
        """Operator-only Python/CLI entry point. Never exposed as an HTTP command."""
        slug, login = normalized(tenant_slug, slug=True), normalized(login)
        hashed = await self.password_hash(password)
        try:
            async with self.database.sessions.begin() as session:
                tenant = Tenant(id=str(uuid4()), slug=slug)
                user = User(id=uuid4(), login=login, password_hash=hashed, enabled=True)
                session.add_all([tenant, user])
                await session.flush()
                member = Membership(
                    id=uuid4(), tenant_id=tenant.id, user_id=user.id, role="ADMIN", enabled=True
                )
                session.add(member)
                await session.flush()
                actor = Actor(member.id, user.id, tenant.id, member.role)
                from .audit import append_event

                await append_event(
                    session,
                    actor,
                    "tenant",
                    UUID(tenant.id),
                    "tenant_bootstrapped",
                    0,
                    principal_type="OPERATOR",
                )
                return actor
        except IntegrityError:
            raise Conflict("Bootstrap identity already exists; nothing was changed") from None

    async def create_account(self, actor, login, password, role):
        authorize(actor, "manage_accounts", actor.tenant_id)
        if role not in {"REQUESTER", "REVIEWER"}:
            raise Denied("operator_required")
        login, hashed = normalized(login), await self.password_hash(password)
        try:
            async with self.database.sessions.begin() as session:
                actor = await lock_actor(session, actor)
                user = User(id=uuid4(), login=login, password_hash=hashed, enabled=True)
                session.add(user)
                await session.flush()
                member = Membership(
                    id=uuid4(), tenant_id=actor.tenant_id, user_id=user.id, role=role, enabled=True
                )
                session.add(member)
                await session.flush()
                from .audit import append_event

                await append_event(session, actor, "account", member.id, "account_created", 0)
                return Actor(member.id, user.id, member.tenant_id, member.role)
        except IntegrityError:
            # Do not confirm whether a login exists in another tenant.
            raise Conflict("Account could not be created") from None

    async def _new_session(self, session, actor=None):
        token = secrets.token_urlsafe(32)
        now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
        session.add(
            AuthSession(
                token_hash=token_hash(token),
                csrf_hash=token_hash(csrf_token(token)),
                user_id=actor.user_id if actor else None,
                membership_id=actor.membership_id if actor else None,
                tenant_id=actor.tenant_id if actor else None,
                created_at=now,
                last_seen_at=now,
                idle_expires_at=now + timedelta(minutes=30 if actor else 10),
                absolute_expires_at=now + timedelta(hours=12)
                if actor
                else now + timedelta(minutes=10),
                revoked_at=None,
            )
        )
        return SessionContext(actor, token, csrf_token(token))

    async def set_account_enabled(self, actor, membership_id, enabled):
        authorize(actor, "manage_accounts", actor.tenant_id)
        if type(enabled) is not bool:
            raise Denied("invalid_account_state", 422)
        membership_id = UUID(str(membership_id))
        async with self.database.sessions.begin() as session:
            await lock_actor(session, actor, memberships=[membership_id])
            member = await session.get(Membership, membership_id)
            if member is None or member.tenant_id != actor.tenant_id:
                raise Denied("not_found", 404)
            if member.role == "ADMIN":
                raise Denied("operator_required")
            if member.enabled == enabled:
                return {"membership_id": str(member.id), "enabled": enabled}
            member.enabled = enabled
            if not enabled:
                await session.execute(
                    update(AuthSession)
                    .where(
                        AuthSession.membership_id == member.id,
                        AuthSession.tenant_id == actor.tenant_id,
                        AuthSession.revoked_at.is_(None),
                    )
                    .values(revoked_at=func.clock_timestamp())
                )
            from .audit import append_event

            await append_event(
                session,
                actor,
                "account",
                member.id,
                "account_enabled" if enabled else "account_disabled",
                0,
            )
            return {"membership_id": str(member.id), "enabled": enabled}

    async def pre_session(self):
        async with self.database.sessions.begin() as session:
            return await self._new_session(session)

    async def _load(self, session, token, *, anonymous=False, lock=True):
        if not token or not 40 <= len(token) <= 128:
            raise AuthenticationFailed()
        query = select(AuthSession).where(AuthSession.token_hash == token_hash(token))
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        row = (await session.execute(query)).scalar_one_or_none()
        now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
        if (
            row is None
            or row.revoked_at
            or row.idle_expires_at <= now
            or row.absolute_expires_at <= now
            or (not anonymous and row.user_id is None)
        ):
            raise AuthenticationFailed()
        return row, now

    @staticmethod
    def check_csrf(row, csrf):
        if not csrf or not hmac.compare_digest(row.csrf_hash, token_hash(csrf)):
            raise Denied("csrf_failed")

    async def _throttle(self, login, source):
        async with self.database.sessions.begin() as session:
            now = (await session.execute(select(func.clock_timestamp()))).scalar_one()
            for prefix, value, limit in (("account", login, 10), ("source", source, 50)):
                key = token_hash(prefix + ":" + value)
                await session.execute(
                    insert(LoginThrottle)
                    .values(key=key, attempts=0, resets_at=now + timedelta(minutes=15))
                    .on_conflict_do_nothing()
                )
                row = (
                    await session.execute(
                        select(LoginThrottle).where(LoginThrottle.key == key).with_for_update()
                    )
                ).scalar_one()
                if row.resets_at <= now:
                    row.attempts, row.resets_at = 0, now + timedelta(minutes=15)
                if row.attempts >= limit:
                    raise Denied("login_temporarily_unavailable", 429)
                row.attempts += 1

    async def login(self, token, csrf, tenant_slug, login, password, source):
        async with self.database.sessions.begin() as session:
            row, _ = await self._load(session, token, anonymous=True)
            self.check_csrf(row, csrf)
            if row.user_id is not None:
                raise AuthenticationFailed()
        # Throttle is independently committed even when credential verification fails.
        login = login.strip().casefold()
        await self._throttle(login, source)
        async with self.database.sessions() as session:
            result = (
                await session.execute(
                    select(User, Membership)
                    .join(Membership)
                    .join(Tenant)
                    .where(
                        User.login == login,
                        Tenant.slug == tenant_slug.strip().casefold(),
                        User.enabled.is_(True),
                        Membership.enabled.is_(True),
                    )
                )
            ).one_or_none()
        valid = await self._verify(result[0].password_hash if result else _dummy_hash, password)
        if not result or not valid:
            raise AuthenticationFailed()
        actor = Actor(result[1].id, result[0].id, result[1].tenant_id, result[1].role)
        async with self.database.sessions.begin() as session:
            actor = await lock_actor(session, actor)
            row, now = await self._load(session, token, anonymous=True)
            self.check_csrf(row, csrf)
            if row.user_id is not None:
                raise AuthenticationFailed()
            row.revoked_at = now
            await session.execute(
                update(LoginThrottle)
                .where(LoginThrottle.key == token_hash("account:" + login))
                .values(attempts=0)
            )
            context = await self._new_session(session, actor)
            from .audit import append_event

            # Audit uses an independent event entity ID, never the token or its hash.
            await append_event(session, actor, "session", uuid4(), "session_created", 0)
            return context

    async def resolve(self, token, csrf=None):
        async with self.database.sessions.begin() as session:
            row, _ = await self._load(session, token, lock=False)
            if csrf is not None:
                self.check_csrf(row, csrf)
            membership = await session.get(Membership, row.membership_id)
            if membership is None or membership.user_id != row.user_id:
                raise AuthenticationFailed()
            actor = await lock_actor(
                session, Actor(membership.id, row.user_id, membership.tenant_id, membership.role)
            )
            row, now = await self._load(session, token)
            if csrf is not None:
                self.check_csrf(row, csrf)
            row.last_seen_at = now
            row.idle_expires_at = min(now + timedelta(minutes=30), row.absolute_expires_at)
            return SessionContext(actor, token, csrf_token(token))

    async def logout(self, token, csrf):
        context = await self.resolve(token, csrf)
        async with self.database.sessions.begin() as session:
            await lock_actor(session, context.actor)
            row, now = await self._load(session, token)
            self.check_csrf(row, csrf)
            row.revoked_at = now
            from .audit import append_event

            await append_event(session, context.actor, "session", uuid4(), "session_revoked", 0)

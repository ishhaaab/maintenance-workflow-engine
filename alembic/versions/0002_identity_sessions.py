"""Named tenant identities and durable cookie sessions."""

import json
import re
from pathlib import Path

import sqlalchemy as sa

from alembic import context, op

revision = "0002_identity_sessions"
down_revision = "0001_relational_execution"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "tenants",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("slug", sa.String(80), nullable=False, unique=True),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("login", sa.String(100), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(512), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "tenant_memberships",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(128), sa.ForeignKey("tenants.id"), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("tenant_id", "id", name="uq_membership_tenant_id"),
        sa.UniqueConstraint("tenant_id", "user_id", name="uq_membership_user"),
        sa.CheckConstraint("role IN ('ADMIN', 'REQUESTER', 'REVIEWER')", name="ck_membership_role"),
    )
    op.create_table(
        "auth_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("csrf_hash", sa.String(64), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id")),
        sa.Column("membership_id", sa.Uuid()),
        sa.Column("tenant_id", sa.String(128)),
        *[
            sa.Column(name, sa.DateTime(timezone=True), nullable=False)
            for name in ("created_at", "last_seen_at", "idle_expires_at", "absolute_expires_at")
        ],
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["tenant_id", "membership_id"],
            ["tenant_memberships.tenant_id", "tenant_memberships.id"],
            name="fk_session_membership",
        ),
        sa.CheckConstraint(
            "(user_id IS NULL AND membership_id IS NULL AND tenant_id IS NULL) OR "
            "(user_id IS NOT NULL AND membership_id IS NOT NULL AND tenant_id IS NOT NULL)",
            name="ck_session_identity",
        ),
    )
    op.create_index("ix_session_user_revoked", "auth_sessions", ["user_id", "revoked_at"])
    op.create_index("ix_session_expiry", "auth_sessions", ["absolute_expires_at"])
    op.create_table(
        "login_throttles",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("resets_at", sa.DateTime(timezone=True), nullable=False),
    )
    # Never guess tenant identity from legacy keys or turn retained runs into users.
    _enroll_explicit_retained_tenants(op.get_bind())
    op.create_foreign_key("fk_execution_tenant", "execution_runs", "tenants", ["tenant_id"], ["id"])
    role = (
        op.get_bind()
        .execute(sa.text("SELECT current_setting('workflow.runtime_role')"))
        .scalar_one()
    )
    role = op.get_bind().dialect.identifier_preparer.quote(role)
    for table in ("tenants", "users", "tenant_memberships", "auth_sessions", "login_throttles"):
        op.execute(f"REVOKE ALL ON {table} FROM PUBLIC")
        op.execute(f"GRANT SELECT, INSERT, UPDATE ON {table} TO {role}")


def downgrade():
    raise RuntimeError("Identity history must not be destructively downgraded")


def _enroll_explicit_retained_tenants(connection):
    existing = set(
        connection.execute(sa.text("SELECT DISTINCT tenant_id FROM execution_runs")).scalars()
    )
    if not existing:
        return
    filename = context.get_x_argument(as_dictionary=True).get("tenant_registry")
    try:
        if not filename:
            raise ValueError
        with Path(filename).open("rb") as file:
            raw = file.read(65537)
        if len(raw) > 65536:
            raise ValueError
        records = json.loads(raw)
        if not isinstance(records, list) or not 1 <= len(records) <= 1000:
            raise ValueError
        identities, slugs = set(), set()
        for record in records:
            if not isinstance(record, dict) or set(record) != {"id", "slug"}:
                raise ValueError
            identity, slug = record["id"], record["slug"]
            if (
                not isinstance(identity, str)
                or not isinstance(slug, str)
                or identity not in existing
                or identity in identities
                or slug in slugs
                or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", slug)
            ):
                raise ValueError
            identities.add(identity)
            slugs.add(slug)
        if identities != existing:
            raise ValueError
    except (OSError, ValueError, TypeError, KeyError):
        raise RuntimeError(
            "Retained PostgreSQL tenants require an explicit complete tenant_registry file"
        ) from None
    for record in records:
        connection.execute(sa.text("INSERT INTO tenants (id, slug) VALUES (:id, :slug)"), record)

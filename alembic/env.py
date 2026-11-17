"""Explicit migration commands only. Application startup never imports this module."""

import os

from sqlalchemy import create_engine, pool
from sqlalchemy.engine import make_url

from alembic import context
from workflow_engine import (
    demo_models,  # noqa: F401
    identity_models,  # noqa: F401
    notification_models,  # noqa: F401
    review_models,  # noqa: F401
    version_models,  # noqa: F401
)
from workflow_engine.db_models import Base


def run_migrations():
    if context.is_offline_mode():
        raise RuntimeError("Offline migrations are not supported")
    value = os.environ.get("WORKFLOW_MIGRATION_URL")
    if not value:
        raise RuntimeError("WORKFLOW_MIGRATION_URL is required for explicit migrations")
    try:
        url = make_url(value)
        if url.get_backend_name() != "postgresql":
            raise ValueError
        url = url.set(drivername="postgresql+psycopg")
        engine = create_engine(
            url,
            poolclass=pool.NullPool,
            echo=False,
            hide_parameters=True,
            connect_args={"options": "-c timezone=UTC -c search_path=public", "connect_timeout": 5},
        )
        try:
            with engine.connect() as connection:
                context.configure(
                    connection=connection, target_metadata=Base.metadata, compare_type=True
                )
                with context.begin_transaction():
                    context.run_migrations()
        finally:
            engine.dispose()
    except Exception:
        raise RuntimeError("Explicit PostgreSQL migration failed") from None


run_migrations()

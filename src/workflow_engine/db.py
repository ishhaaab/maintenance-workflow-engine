"""Short-session PostgreSQL lifecycle. Schema changes belong only to explicit Alembic commands."""

from dataclasses import dataclass, field

from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from ._storage_codec import _storage_dumps, _storage_loads

SCHEMA_REVISION = "0007_demo_seed_records"


class SchemaNotCurrent(RuntimeError):
    """The database has not been explicitly migrated to this application's revision."""


@dataclass
class Database:
    engine: AsyncEngine = field(repr=False)
    sessions: async_sessionmaker[AsyncSession] = field(repr=False)

    async def check_schema(self) -> None:
        try:
            async with self.engine.connect() as connection:
                result = await connection.execute(
                    text("SELECT version_num FROM public.alembic_version")
                )
                revisions = result.scalars().all()
        except SQLAlchemyError:
            # SQL/driver diagnostics can contain URLs, parameters or server-provided text.
            raise SchemaNotCurrent(
                "PostgreSQL schema check failed; explicit migration required"
            ) from None
        if revisions != [SCHEMA_REVISION]:
            raise SchemaNotCurrent("PostgreSQL schema is not at the required revision")

    async def close(self) -> None:
        await self.engine.dispose()


def create_database(url: SecretStr) -> Database:
    try:
        parsed = make_url(url.get_secret_value())
        if parsed.get_backend_name() != "postgresql":
            raise ValueError
        engine = create_async_engine(
            parsed.set(drivername="postgresql+psycopg"),
            echo=False,
            hide_parameters=True,
            pool_pre_ping=True,
            json_serializer=_storage_dumps,
            json_deserializer=_storage_loads,
            connect_args={"options": "-c timezone=UTC -c search_path=public", "connect_timeout": 5},
        )
    except Exception:
        raise ValueError("A valid secret PostgreSQL URL is required") from None
    return Database(engine, async_sessionmaker(engine, expire_on_commit=False))

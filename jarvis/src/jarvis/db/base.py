"""Database engine, session management, and shared column conventions."""

from __future__ import annotations

import enum
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any, Generic, TypeVar

from sqlalchemy import DateTime, MetaData, String, TypeDecorator, event
from sqlalchemy.engine import Dialect
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, mapped_column
from sqlalchemy.pool import StaticPool

# Explicit naming convention so Alembic autogenerate produces stable, named
# constraints. Without this, SQLite constraint names are anonymous and later
# migrations cannot drop or alter them.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    """Prefixed identifier — ``task_3f2a…``.

    The prefix makes IDs self-describing in logs and API payloads, which is
    worth the handful of extra bytes when debugging a pipeline that threads
    four different ID types through one request.
    """
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


E = TypeVar("E", bound=enum.Enum)


class EnumType(TypeDecorator, Generic[E]):  # type: ignore[type-arg]
    """Store a Python enum as its ``value`` string, load it back as the enum.

    A plain ``String`` column does not round-trip: values written as enums come
    back as ``str`` after a reload, so ``.value`` raises ``AttributeError`` at
    some arbitrary later point. Storing the readable value (rather than
    SQLAlchemy's native ``Enum``, which persists the *name* and creates a DB
    constraint that fights migrations) keeps the database greppable while
    guaranteeing the Python-side type.

    Unknown values coming back from the database are returned as-is rather than
    raising: a row written by a newer build must not make an older one
    unbootable.
    """

    impl = String
    cache_ok = True

    def __init__(self, enum_class: type[E], length: int = 48) -> None:
        self.enum_class = enum_class
        super().__init__(length=length)

    def process_bind_param(self, value: Any, dialect: Dialect) -> str | None:
        if value is None:
            return None
        if isinstance(value, self.enum_class):
            return str(value.value)
        # Accept the raw value too, so callers passing a string still work.
        return str(self.enum_class(value).value)

    def process_result_value(self, value: Any, dialect: Dialect) -> Any:
        if value is None:
            return None
        try:
            return self.enum_class(value)
        except ValueError:
            return value


def ts_column(**kwargs: object):
    """Timezone-aware timestamp column.

    SQLite does not persist tzinfo, so values come back naive; the session
    listener below re-attaches UTC on load.
    """
    return mapped_column(DateTime(timezone=True), **kwargs)


class Database:
    """Owns the engine and session factory.

    Instantiated once per process (or per test), rather than living in module
    globals, so tests can spin up isolated in-memory databases concurrently.
    """

    def __init__(self, url: str, *, echo: bool = False) -> None:
        self.url = url
        connect_args: dict[str, object] = {}
        kwargs: dict[str, object] = {"echo": echo, "future": True}

        if url.startswith("sqlite"):
            connect_args["check_same_thread"] = False
            if ":memory:" in url:
                # One shared connection, or each session gets its own blank DB.
                kwargs["poolclass"] = StaticPool

        self.engine: AsyncEngine = create_async_engine(
            url, connect_args=connect_args, **kwargs
        )
        self._configure_sqlite()
        self.session_factory = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

    def _configure_sqlite(self) -> None:
        if not self.url.startswith("sqlite"):
            return

        @event.listens_for(self.engine.sync_engine, "connect")
        def _set_pragmas(dbapi_conn, _record):  # type: ignore[no-untyped-def]
            cursor = dbapi_conn.cursor()
            # WAL: concurrent readers alongside the writer, which matters once
            # background jobs and the request path share the file.
            cursor.execute("PRAGMA journal_mode=WAL")
            # SQLite does not enforce foreign keys unless asked to.
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    async def create_all(self) -> None:
        """Schema creation for tests. Production uses Alembic migrations."""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self.engine.dispose()

    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            yield session

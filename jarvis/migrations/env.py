"""Alembic environment.

Reads the database URL from :mod:`jarvis.config` rather than ``alembic.ini``,
so migrations always target the same database the application does and no
connection string is duplicated into a tracked file.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy import pool

from jarvis.config import get_settings
from jarvis.db.base import Base
from jarvis.db import models  # noqa: F401  — imported for metadata registration

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().resolved_database_url)
target_metadata = Base.metadata


def render_item(type_, obj, autogen_context):  # type: ignore[no-untyped-def]
    """Render application-defined column types as their plain SQL equivalent.

    Autogenerate would otherwise emit ``jarvis.db.base.EnumType(...)`` into the
    migration, coupling a historical artifact to code that will be refactored
    or deleted. A migration has to keep running years later, so it must depend
    only on SQLAlchemy. ``EnumType`` is a ``VARCHAR`` on disk, so rendering it
    as ``sa.String`` produces byte-identical DDL.
    """
    from jarvis.db.base import EnumType

    if type_ == "type" and isinstance(obj, EnumType):
        autogen_context.imports.add("import sqlalchemy as sa")
        return f"sa.String(length={obj.impl.length})"
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # SQLite cannot ALTER most things in place; batch mode rewrites the
        # table instead, which is what makes future migrations possible at all.
        render_as_batch=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:  # type: ignore[no-untyped-def]
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        compare_type=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_async_migrations())

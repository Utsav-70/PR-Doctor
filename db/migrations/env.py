"""Alembic environment, wired for the async engine.

The async setup is deliberate and worth doing now: retrofitting it after a dozen
migrations means rewriting every one that used a sync connection.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

from db.models import Base
from settings import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Single source of truth for the URL: settings, not alembic.ini.
config.set_main_option("sqlalchemy.url", get_settings().DATABASE_URL)

target_metadata = Base.metadata


def _run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,  # catch VARCHAR(64) -> VARCHAR(128)
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting — `alembic upgrade head --sql`."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


async def _run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=NullPool,  # migrations are one-shot; do not hold a pool
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

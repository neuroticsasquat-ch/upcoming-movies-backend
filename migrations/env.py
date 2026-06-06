import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from upmovies.config import get_settings
from upmovies.db import Base

import upmovies.app.models      # noqa: F401  -- register models with Base.metadata
import upmovies.catalog.models  # noqa: F401
import upmovies.news.models     # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = Base.metadata


def include_name(name, type_, parent_names):
    if type_ == "schema":
        return name in ("app", "catalog", "news")
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        include_schemas=True,
        include_name=include_name,
        version_table_schema="app",
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _ensure_schemas(connection: Connection) -> None:
    # The Alembic version table lives in the `app` schema, so the schemas (and the
    # extensions our models rely on) must exist before Alembic touches the DB. Doing
    # this here keeps prod's `alembic upgrade head` self-contained against a bare
    # database — no external schema bootstrap step required.
    for schema in ("app", "catalog", "news"):
        connection.exec_driver_sql(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    connection.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS citext")
    connection.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS pgcrypto")


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_schemas=True,
        include_name=include_name,
        version_table_schema="app",
    )
    # Create the schemas inside Alembic's transaction so Alembic owns (and commits)
    # it; the `app` schema must exist before Alembic creates its version table.
    with context.begin_transaction():
        _ensure_schemas(connection)
        context.run_migrations()


async def run_migrations_online() -> None:
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
    asyncio.run(run_migrations_online())

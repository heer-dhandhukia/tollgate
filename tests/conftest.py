"""Shared fixtures. Tests create (and drop) their own databases on the Postgres server at
TEST_PG_ADMIN_URL. Without a reachable server, DB tests are skipped locally and fail in CI."""

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from hypothesis import settings
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parents[1]
ADMIN_URL = os.environ.get(
    "TEST_PG_ADMIN_URL", "postgresql+asyncpg://tollgate:tollgate@localhost:5432/postgres"
)

settings.register_profile("dev", max_examples=25, deadline=None)
settings.register_profile("ci", max_examples=100, deadline=None)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))


def database_url(name: str) -> str:
    return make_url(ADMIN_URL).set(database=name).render_as_string(hide_password=False)


async def _recreate(name: str) -> None:
    engine = create_async_engine(ADMIN_URL, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await engine.dispose()


def recreate_database(name: str) -> str:
    try:
        asyncio.run(_recreate(name))
    except Exception as exc:
        if os.environ.get("CI"):
            raise
        pytest.skip(f"Postgres not reachable at {ADMIN_URL}: {exc}")
    return database_url(name)


@pytest.fixture(scope="session")
def tollgate_db_url() -> str:
    url = recreate_database("tollgate_test")
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")
    return url


@pytest.fixture
async def engine(tollgate_db_url: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(tollgate_db_url, poolclass=NullPool)
    yield eng
    await eng.dispose()


@pytest.fixture
def maker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)

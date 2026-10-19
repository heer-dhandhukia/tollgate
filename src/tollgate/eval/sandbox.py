"""Isolated databases per scenario attempt, cloned from migrated templates.

Cloning (`CREATE DATABASE … TEMPLATE …`) gives every scenario a pristine ledger and provider in
~100 ms, lets scenarios run concurrently without interfering, and makes a crashed attempt
trivially retryable (drop and clone again). In-place cleanup is impossible anyway: the ledger is
append-only by design."""

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from mockpay.models import Base as MockpayBase
from tollgate.config import REPO_ROOT


class Sandboxes:
    def __init__(self, admin_url: str, prefix: str = "tg_eval", keep: bool = False) -> None:
        self.admin_url = admin_url
        self.prefix = prefix
        self.keep = keep or bool(os.environ.get("KEEP_SANDBOXES"))
        self.tollgate_template = f"{prefix}_tpl_ledger"
        self.mockpay_template = f"{prefix}_tpl_mockpay"
        self._clone_lock = asyncio.Lock()

    def url(self, database: str) -> str:
        return make_url(self.admin_url).set(database=database).render_as_string(hide_password=False)

    async def _admin(self, *statements: str) -> None:
        engine = create_async_engine(
            self.admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
        )
        try:
            async with engine.connect() as conn:
                for statement in statements:
                    await conn.execute(text(statement))
        finally:
            await engine.dispose()

    async def _recreate(self, name: str, template: str | None = None) -> None:
        create = f'CREATE DATABASE "{name}"' + (f' TEMPLATE "{template}"' if template else "")
        await self._admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)', create)

    async def prepare(self) -> None:
        """(Re)build both templates from migrations / models."""
        await self._recreate(self.tollgate_template)
        cfg = Config(str(REPO_ROOT / "alembic.ini"))
        cfg.set_main_option("sqlalchemy.url", self.url(self.tollgate_template))
        cfg.attributes["configure_logger"] = False
        await asyncio.to_thread(command.upgrade, cfg, "head")

        await self._recreate(self.mockpay_template)
        engine = create_async_engine(self.url(self.mockpay_template), poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(MockpayBase.metadata.create_all)
        finally:
            await engine.dispose()

    @asynccontextmanager
    async def scenario(self, name: str) -> AsyncIterator[tuple[str, str]]:
        ledger_db, provider_db = f"{name}_l", f"{name}_p"
        async with self._clone_lock:  # a template can't be cloned while another clone reads it
            await self._recreate(ledger_db, self.tollgate_template)
            await self._recreate(provider_db, self.mockpay_template)
        try:
            yield self.url(ledger_db), self.url(provider_db)
        finally:
            if not self.keep:
                await self._admin(
                    f'DROP DATABASE IF EXISTS "{ledger_db}" WITH (FORCE)',
                    f'DROP DATABASE IF EXISTS "{provider_db}" WITH (FORCE)',
                )

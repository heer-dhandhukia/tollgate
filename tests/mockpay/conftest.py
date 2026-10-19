from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from mockpay.app import Provider, create_app
from mockpay.config import MockpaySettings
from tests.conftest import recreate_database

WEBHOOK_URL = "http://tollgate.test/webhooks/mockpay"
SECRET = "test-secret"


@dataclass
class Recorder:
    """Stands in for Tollgate's webhook endpoint."""

    status: int = 200
    requests: list[httpx.Request] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status)

    def bodies(self) -> list[dict[str, Any]]:
        import json

        return [json.loads(r.content) for r in self.requests]


@dataclass
class Mockpay:
    client: httpx.AsyncClient
    provider: Provider
    recorder: Recorder

    async def payment(self, amount: int = 100_000) -> str:
        r = await self.client.post("/v1/payments", json={"amount_paise": amount})
        assert r.status_code == 200, r.text
        return str(r.json()["id"])

    async def refunds_for(self, payment_id: str) -> list[dict[str, Any]]:
        r = await self.client.get("/v1/refunds", params={"payment_id": payment_id})
        return list(r.json()["items"])

    async def settle_and_deliver(self) -> None:
        await self.provider.settle_due()
        await self.provider.dispatcher.drain()


@pytest.fixture(scope="session")
def mockpay_db_url() -> str:
    return recreate_database("mockpay_test")


@pytest.fixture
async def mockpay(mockpay_db_url: str) -> AsyncIterator[Mockpay]:
    settings = MockpaySettings(
        database_url=mockpay_db_url,
        webhook_url=WEBHOOK_URL,
        webhook_secret=SECRET,
        settle_delay_ms=0,
        settle_poll_ms=0,
    )
    recorder = Recorder()
    app = create_app(settings, httpx.MockTransport(recorder))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://mockpay.test") as c:
            yield Mockpay(c, app.state.provider, recorder)

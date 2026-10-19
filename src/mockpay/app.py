"""mockpay HTTP API (Razorpay/Stripe-like).

    POST /v1/payments                 create a captured payment (used to seed scenarios)
    GET  /v1/payments/{id}
    POST /v1/refunds                  create a refund; honours Idempotency-Key
    GET  /v1/refunds/{id}
    GET  /v1/refunds?payment_id=&receipt=
    /_admin/...                       fault scripting, forced settlement, webhook inspection

Idempotency-Key semantics (IETF draft-ietf-httpapi-idempotency-key-header, as Stripe does it):
same key + same body -> the stored response is replayed (`Idempotent-Replayed: true`);
same key + different body -> 422; same key while the first request is in flight -> 409.
Requests without a key are not deduplicated: every call creates a new refund.
"""

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Annotated, Any

import httpx
from fastapi import FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from mockpay.config import MockpaySettings
from mockpay.faults import FaultInjector, FaultSpecError
from mockpay.models import Base
from mockpay.store import Store, error_body, payment_json, refund_json, request_hash
from mockpay.webhooks import Dispatcher


class PaymentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str | None = Field(default=None, max_length=32)
    amount_paise: Annotated[StrictInt, Field(gt=0)]
    captured_paise: Annotated[StrictInt, Field(ge=0)] | None = None


class RefundCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payment_id: str
    amount_paise: Annotated[StrictInt, Field(gt=0)]
    receipt: str | None = Field(default=None, max_length=128)
    notes: dict[str, str] = Field(default_factory=dict)


class FaultScript(BaseModel):
    faults: list[str]


class Provider:
    def __init__(self, store: Store, dispatcher: Dispatcher, faults: FaultInjector) -> None:
        self.store = store
        self.dispatcher = dispatcher
        self.faults = faults

    async def settle_due(self) -> int:
        events = await self.store.settle_due()
        for event in events:
            self.dispatcher.publish(event)
        return len(events)


def _error(status: int, code: str, description: str) -> JSONResponse:
    return JSONResponse(error_body(code, description), status_code=status)


async def _sweep(provider: Provider, every_ms: int) -> None:
    while True:
        await asyncio.sleep(every_ms / 1000)
        await provider.settle_due()


def create_app(
    settings: MockpaySettings | None = None,
    webhook_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    cfg = settings or MockpaySettings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_async_engine(cfg.database_url, pool_pre_ping=True)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        dispatcher = Dispatcher(cfg, webhook_transport)
        await dispatcher.start()
        provider = Provider(
            Store(async_sessionmaker(engine, expire_on_commit=False), cfg),
            dispatcher,
            FaultInjector(cfg),
        )
        app.state.provider = provider
        sweeper = (
            asyncio.create_task(_sweep(provider, cfg.settle_poll_ms))
            if cfg.settle_poll_ms > 0
            else None
        )
        try:
            yield
        finally:
            if sweeper is not None:
                sweeper.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sweeper
            await dispatcher.stop()
            await engine.dispose()

    app = FastAPI(title="mockpay", lifespan=lifespan)

    def provider_of(request: Request) -> Provider:
        return request.app.state.provider  # type: ignore[no-any-return]

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/payments")
    async def create_payment(body: PaymentCreate, request: Request) -> JSONResponse:
        if body.captured_paise is not None and body.captured_paise > body.amount_paise:
            return _error(400, "BAD_REQUEST_ERROR", "captured_paise exceeds amount_paise")
        payment = await provider_of(request).store.create_payment(
            body.amount_paise, body.captured_paise, body.id
        )
        return JSONResponse(payment_json(payment))

    @app.get("/v1/payments/{payment_id}")
    async def get_payment(payment_id: str, request: Request) -> JSONResponse:
        payment = await provider_of(request).store.get_payment(payment_id)
        if payment is None:
            return _error(404, "NOT_FOUND", f"payment {payment_id} not found")
        return JSONResponse(payment_json(payment))

    @app.post("/v1/refunds")
    async def create_refund(
        body: RefundCreate,
        request: Request,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        mock_fault: Annotated[str | None, Header(alias="X-Mock-Fault")] = None,
    ) -> JSONResponse:
        p = provider_of(request)
        try:
            fault = p.faults.next(mock_fault)
        except FaultSpecError as exc:
            return _error(400, "BAD_FAULT_SPEC", str(exc))

        if fault.latency_ms:
            await asyncio.sleep(fault.latency_ms / 1000)
        if fault.error_before_commit:
            return _error(
                fault.error_before_commit, "SERVER_ERROR", "injected: error before commit"
            )

        payload = body.model_dump()
        req_hash = request_hash(payload)
        if idempotency_key:
            existing = await p.store.claim_key(idempotency_key, req_hash)
            if existing is not None:
                if existing.request_hash != req_hash:
                    return _error(
                        422,
                        "IDEMPOTENCY_KEY_REUSED",
                        "Idempotency-Key was already used with a different request body",
                    )
                if existing.state == "in_flight":
                    return _error(
                        409, "IDEMPOTENCY_KEY_IN_USE", "a request with this key is in progress"
                    )
                return JSONResponse(
                    existing.response_body,
                    status_code=existing.response_status or 200,
                    headers={"Idempotent-Replayed": "true"},
                )

        try:
            status, response, events = await p.store.create_refund(
                body.payment_id,
                body.amount_paise,
                body.receipt,
                body.notes,
                idempotency_key or None,
                fault,
            )
        except Exception:
            if idempotency_key:
                await p.store.release_key(idempotency_key)
            raise
        for event in events:
            p.dispatcher.publish(event)

        # The refund is committed from here on. These faults lose the response, not the refund.
        if fault.error_after_commit:
            return _error(fault.error_after_commit, "SERVER_ERROR", "injected: error after commit")
        if fault.timeout_after_commit_ms:
            await asyncio.sleep(fault.timeout_after_commit_ms / 1000)
        return JSONResponse(response, status_code=status)

    @app.get("/v1/refunds/{refund_id}")
    async def get_refund(refund_id: str, request: Request) -> JSONResponse:
        refund = await provider_of(request).store.get_refund(refund_id)
        if refund is None:
            return _error(404, "NOT_FOUND", f"refund {refund_id} not found")
        return JSONResponse(refund_json(refund))

    @app.get("/v1/refunds")
    async def list_refunds(
        request: Request,
        payment_id: Annotated[str | None, Query()] = None,
        receipt: Annotated[str | None, Query()] = None,
    ) -> JSONResponse:
        refunds = await provider_of(request).store.list_refunds(payment_id, receipt)
        return JSONResponse({"entity": "collection", "items": [refund_json(r) for r in refunds]})

    # -------------------------------------------------------------- admin (test control)

    @app.post("/_admin/faults")
    async def script_faults(body: FaultScript, request: Request) -> JSONResponse:
        try:
            provider_of(request).faults.script(body.faults)
        except FaultSpecError as exc:
            return _error(400, "BAD_FAULT_SPEC", str(exc))
        return JSONResponse({"queued": len(provider_of(request).faults.scripted)})

    @app.delete("/_admin/faults")
    async def clear_faults(request: Request) -> dict[str, Any]:
        provider_of(request).faults.clear()
        return {"queued": 0}

    @app.post("/_admin/settle")
    async def settle(request: Request) -> dict[str, Any]:
        return {"settled": await provider_of(request).settle_due()}

    @app.post("/_admin/webhooks/flush")
    async def flush_webhooks(request: Request) -> dict[str, Any]:
        d = provider_of(request).dispatcher
        d.flush_held()
        await d.drain()
        return {"delivered": len(d.deliveries)}

    @app.get("/_admin/deliveries")
    async def deliveries(request: Request) -> dict[str, Any]:
        return {"items": [asdict(d) for d in provider_of(request).dispatcher.deliveries]}

    return app


def main() -> None:
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=8100)


if __name__ == "__main__":
    main()

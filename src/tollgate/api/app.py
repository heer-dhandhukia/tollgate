"""Tollgate HTTP service: health and the provider webhook receiver."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from tollgate.config import get_settings
from tollgate.guarded.webhooks import SIGNATURE_HEADER, WebhookReceiver


def create_app(
    maker: async_sessionmaker[AsyncSession] | None = None, webhook_secret: str | None = None
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings = get_settings()
        engine = None
        session_maker = maker
        if session_maker is None:
            engine = create_async_engine(settings.database_url, pool_pre_ping=True)
            session_maker = async_sessionmaker(engine, expire_on_commit=False)
        app.state.webhooks = WebhookReceiver(
            session_maker, webhook_secret or settings.mockpay_webhook_secret
        )
        try:
            yield
        finally:
            if engine is not None:
                await engine.dispose()

    app = FastAPI(title="tollgate", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/webhooks/mockpay")
    async def mockpay_webhook(request: Request) -> JSONResponse:
        receiver: WebhookReceiver = request.app.state.webhooks
        result = await receiver.handle(await request.body(), request.headers.get(SIGNATURE_HEADER))
        return JSONResponse({"outcome": result.outcome}, status_code=result.status_code)

    return app


app = create_app()

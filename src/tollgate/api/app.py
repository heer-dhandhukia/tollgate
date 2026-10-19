"""Tollgate HTTP service. Phase A1 exposes health only; the webhook receiver lands in A3."""

from fastapi import FastAPI

app = FastAPI(title="tollgate")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}

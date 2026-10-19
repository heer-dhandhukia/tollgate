"""HTTP client for the payment provider (mockpay). The timeout is enforced here, so it also
applies when the provider runs in-process over an ASGI transport."""

import asyncio
from typing import Any

import httpx


class ProviderTimeout(Exception):
    """No response in time. The request may or may not have been executed."""


class ProviderError(Exception):
    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status = status
        self.body = body
        description = (body.get("error") or {}).get("description", "") if body else ""
        super().__init__(f"provider returned {status}: {description}")

    @property
    def description(self) -> str:
        return str((self.body.get("error") or {}).get("description", ""))


class ProviderClient:
    def __init__(self, http: httpx.AsyncClient, timeout_s: float = 5.0) -> None:
        self.http = http
        self.timeout_s = timeout_s

    async def _request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await asyncio.wait_for(
                self.http.request(method, url, **kwargs), self.timeout_s
            )
        except (TimeoutError, httpx.TimeoutException) as exc:
            raise ProviderTimeout(f"{method} {url} timed out after {self.timeout_s}s") from exc
        try:
            body: dict[str, Any] = response.json()
        except ValueError:
            body = {}
        if not response.is_success:
            raise ProviderError(response.status_code, body)
        return body

    async def create_payment(
        self, amount_paise: int, payment_id: str, captured_paise: int | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {"id": payment_id, "amount_paise": amount_paise}
        if captured_paise is not None:
            payload["captured_paise"] = captured_paise
        return await self._request("POST", "/v1/payments", json=payload)

    async def create_refund(
        self,
        payment_id: str,
        amount_paise: int,
        *,
        idempotency_key: str | None = None,
        receipt: str | None = None,
        notes: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        payload: dict[str, Any] = {"payment_id": payment_id, "amount_paise": amount_paise}
        if receipt is not None:
            payload["receipt"] = receipt
        if notes:
            payload["notes"] = notes
        return await self._request("POST", "/v1/refunds", json=payload, headers=headers)

    async def list_refunds(
        self, payment_id: str | None = None, receipt: str | None = None
    ) -> list[dict[str, Any]]:
        params = {k: v for k, v in {"payment_id": payment_id, "receipt": receipt}.items() if v}
        body = await self._request("GET", "/v1/refunds", params=params)
        return list(body["items"])

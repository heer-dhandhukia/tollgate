"""Idempotency-Key behaviour and the provider-side refund cap."""

import asyncio

import pytest

from tests.mockpay.conftest import Mockpay

pytestmark = pytest.mark.db


async def test_same_key_same_body_replays_stored_response(mockpay: Mockpay) -> None:
    pid = await mockpay.payment()
    body = {"payment_id": pid, "amount_paise": 10_000}
    first = await mockpay.client.post("/v1/refunds", json=body, headers={"Idempotency-Key": "k1a"})
    again = await mockpay.client.post("/v1/refunds", json=body, headers={"Idempotency-Key": "k1a"})
    assert first.status_code == again.status_code == 200
    assert again.json() == first.json()
    assert again.headers["Idempotent-Replayed"] == "true"
    assert len(await mockpay.refunds_for(pid)) == 1


async def test_same_key_different_body_rejected(mockpay: Mockpay) -> None:
    pid = await mockpay.payment()
    headers = {"Idempotency-Key": "k2a"}
    ok = await mockpay.client.post(
        "/v1/refunds", json={"payment_id": pid, "amount_paise": 10_000}, headers=headers
    )
    bad = await mockpay.client.post(
        "/v1/refunds", json={"payment_id": pid, "amount_paise": 20_000}, headers=headers
    )
    assert ok.status_code == 200
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert len(await mockpay.refunds_for(pid)) == 1


async def test_concurrent_requests_with_same_key_create_one_refund(mockpay: Mockpay) -> None:
    pid = await mockpay.payment()
    body = {"payment_id": pid, "amount_paise": 10_000}
    responses = await asyncio.gather(
        *(
            mockpay.client.post(
                "/v1/refunds",
                json=body,
                headers={"Idempotency-Key": "k3a", "X-Mock-Fault": "latency=50"},
            )
            for _ in range(5)
        )
    )
    assert {r.status_code for r in responses} <= {200, 409}
    assert len(await mockpay.refunds_for(pid)) == 1


async def test_without_a_key_every_retry_is_a_new_refund(mockpay: Mockpay) -> None:
    """The danger the naive agent walks into: no key, so a retry moves money twice."""
    pid = await mockpay.payment()
    body = {"payment_id": pid, "amount_paise": 10_000}
    for _ in range(2):
        assert (await mockpay.client.post("/v1/refunds", json=body)).status_code == 200
    assert len(await mockpay.refunds_for(pid)) == 2


async def test_provider_refuses_refunds_beyond_capture(mockpay: Mockpay) -> None:
    pid = await mockpay.payment(100_000)
    post = mockpay.client.post
    assert (await post("/v1/refunds", json={"payment_id": pid, "amount_paise": 70_000})).is_success
    over = await post("/v1/refunds", json={"payment_id": pid, "amount_paise": 30_001})
    assert over.status_code == 400
    assert (await post("/v1/refunds", json={"payment_id": pid, "amount_paise": 30_000})).is_success


async def test_failed_validation_is_cached_under_the_key(mockpay: Mockpay) -> None:
    pid = await mockpay.payment(1_000)
    body = {"payment_id": pid, "amount_paise": 5_000}
    first = await mockpay.client.post("/v1/refunds", json=body, headers={"Idempotency-Key": "k4a"})
    again = await mockpay.client.post("/v1/refunds", json=body, headers={"Idempotency-Key": "k4a"})
    assert first.status_code == again.status_code == 400
    assert again.headers["Idempotent-Replayed"] == "true"


@pytest.mark.parametrize("amount", [100.0, 99.5, "100", True, 0, -5])
async def test_amount_must_be_positive_integer_paise(mockpay: Mockpay, amount: object) -> None:
    pid = await mockpay.payment()
    r = await mockpay.client.post("/v1/refunds", json={"payment_id": pid, "amount_paise": amount})
    assert r.status_code == 422


async def test_unknown_payment_is_404(mockpay: Mockpay) -> None:
    r = await mockpay.client.post(
        "/v1/refunds", json={"payment_id": "pay_missing", "amount_paise": 1}
    )
    assert r.status_code == 404

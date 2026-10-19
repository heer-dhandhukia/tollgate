"""Faults through the HTTP API, especially the ones that commit and then lose the response."""

import asyncio

import pytest

from tests.mockpay.conftest import Mockpay

pytestmark = pytest.mark.db


async def test_error_before_commit_persists_nothing(mockpay: Mockpay) -> None:
    pid = await mockpay.payment()
    r = await mockpay.client.post(
        "/v1/refunds",
        json={"payment_id": pid, "amount_paise": 10_000},
        headers={"X-Mock-Fault": "error_before_commit=500", "Idempotency-Key": "f1a"},
    )
    assert r.status_code == 500
    assert await mockpay.refunds_for(pid) == []
    # Nothing was recorded under the key either, so a retry executes normally.
    retry = await mockpay.client.post(
        "/v1/refunds",
        json={"payment_id": pid, "amount_paise": 10_000},
        headers={"Idempotency-Key": "f1a"},
    )
    assert retry.status_code == 200
    assert "Idempotent-Replayed" not in retry.headers


async def test_error_after_commit_refund_exists_and_keyed_retry_replays(mockpay: Mockpay) -> None:
    pid = await mockpay.payment()
    body = {"payment_id": pid, "amount_paise": 10_000}
    r = await mockpay.client.post(
        "/v1/refunds",
        json=body,
        headers={"X-Mock-Fault": "error_after_commit", "Idempotency-Key": "f2a"},
    )
    assert r.status_code == 502
    [refund] = await mockpay.refunds_for(pid)

    retry = await mockpay.client.post("/v1/refunds", json=body, headers={"Idempotency-Key": "f2a"})
    assert retry.status_code == 200
    assert retry.json()["id"] == refund["id"]
    assert len(await mockpay.refunds_for(pid)) == 1


async def test_error_after_commit_unkeyed_retry_double_refunds(mockpay: Mockpay) -> None:
    pid = await mockpay.payment()
    body = {"payment_id": pid, "amount_paise": 10_000}
    r = await mockpay.client.post(
        "/v1/refunds", json=body, headers={"X-Mock-Fault": "error_after_commit"}
    )
    assert r.status_code == 502
    assert (await mockpay.client.post("/v1/refunds", json=body)).status_code == 200
    assert len(await mockpay.refunds_for(pid)) == 2


async def test_timeout_after_commit_client_gives_up_but_refund_exists(mockpay: Mockpay) -> None:
    pid = await mockpay.payment()
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            mockpay.client.post(
                "/v1/refunds",
                json={"payment_id": pid, "amount_paise": 10_000, "receipt": "intent-42"},
                headers={"X-Mock-Fault": "timeout_after_commit=2000"},
            ),
            timeout=0.3,
        )
    found = await mockpay.client.get("/v1/refunds", params={"receipt": "intent-42"})
    [refund] = [i for i in found.json()["items"] if i["payment_id"] == pid]
    assert refund["amount_paise"] == 10_000


async def test_scripted_faults_apply_in_order(mockpay: Mockpay) -> None:
    pid = await mockpay.payment()
    queued = await mockpay.client.post(
        "/_admin/faults", json={"faults": ["error_before_commit", "none", "error_after_commit"]}
    )
    assert queued.json() == {"queued": 3}
    codes = [
        (
            await mockpay.client.post(
                "/v1/refunds", json={"payment_id": pid, "amount_paise": 1_000}
            )
        ).status_code
        for _ in range(4)
    ]
    assert codes == [503, 200, 502, 200]
    assert len(await mockpay.refunds_for(pid)) == 3


async def test_bad_fault_header_is_400(mockpay: Mockpay) -> None:
    pid = await mockpay.payment()
    r = await mockpay.client.post(
        "/v1/refunds",
        json={"payment_id": pid, "amount_paise": 1},
        headers={"X-Mock-Fault": "meteor_strike"},
    )
    assert r.status_code == 400


async def test_refund_fails_releases_provider_reservation(mockpay: Mockpay) -> None:
    pid = await mockpay.payment(10_000)
    r = await mockpay.client.post(
        "/v1/refunds",
        json={"payment_id": pid, "amount_paise": 10_000},
        headers={"X-Mock-Fault": "refund_fails"},
    )
    assert r.json()["status"] == "pending"
    await mockpay.settle_and_deliver()
    refund = (await mockpay.client.get(f"/v1/refunds/{r.json()['id']}")).json()
    assert refund["status"] == "failed"
    payment = (await mockpay.client.get(f"/v1/payments/{pid}")).json()
    assert payment["refunded_paise"] == 0

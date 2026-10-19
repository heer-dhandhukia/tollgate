"""Webhook signing, ordering, duplication and retries."""

import json

import httpx
import pytest

from mockpay.config import MockpaySettings
from mockpay.store import OutgoingEvent
from mockpay.webhooks import EVENT_ID_HEADER, SIGNATURE_HEADER, Dispatcher, sign, verify
from tests.mockpay.conftest import SECRET, Mockpay, Recorder


def test_signature_round_trip() -> None:
    body = b'{"id":"evt_1"}'
    assert verify(SECRET, body, sign(SECRET, body))
    assert not verify(SECRET, body + b" ", sign(SECRET, body))
    assert not verify("other", body, sign(SECRET, body))


def _event(
    seq: int, refund: str = "rfnd_1", mode: str = "", terminal: bool = False
) -> OutgoingEvent:
    envelope = {"id": f"evt_{seq}", "sequence": seq, "type": "refund.x", "data": {}}
    return OutgoingEvent(envelope, refund, mode, terminal)


async def _dispatcher(recorder: Recorder) -> Dispatcher:
    settings = MockpaySettings(webhook_url="http://hook.test/", webhook_secret=SECRET)
    d = Dispatcher(settings, httpx.MockTransport(recorder))
    await d.start()
    return d


async def test_duplicate_mode_delivers_each_event_n_times() -> None:
    recorder = Recorder()
    d = await _dispatcher(recorder)
    d.publish(_event(1, mode="duplicate:3"))
    await d.drain()
    await d.stop()
    assert [b["id"] for b in recorder.bodies()] == ["evt_1"] * 3


async def test_reverse_mode_holds_until_terminal_then_delivers_newest_first() -> None:
    recorder = Recorder()
    d = await _dispatcher(recorder)
    d.publish(_event(1, mode="reverse"))
    await d.drain()
    assert recorder.requests == []
    d.publish(_event(2, mode="reverse", terminal=True))
    await d.drain()
    await d.stop()
    assert [b["sequence"] for b in recorder.bodies()] == [2, 1]


async def test_failed_delivery_is_retried() -> None:
    recorder = Recorder(status=500)
    d = await _dispatcher(recorder)
    d.publish(_event(1))
    await d.drain()
    await d.stop()
    assert len(recorder.requests) == d.settings.webhook_max_attempts
    assert [x.attempt for x in d.deliveries] == [1, 2, 3]


@pytest.mark.db
async def test_refund_lifecycle_webhooks_are_signed_and_ordered(mockpay: Mockpay) -> None:
    pid = await mockpay.payment()
    r = await mockpay.client.post("/v1/refunds", json={"payment_id": pid, "amount_paise": 500})
    await mockpay.settle_and_deliver()
    mine = [req for req in mockpay.recorder.requests if pid in req.content.decode()]
    events = [json.loads(req.content) for req in mine]
    assert [e["type"] for e in events] == ["refund.created", "refund.processed"]
    assert events[0]["sequence"] < events[1]["sequence"]
    assert events[1]["data"]["refund"]["id"] == r.json()["id"]
    for req in mine:
        assert verify(SECRET, req.content, req.headers[SIGNATURE_HEADER])
        assert req.headers[EVENT_ID_HEADER] == json.loads(req.content)["id"]


@pytest.mark.db
async def test_duplicate_webhook_fault_end_to_end(mockpay: Mockpay) -> None:
    pid = await mockpay.payment()
    await mockpay.client.post(
        "/v1/refunds",
        json={"payment_id": pid, "amount_paise": 500},
        headers={"X-Mock-Fault": "duplicate_webhook=3"},
    )
    await mockpay.settle_and_deliver()
    types = [b["type"] for b in mockpay.recorder.bodies() if pid in json.dumps(b)]
    assert types == ["refund.created"] * 3 + ["refund.processed"] * 3


@pytest.mark.db
async def test_out_of_order_webhook_fault_end_to_end(mockpay: Mockpay) -> None:
    pid = await mockpay.payment()
    await mockpay.client.post(
        "/v1/refunds",
        json={"payment_id": pid, "amount_paise": 500},
        headers={"X-Mock-Fault": "out_of_order_webhooks"},
    )
    await mockpay.provider.dispatcher.drain()
    assert not [b for b in mockpay.recorder.bodies() if pid in json.dumps(b)]
    await mockpay.settle_and_deliver()
    events = [b for b in mockpay.recorder.bodies() if pid in json.dumps(b)]
    assert [e["type"] for e in events] == ["refund.processed", "refund.created"]
    assert events[0]["sequence"] > events[1]["sequence"]

"""The webhook must answer fast, and must not serialise behind slow work.

These are the tests that speak directly to the production incident. It was not
caused by a single slow request but by a feedback loop: slow synchronous work
ran on the event loop, so *every* concurrent request — including 3ms delivery
receipts — queued behind it. Latency climbed to 82 seconds across 60 requests,
Meta timed out, retried, and the retries made it worse.

So there are two properties to hold, not one:

1. the acknowledgement is fast, and
2. slow processing does not block anything else.

Property 2 needs a real server with a single event loop and lives in
`test_event_loop_not_blocked.py`; `TestClient` builds a fresh event loop per
request, so it cannot observe loop blocking at all.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from tests.fakes.whatsapp import signed, status_callback, text_message

# Generous versus the ~50ms this actually takes, but far under any plausible
# webhook timeout. The point is to catch a regression to synchronous processing,
# not to benchmark the machine.
ACK_BUDGET_SECONDS = 1.0


@pytest.fixture
def client(nano_app) -> TestClient:
    return TestClient(nano_app)


def post(client: TestClient, payload: dict, app_secret: str):
    body, headers = signed(payload, app_secret)
    return client.post("/webhook", content=body, headers=headers)


class TestAcknowledgementIsFast:
    def test_status_callbacks_are_cheap(self, client: TestClient, app_secret: str):
        """These are most of the inbound traffic and do no work at all."""
        start = time.perf_counter()
        for _ in range(20):
            post(client, status_callback(), app_secret)
        elapsed = time.perf_counter() - start
        assert elapsed < ACK_BUDGET_SECONDS, f"20 status callbacks took {elapsed:.2f}s"

    def test_a_forged_request_is_rejected_without_doing_work(
        self, client: TestClient, fake_whatsapp, fake_bq
    ):
        start = time.perf_counter()
        response = post(client, text_message(), "wrong-secret")
        elapsed = time.perf_counter() - start

        assert response.status_code == 403
        assert elapsed < ACK_BUDGET_SECONDS
        assert fake_whatsapp.sent == [], "no work should happen before authentication"
        assert fake_bq.queries == []


class TestRoutesDeclaredCorrectly:
    """Guard the mechanism that keeps blocking work off the loop.

    FastAPI runs a `def` endpoint in a worker thread and an `async def` endpoint
    on the event loop. `/tasks/process` does all the slow work, so it must stay
    synchronous; if someone makes it `async def`, the original failure mode
    returns silently.
    """

    def test_task_processing_route_is_synchronous(self):
        import inspect

        from nano.routes import process_task

        assert not inspect.iscoroutinefunction(process_task), (
            "/tasks/process must stay `def`, not `async def`: it makes blocking "
            "BigQuery, Gemini and HTTP calls, which on the event loop would stall "
            "every concurrent request — the original incident."
        )

    def test_webhook_route_is_async_and_awaits_the_raw_body(self):
        import inspect

        from nano.routes import receive_webhook

        assert inspect.iscoroutinefunction(receive_webhook)
        source = inspect.getsource(receive_webhook)
        assert "await request.body()" in source, "signature must cover the raw bytes"
        assert "run_in_threadpool" in source, "blocking calls must leave the event loop"

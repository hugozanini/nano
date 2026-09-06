"""Prove that slow processing cannot stall concurrent requests.

This runs a real uvicorn server rather than using `TestClient`, and the reason
is worth recording: `TestClient` spins up a **fresh event loop per request**, so
a handler that blocks the loop does not affect any other `TestClient` request.
A concurrency test written against it passes whether the bug is present or not —
confirmed by mutation, which is how this file came to exist.

The failure being guarded is the production incident. `receive()` was
`async def` but called `requests.post`, `bq_client.query().result()` and the
Vertex SDK directly, so a single slow receipt froze every other request on the
instance, including 3ms delivery receipts. Latency reached 82 seconds across 60
concurrent requests, Meta timed out, retried, and the retries compounded it.

One event loop, two requests: if the slow one blocks it, the fast one waits.
"""

from __future__ import annotations

import socket
import threading
import time

import httpx
import pytest
import uvicorn

from tests.fakes.whatsapp import signed, status_callback, text_message

# The slow handler parks for this long. Comfortably longer than a healthy
# response, comfortably shorter than the test timeout.
BLOCK_SECONDS = 3.0

# A concurrent request must be served while the slow one is parked. Generous
# against the ~10ms it really takes, but far below BLOCK_SECONDS, so a blocked
# loop is unambiguous.
CONCURRENT_BUDGET = 1.0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def live_server(nano_app):
    """A real uvicorn server with one event loop, in a background thread."""
    port = _free_port()
    config = uvicorn.Config(nano_app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    if not server.started:
        server.should_exit = True
        pytest.fail("uvicorn did not start")

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=10)


def test_slow_processing_does_not_stall_concurrent_requests(
    live_server: str, app_secret: str, monkeypatch
):
    from nano import handlers

    started = threading.Event()
    real_process = handlers.process_payload

    def slow_process(payload, settings=None):
        started.set()
        time.sleep(BLOCK_SECONDS)
        return real_process(payload, settings)

    monkeypatch.setattr(handlers, "process_payload", slow_process)

    slow_body, slow_headers = signed(
        text_message("add coffee", message_id="wamid.SLOW"), app_secret
    )
    fast_body, fast_headers = signed(status_callback(), app_secret)

    slow_status: list[int] = []

    def fire_slow():
        with httpx.Client(timeout=30) as client:
            response = client.post(
                f"{live_server}/webhook", content=slow_body, headers=slow_headers
            )
            slow_status.append(response.status_code)

    slow_thread = threading.Thread(target=fire_slow)
    slow_thread.start()

    assert started.wait(timeout=10), "the slow handler never ran"

    # The slow handler is now parked. A second request must still be served.
    with httpx.Client(timeout=30) as client:
        began = time.perf_counter()
        fast_response = client.post(
            f"{live_server}/webhook", content=fast_body, headers=fast_headers
        )
        elapsed = time.perf_counter() - began

    slow_thread.join(timeout=30)

    assert fast_response.status_code == 200
    assert elapsed < CONCURRENT_BUDGET, (
        f"a concurrent request waited {elapsed:.2f}s while one message was being "
        f"processed. Blocking work is running on the event loop again — this is "
        f"the exact failure that produced 82-second latencies in production."
    )
    assert slow_status == [200]


def test_the_webhook_answers_before_processing_finishes(
    live_server: str, app_secret: str, monkeypatch
):
    """Meta gets its 200 without waiting for Gemini or BigQuery."""
    from nano import handlers

    real_process = handlers.process_payload

    def slow_process(payload, settings=None):
        time.sleep(BLOCK_SECONDS)
        return real_process(payload, settings)

    monkeypatch.setattr(handlers, "process_payload", slow_process)

    body, headers = signed(text_message(message_id="wamid.ACKFAST"), app_secret)

    with httpx.Client(timeout=30) as client:
        began = time.perf_counter()
        response = client.post(f"{live_server}/webhook", content=body, headers=headers)
        elapsed = time.perf_counter() - began

    assert response.status_code == 200
    assert elapsed < CONCURRENT_BUDGET, (
        f"the webhook took {elapsed:.2f}s to acknowledge; it is waiting for "
        f"processing to finish instead of handing it off"
    )

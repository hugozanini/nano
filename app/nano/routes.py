"""HTTP surface.

The webhook's whole job is to decide, in milliseconds, whether a payload is
genuine and worth processing — then hand it off and return 200. It does no
network I/O of its own beyond enqueueing.

Two rules hold everywhere in this module:

**Never return 5xx to Meta.** A non-2xx is a redelivery instruction. The original
service could take 82 seconds to answer, which Meta read as failure, and each
redelivery re-ran the whole receipt. Anything that goes wrong internally is
logged and answered 200; only a forged signature gets a non-2xx, because there
is nothing to redeliver.

**Never block the event loop.** ``POST /webhook`` is async because it must await
the raw body, so its two blocking calls go through ``run_in_threadpool``.
``POST /tasks/process`` is a plain ``def``, which FastAPI runs in a worker
thread, so all the slow work there is free to block.
"""

from __future__ import annotations

import hmac
import logging
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Header, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from starlette.concurrency import run_in_threadpool

from nano import handlers, tasks, whatsapp
from nano.config import get_settings
from nano.logging_setup import bind_request, log_with, redact_number

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness only — deliberately makes no external calls."""
    return {"status": "ok"}


@router.get("/webhook")
def verify_webhook(request: Request) -> Response:
    """Meta's subscription handshake.

    Returns the challenge as plain text. The original returned ``int(challenge)``
    and raised a TypeError when the parameter was absent, which is how the two
    404s in the logs were generated during setup.
    """
    params = request.query_params
    token = params.get("hub.verify_token", "")
    challenge = params.get("hub.challenge", "")
    settings = get_settings()

    if not hmac.compare_digest(token, settings.verify_token):
        logger.warning("webhook verification failed: bad verify token")
        return PlainTextResponse("invalid verify token", status_code=403)

    if not challenge:
        return PlainTextResponse("missing hub.challenge", status_code=400)

    logger.info("webhook verification succeeded")
    return PlainTextResponse(challenge)


@router.post("/webhook")
async def receive_webhook(
    request: Request,
    background: BackgroundTasks,
    x_hub_signature_256: str | None = Header(default=None),
) -> Response:
    """Authenticate, enqueue, acknowledge. Target: well under 100ms."""
    raw_body = await request.body()
    settings = get_settings()

    bind_request(trace_header=request.headers.get("X-Cloud-Trace-Context", ""))

    if settings.require_signature:
        if not whatsapp.verify_signature(
            raw_body, x_hub_signature_256, settings.whatsapp_app_secret
        ):
            logger.warning("rejected webhook with an invalid signature")
            return JSONResponse({"error": "invalid signature"}, status_code=403)
    elif x_hub_signature_256 is None:
        logger.critical("accepting an unsigned webhook: WHATSAPP_APP_SECRET is not configured")

    try:
        payload: dict[str, Any] = await request.json()
    except Exception:
        logger.warning("webhook body was not valid JSON")
        return JSONResponse({"status": "ignored"}, status_code=200)

    message = whatsapp.parse_inbound(payload)
    if message is None:
        # Delivery-status callbacks land here and are the bulk of traffic.
        return JSONResponse({"status": "ignored"}, status_code=200)

    bind_request(message_id=message.id)

    if not settings.is_household(message.sender):
        log_with(
            logger,
            logging.WARNING,
            "ignoring message from a non-household number",
            sender=redact_number(message.sender),
        )
        # Deliberately no reply: answering costs money and confirms the number
        # is live to whoever is probing it.
        return JSONResponse({"status": "ignored"}, status_code=200)

    try:
        backend = await run_in_threadpool(
            tasks.enqueue,
            payload,
            message.id,
            settings,
            background.add_task,
            handlers.process_payload,
        )
        log_with(logger, logging.INFO, "message accepted", backend=backend, type=message.type)
        return JSONResponse({"status": "accepted"}, status_code=200)

    except tasks.DuplicateTask:
        return JSONResponse({"status": "duplicate"}, status_code=200)

    except Exception:
        # Still a 200. A 5xx would make Meta redeliver, and redelivery is the
        # failure mode this whole change exists to stop.
        logger.exception("could not enqueue message")
        return JSONResponse({"status": "error"}, status_code=200)


@router.post("/tasks/process")
def process_task(
    payload: dict[str, Any],
    authorization: str | None = Header(default=None),
) -> Response:
    """Do the real work. Called by Cloud Tasks, never by Meta.

    A plain ``def`` on purpose: FastAPI runs it in a worker thread, so the slow
    blocking calls inside cannot stall the event loop that serves the webhook.

    This route is publicly reachable — the service must stay unauthenticated for
    Meta — so the OIDC token is verified in-process.
    """
    if not tasks.verify_task_request(authorization):
        return JSONResponse({"error": "unauthorised"}, status_code=403)

    try:
        outcome = handlers.process_payload(payload)
        return JSONResponse({"status": outcome}, status_code=200)
    except Exception:
        logger.exception("task processing failed")
        # 500 here is correct and wanted: this is Cloud Tasks, not Meta, and a
        # 500 is what triggers its retry with backoff.
        return JSONResponse({"error": "processing failed"}, status_code=500)

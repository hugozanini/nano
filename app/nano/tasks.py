"""Getting slow work off the webhook request path.

The production incident was a feedback loop: receipt handling did ~40 seconds of
synchronous work, Meta timed out and retried, retries piled onto the same
instance and made everything slower, so they timed out too. Latency peaked at 82
seconds across 60 concurrent requests, and every completed attempt wrote another
duplicate purchase.

The fix is to acknowledge Meta in milliseconds and do the work elsewhere.

Two backends:

- ``cloud_tasks`` (production) enqueues an OIDC-authenticated task that calls
  back into ``/tasks/process``. Cloud Tasks gives real retries with backoff, and
  because the task name is derived from the WhatsApp message id it also rejects
  a duplicate enqueue outright — a second dedupe layer, free, before Firestore
  is even touched.
- ``inline`` (local development and tests) runs the same handler through
  FastAPI's background tasks.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
from collections.abc import Callable
from typing import Any

from nano.config import Settings, get_settings
from nano.logging_setup import log_with

logger = logging.getLogger(__name__)

# Cloud Tasks retains completed task names for roughly an hour and refuses to
# recreate one, which is exactly the dedupe window we want for webhook retries.
TASK_DEADLINE = datetime.timedelta(seconds=540)


def task_name_for(message_id: str) -> str:
    """A deterministic, Cloud Tasks-safe id.

    WhatsApp ids look like `wamid.HBgLMzQ2...==`, which contains characters the
    API rejects, so hash rather than sanitise: same input always yields the same
    name, which is what makes the dedupe work.
    """
    return "msg-" + hashlib.sha256(message_id.encode()).hexdigest()[:48]


class DuplicateTask(Exception):
    """The task already exists, so this delivery is a retry we can drop."""


def enqueue(
    payload: dict[str, Any],
    message_id: str,
    settings: Settings | None = None,
    background_runner: Callable[..., None] | None = None,
    handler: Callable[[dict[str, Any]], None] | None = None,
) -> str:
    """Schedule processing of one inbound message.

    Returns the backend used. Raises DuplicateTask if Cloud Tasks already holds
    a task for this message id.
    """
    settings = settings or get_settings()

    if settings.uses_cloud_tasks:
        _enqueue_cloud_task(payload, message_id, settings)
        return "cloud_tasks"

    if background_runner is None or handler is None:
        raise RuntimeError("inline backend requires a background runner and handler")

    background_runner(handler, payload)
    return "inline"


def _enqueue_cloud_task(payload: dict[str, Any], message_id: str, settings: Settings) -> None:
    from google.api_core import exceptions
    from google.cloud import tasks_v2

    from nano.clients import tasks_client

    client = tasks_client()
    parent = client.queue_path(settings.project_id, settings.tasks_location, settings.tasks_queue)

    task = {
        "name": f"{parent}/tasks/{task_name_for(message_id)}",
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": settings.tasks_target_url,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(payload).encode(),
            "oidc_token": {
                "service_account_email": settings.tasks_invoker_sa,
                "audience": settings.tasks_target_url,
            },
        },
        "dispatch_deadline": TASK_DEADLINE,
    }

    try:
        client.create_task(parent=parent, task=task)
        log_with(logger, logging.INFO, "task enqueued", message_id=message_id)
    except exceptions.AlreadyExists as exc:
        log_with(
            logger,
            logging.INFO,
            "task already enqueued; dropping duplicate delivery",
            message_id=message_id,
        )
        raise DuplicateTask(message_id) from exc


# --------------------------------------------------------------------------
# Caller authentication for /tasks/process
# --------------------------------------------------------------------------


def verify_task_request(authorization: str | None, settings: Settings | None = None) -> bool:
    """Check the OIDC token Cloud Tasks attaches.

    ``/tasks/process`` sits on a service that must stay publicly reachable so
    Meta can post to it, so Cloud Run's own IAM cannot protect this route. The
    token is verified here instead: correct issuer, correct audience, and the
    service account we expect.

    With the inline backend there is no token and no Cloud Tasks, so the check
    is skipped — but only in that configuration.
    """
    settings = settings or get_settings()

    if not settings.uses_cloud_tasks:
        return True

    if not authorization or not authorization.startswith("Bearer "):
        logger.warning("task request rejected: no bearer token")
        return False

    token = authorization.removeprefix("Bearer ").strip()

    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token

        claims = id_token.verify_oauth2_token(
            token,
            google_requests.Request(),
            audience=settings.tasks_target_url,
        )
    except Exception as exc:
        log_with(logger, logging.WARNING, "task request rejected: bad token", error=str(exc))
        return False

    email = claims.get("email", "")
    if settings.tasks_invoker_sa and email != settings.tasks_invoker_sa:
        log_with(
            logger,
            logging.WARNING,
            "task request rejected: unexpected service account",
            presented=email,
        )
        return False

    if not claims.get("email_verified", False):
        logger.warning("task request rejected: email not verified")
        return False

    return True

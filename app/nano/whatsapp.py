"""WhatsApp Cloud API: signature verification, inbound parsing, outbound sending.

Three things here are load-bearing for the reliability of the whole service.

**Signatures.** The Cloud Run service is `--allow-unauthenticated` because Meta
has to reach it, so the HMAC is the only thing separating real messages from
anyone who finds the URL. It must be computed over the *raw request bytes*,
before any JSON parsing, because re-serialising changes them.

**Sends never raise.** `send_text` returns a bool. The previous implementation
ended in `raise_for_status()`, which meant one undeliverable recipient could take
down the whole request. A permanently blocked number (Meta error 130497) is a
log line, not an outage.

**Parsing tolerates reality.** Meta sends delivery-status callbacks, unsupported
message types, and occasionally payloads with none of the keys the happy path
expects. All of them must return "nothing to do" rather than raise.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from typing import Any

import requests

from nano.config import Settings, get_settings
from nano.logging_setup import log_with, redact_number

logger = logging.getLogger(__name__)

# Meta error codes that will never succeed on retry, so retrying wastes time and
# quota. 130497 is the cross-border messaging restriction.
PERMANENT_ERROR_CODES = {
    100,  # invalid parameter
    131026,  # message undeliverable
    130497,  # cross-border restriction
    131047,  # re-engagement outside the 24h window
    131051,  # unsupported message type
}

SEND_TIMEOUT = 10
MEDIA_TIMEOUT = 30
MAX_SEND_ATTEMPTS = 3


@dataclass(frozen=True)
class InboundMessage:
    """One actionable inbound message, normalised."""

    id: str
    sender: str
    type: str
    text: str = ""
    media_id: str = ""
    mime_type: str = ""
    timestamp: str = ""

    @property
    def is_text(self) -> bool:
        return self.type == "text"

    @property
    def is_image(self) -> bool:
        return self.type == "image"


# --------------------------------------------------------------------------
# Signature verification
# --------------------------------------------------------------------------


def compute_signature(raw_body: bytes, app_secret: str) -> str:
    digest = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def verify_signature(raw_body: bytes, header: str | None, app_secret: str) -> bool:
    """Constant-time check of Meta's X-Hub-Signature-256 over the raw bytes."""
    if not header or not app_secret:
        return False
    if not header.startswith("sha256="):
        return False
    return hmac.compare_digest(compute_signature(raw_body, app_secret), header)


# --------------------------------------------------------------------------
# Inbound parsing
# --------------------------------------------------------------------------


def parse_inbound(payload: dict[str, Any]) -> InboundMessage | None:
    """Extract the single actionable message from a webhook payload.

    Returns None for anything Nano should ignore: delivery-status callbacks,
    malformed envelopes, or payloads carrying no messages at all. Never raises.
    """
    try:
        value = payload["entry"][0]["changes"][0]["value"]
    except (KeyError, IndexError, TypeError):
        return None

    if not isinstance(value, dict):
        return None

    # Status callbacks (sent/delivered/read) arrive constantly and carry no
    # `messages` key. They are the majority of inbound traffic.
    messages = value.get("messages")
    if not messages:
        return None

    try:
        message = messages[0]
        message_id = message["id"]
        sender = message["from"]
        msg_type = message.get("type", "unknown")
    except (KeyError, IndexError, TypeError):
        return None

    text = ""
    media_id = ""
    mime_type = ""

    if msg_type == "text":
        text = (message.get("text") or {}).get("body", "")
    elif msg_type in {"image", "document"}:
        media = message.get(msg_type) or {}
        media_id = media.get("id", "")
        mime_type = media.get("mime_type", "image/jpeg")

    return InboundMessage(
        id=message_id,
        sender=sender,
        type=msg_type,
        text=text,
        media_id=media_id,
        mime_type=mime_type,
        timestamp=message.get("timestamp", ""),
    )


# --------------------------------------------------------------------------
# Outbound
# --------------------------------------------------------------------------


def _graph_url(settings: Settings, path: str) -> str:
    return f"https://graph.facebook.com/{settings.graph_api_version}/{path}"


def _error_code(response: requests.Response) -> int | None:
    try:
        return (response.json().get("error") or {}).get("code")
    except ValueError:
        return None


def send_text(to_number: str, text: str, settings: Settings | None = None) -> bool:
    """Send a message. Returns success; never raises.

    Retries transient failures (429, 5xx, connection errors) with backoff, and
    gives up immediately on codes that cannot succeed.
    """
    settings = settings or get_settings()
    url = _graph_url(settings, f"{settings.phone_number_id}/messages")
    headers = {
        "Authorization": f"Bearer {settings.whatsapp_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": text},
    }

    for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=SEND_TIMEOUT)
        except requests.RequestException as exc:
            log_with(
                logger,
                logging.WARNING,
                "whatsapp send failed with a network error",
                recipient=redact_number(to_number),
                attempt=attempt,
                error=str(exc),
            )
            if attempt == MAX_SEND_ATTEMPTS:
                return False
            time.sleep(2 ** (attempt - 1))
            continue

        if response.ok:
            log_with(
                logger,
                logging.INFO,
                "whatsapp message sent",
                recipient=redact_number(to_number),
                attempt=attempt,
            )
            return True

        code = _error_code(response)
        permanent = code in PERMANENT_ERROR_CODES or (
            400 <= response.status_code < 500 and response.status_code != 429
        )

        log_with(
            logger,
            logging.WARNING,
            "whatsapp send rejected",
            recipient=redact_number(to_number),
            status=response.status_code,
            meta_error_code=code,
            permanent=permanent,
            attempt=attempt,
        )

        if permanent or attempt == MAX_SEND_ATTEMPTS:
            return False
        time.sleep(2 ** (attempt - 1))

    return False


def notify(numbers: list[str], text: str, settings: Settings | None = None) -> dict[str, bool]:
    """Fan out to several recipients. One failure never affects the others."""
    return {number: send_text(number, text, settings) for number in numbers}


def mark_as_read(message_id: str, settings: Settings | None = None) -> bool:
    """Show the blue ticks. Cosmetic, so failure is logged and swallowed."""
    settings = settings or get_settings()
    try:
        response = requests.post(
            _graph_url(settings, f"{settings.phone_number_id}/messages"),
            headers={
                "Authorization": f"Bearer {settings.whatsapp_token}",
                "Content-Type": "application/json",
            },
            json={
                "messaging_product": "whatsapp",
                "status": "read",
                "message_id": message_id,
            },
            timeout=SEND_TIMEOUT,
        )
        return response.ok
    except requests.RequestException:
        return False


def download_media(media_id: str, settings: Settings | None = None) -> tuple[bytes, str]:
    """Fetch media in the two steps Meta requires: resolve URL, then download."""
    settings = settings or get_settings()
    headers = {"Authorization": f"Bearer {settings.whatsapp_token}"}

    meta_response = requests.get(
        _graph_url(settings, media_id), headers=headers, timeout=SEND_TIMEOUT
    )
    meta_response.raise_for_status()
    meta = meta_response.json()

    media_response = requests.get(meta["url"], headers=headers, timeout=MEDIA_TIMEOUT)
    media_response.raise_for_status()

    return media_response.content, meta.get("mime_type", "image/jpeg")

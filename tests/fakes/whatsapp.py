"""Capture outbound WhatsApp traffic and build realistic inbound payloads.

The payload builders matter as much as the capture: several of Nano's bugs came
from payload shapes that were never exercised (delivery-status callbacks, image
messages, unknown senders), so tests need to construct the real thing rather than
a convenient simplification.

**No real identifiers appear in this file.** Phone numbers come from the
+1-555-01xx block reserved for fiction, and the WhatsApp/business IDs are
all-zero placeholders. Household members are referred to by role, never by name.
Real values live in GitHub secrets and reach the integration tier through the
environment — see `tests/integration/conftest.py`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any

# Placeholder identifiers. Structurally valid, deliberately not real.
PHONE_NUMBER_ID = "000000000000000"
WABA_ID = "000000000000000"
BUSINESS_DISPLAY_NUMBER = "15550100000"

# +1 555 01xx is reserved for fictional use, so these can never reach a real handset.
# Named by household role, not by person.
MEMBER_PRIMARY = "15550100001"
MEMBER_SECONDARY = "15550100002"
OUTSIDER = "15550100999"

PROFILE_NAME = "Test Member"


@dataclass
class SentMessage:
    to: str
    text: str
    ok: bool = True


class FakeWhatsAppClient:
    """Records sends. Numbers added via `block()` fail the way Meta's 130497 does."""

    def __init__(self) -> None:
        self.sent: list[SentMessage] = []
        self.read_receipts: list[str] = []
        self._blocked: dict[str, int] = {}
        self._downloads: dict[str, tuple[bytes, str]] = {}

    def block(self, number: str, error_code: int = 130497) -> FakeWhatsAppClient:
        """Model a permanently undeliverable recipient (e.g. a cross-border block)."""
        self._blocked[number] = error_code
        return self

    def stage_media(self, media_id: str, content: bytes, mime_type: str = "image/jpeg"):
        self._downloads[media_id] = (content, mime_type)
        return self

    # --- surface used by nano.whatsapp -------------------------------------

    def send_text(self, to: str, text: str) -> bool:
        ok = to not in self._blocked
        self.sent.append(SentMessage(to=to, text=text, ok=ok))
        return ok

    def mark_as_read(self, message_id: str) -> bool:
        self.read_receipts.append(message_id)
        return True

    def download_media(self, media_id: str) -> tuple[bytes, str]:
        if media_id not in self._downloads:
            raise KeyError(f"no media staged for {media_id}")
        return self._downloads[media_id]

    # --- assertion helpers --------------------------------------------------

    @property
    def recipients(self) -> list[str]:
        return [m.to for m in self.sent]

    @property
    def texts(self) -> list[str]:
        return [m.text for m in self.sent]

    def texts_to(self, number: str) -> list[str]:
        return [m.text for m in self.sent if m.to == number]

    def count_to(self, number: str) -> int:
        return sum(1 for m in self.sent if m.to == number)


# --- inbound payload builders ----------------------------------------------


def _envelope(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": WABA_ID, "changes": [{"field": "messages", "value": value}]}],
    }


def _metadata() -> dict[str, str]:
    return {
        "display_phone_number": BUSINESS_DISPLAY_NUMBER,
        "phone_number_id": PHONE_NUMBER_ID,
    }


def text_message(
    text: str = "add coffee",
    sender: str = MEMBER_PRIMARY,
    message_id: str = "wamid.TEST0001",
    timestamp: int | None = None,
) -> dict[str, Any]:
    return _envelope(
        {
            "messaging_product": "whatsapp",
            "metadata": _metadata(),
            "contacts": [{"profile": {"name": PROFILE_NAME}, "wa_id": sender}],
            "messages": [
                {
                    "from": sender,
                    "id": message_id,
                    "timestamp": str(timestamp or int(time.time())),
                    "type": "text",
                    "text": {"body": text},
                }
            ],
        }
    )


def image_message(
    media_id: str = "MEDIA_1",
    sender: str = MEMBER_PRIMARY,
    message_id: str = "wamid.TESTIMG1",
    mime_type: str = "image/jpeg",
) -> dict[str, Any]:
    return _envelope(
        {
            "messaging_product": "whatsapp",
            "metadata": _metadata(),
            "contacts": [{"profile": {"name": PROFILE_NAME}, "wa_id": sender}],
            "messages": [
                {
                    "from": sender,
                    "id": message_id,
                    "timestamp": str(int(time.time())),
                    "type": "image",
                    "image": {"id": media_id, "mime_type": mime_type, "sha256": "abc"},
                }
            ],
        }
    )


def unsupported_message(
    sender: str = MEMBER_PRIMARY,
    message_id: str = "wamid.TESTAUDIO1",
    msg_type: str = "audio",
) -> dict[str, Any]:
    return _envelope(
        {
            "messaging_product": "whatsapp",
            "metadata": _metadata(),
            "messages": [
                {
                    "from": sender,
                    "id": message_id,
                    "timestamp": str(int(time.time())),
                    "type": msg_type,
                    msg_type: {"id": "MEDIA_X", "mime_type": "audio/ogg"},
                }
            ],
        }
    )


def status_callback(message_id: str = "wamid.TEST0001", status: str = "delivered"):
    """Delivery receipts have no `messages` key. Nano gets several per real message."""
    return _envelope(
        {
            "messaging_product": "whatsapp",
            "metadata": _metadata(),
            "statuses": [
                {
                    "id": message_id,
                    "status": status,
                    "timestamp": str(int(time.time())),
                    "recipient_id": MEMBER_PRIMARY,
                }
            ],
        }
    )


def sign(body: bytes, app_secret: str) -> str:
    """Build the X-Hub-Signature-256 header Meta sends."""
    digest = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def signed(payload: dict[str, Any], app_secret: str) -> tuple[bytes, dict[str, str]]:
    """Serialize a payload and return (raw_body, headers) — signature over raw bytes."""
    body = json.dumps(payload).encode()
    return body, {
        "X-Hub-Signature-256": sign(body, app_secret),
        "Content-Type": "application/json",
    }


# --- receipt fixture --------------------------------------------------------

# Dates are fixed and fictional. The only property the tests depend on is that
# PURCHASE_DATE falls in the calendar month *before* QUERY_DATE — that is the
# shape that produced the "0.00 across 0 trips" report.
PURCHASE_DATE = "2024-05-15"
QUERY_DATE = "2024-06-03"


@dataclass
class ReceiptFixture:
    """A synthetic supermarket receipt with the properties the tests care about.

    Deliberately invented — no real purchase data appears here. What it reproduces
    from the real failing case is the *shape*:

    - abbreviated, non-English (Catalan) item names that plain `LIKE '%milk%'`
      matching can never hit, which is the canonicalization case
    - a non-grocery item on a grocery receipt, which is the `category` case
    - line totals that sum exactly to the stated total, so extraction can be checked
    - a purchase date in the month before the question is asked
    """

    store_name: str = "SUPERMERCAT EXEMPLE, S.L."
    purchase_date: str = PURCHASE_DATE
    currency: str = "EUR"
    total_amount: float = 21.70
    items: list[dict[str, Any]] = field(
        default_factory=lambda: [
            # "llet" is Catalan for milk — the cross-language matching case.
            {"item_name": "LLET SENCERA 1L", "quantity": 1, "unit_price": 1.15, "line_total": 1.15},
            {"item_name": "PA INTEGRAL", "quantity": 1, "unit_price": 1.40, "line_total": 1.40},
            {"item_name": "OUS M-12", "quantity": 1, "unit_price": 2.85, "line_total": 2.85},
            {"item_name": "POMA GOLDEN 1KG", "quantity": 1, "unit_price": 2.10, "line_total": 2.10},
            {
                "item_name": "IOGURT NATURAL P-4",
                "quantity": 1,
                "unit_price": 1.60,
                "line_total": 1.60,
            },
            {"item_name": "ARROS RODO 1KG", "quantity": 1, "unit_price": 1.95, "line_total": 1.95},
            {"item_name": "OLI OLIVA 1L", "quantity": 1, "unit_price": 6.45, "line_total": 6.45},
            # Not food — the reason purchase_items needs a category column.
            {"item_name": "PILES AAA P-4", "quantity": 1, "unit_price": 4.20, "line_total": 4.20},
        ]
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "store_name": self.store_name,
            "purchase_date": self.purchase_date,
            "currency": self.currency,
            "total_amount": self.total_amount,
            "items": self.items,
        }

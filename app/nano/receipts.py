"""Receipt handling: archiving the image, and sanity-checking what Gemini read.

**Archival.** `receipt_image_uri` has been written as `""` for every purchase
ever recorded, so there has never been a way to check an extraction against the
original, or to reprocess receipts after improving the prompt. WhatsApp media
expires, so the image has to be copied somewhere durable at ingest or it is gone.

**Date sanity.** The extracted date is the field a spend query depends on most
and the one a model is most likely to get wrong, because supermarket receipts
print `DD/MM/YYYY` in most of Europe and `MM/DD/YYYY` in the US. The check here
is deliberately conservative: it only rewrites a date that is impossible or
implausible, and it never invents precision it does not have.
"""

from __future__ import annotations

import datetime
import logging
import re

from nano.clients import storage_client
from nano.config import Settings, get_settings
from nano.logging_setup import log_with

logger = logging.getLogger(__name__)

# A receipt older than this is more likely a misread than a genuinely ancient
# shop. Six months is generous: people do photograph old receipts.

_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/heic": "heic",
    "application/pdf": "pdf",
}

_ISO_DATE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")


def archive_receipt(
    image_bytes: bytes,
    mime_type: str,
    purchase_id: str,
    purchased_on: datetime.date,
    settings: Settings | None = None,
) -> str:
    """Copy the receipt image to Cloud Storage. Returns its URI, or "".

    Best-effort: a storage failure must not lose the purchase itself, which is
    the part that carries the money. Laid out by year and month so a human can
    browse the bucket and find a trip.
    """
    settings = settings or get_settings()
    if not settings.receipts_bucket:
        logger.warning("no RECEIPTS_BUCKET configured; receipt image not archived")
        return ""

    extension = _EXTENSIONS.get(mime_type.lower(), "bin")
    blob_path = (
        f"receipts/{purchased_on.year:04d}/{purchased_on.month:02d}/{purchase_id}.{extension}"
    )

    try:
        bucket = storage_client().bucket(settings.receipts_bucket)
        blob = bucket.blob(blob_path)
        blob.upload_from_string(image_bytes, content_type=mime_type)
    except Exception as exc:
        log_with(
            logger,
            logging.ERROR,
            "could not archive the receipt image",
            purchase_id=purchase_id,
            error=str(exc),
        )
        return ""

    uri = f"gs://{settings.receipts_bucket}/{blob_path}"
    log_with(
        logger,
        logging.INFO,
        "archived receipt image",
        purchase_id=purchase_id,
        uri=uri,
        bytes=len(image_bytes),
    )
    return uri


def sanitise_purchase_date(
    raw: str,
    today: datetime.date | None = None,
) -> tuple[datetime.date, str]:
    """Turn the model's date string into a plausible date.

    Returns ``(date, note)`` where note explains any correction, or "" if the
    value was taken as-is. Rules, in order:

    1. Unparseable or missing -> today.
    2. In the future -> try swapping day and month; otherwise today.
    3. Anything else, however old -> keep exactly what was printed.

    The swap is only ever *tried*; it is accepted only when it produces a date
    that is itself plausible. A date like 2026-08-29 has no valid swap (there is
    no month 29), so an unambiguous date can never be mangled by this.

    Rule 3 used to attempt the same swap on any receipt older than six months.
    That was wrong, and it only became obvious with a backfill in prospect: a
    receipt from 8 January, read on 10 September, is 245 days old and swaps to a
    perfectly plausible 1 August, so it was stored seven months away from when
    the shopping actually happened — silently, with the note going only to the
    logs.

    The asymmetry is the whole point. A date in the future is *impossible*, so
    something must be wrong with it and a swap can only improve matters. A date
    in the past is merely unusual, and "unusual" is not evidence: a person
    photographing last winter's invoices is doing something completely normal,
    and second-guessing them corrupts the very history they are building.
    """
    today = today or datetime.date.today()

    parsed = _parse_iso(raw)
    if parsed is None:
        return today, f"unparseable date {raw!r}; used today"

    if parsed > today:
        swapped = _swap_day_month(parsed)
        if swapped and _is_plausible(swapped, today):
            return swapped, f"date {parsed.isoformat()} was in the future; read as {swapped}"
        return today, f"date {parsed.isoformat()} was in the future; used today"

    return parsed, ""


def _parse_iso(raw: str) -> datetime.date | None:
    if not raw:
        return None
    match = _ISO_DATE.search(str(raw).strip())
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    try:
        return datetime.date(year, month, day)
    except ValueError:
        return None


def _swap_day_month(value: datetime.date) -> datetime.date | None:
    """Reinterpret a DD/MM value that was read as MM/DD, when that is possible."""
    try:
        return datetime.date(value.year, value.day, value.month)
    except ValueError:
        return None


def _is_plausible(value: datetime.date, today: datetime.date) -> bool:
    """Could this date be when a shop actually happened?

    Only one thing rules a date out: it has not happened yet. Age used to count
    against a date here too, which made the future-date repair refuse its own
    correct answer — a receipt printed 2026-12-03, read in September, swaps to
    12 March, and that was rejected for being more than six months old, falling
    back to "today" and inventing a date further from the truth than the one it
    was fixing. An old date is a fact about the receipt, not a reason to doubt it.
    """
    return value <= today


def normalise_currency(raw: str, settings: Settings | None = None) -> str:
    """Coerce whatever the model produced into a 3-letter code."""
    settings = settings or get_settings()
    candidate = (raw or "").strip().upper()

    symbols = {"€": "EUR", "$": "USD", "£": "GBP", "R$": "BRL"}
    if candidate in symbols:
        return symbols[candidate]

    if len(candidate) == 3 and candidate.isalpha():
        return candidate

    return settings.default_currency

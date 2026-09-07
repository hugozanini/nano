"""Turning an inbound message into actions and replies.

This runs off the request path, inside a Cloud Tasks callback or a background
task, so it is allowed to be slow. Everything here is deliberately synchronous:
FastAPI runs sync endpoint functions in a worker thread, so blocking calls never
touch the event loop.

Reply routing follows the shape of the action. A change to shared state goes to
everyone, because that is the point of a shared list. A question is answered to
whoever asked, so "what's on the list?" stops buzzing both phones.
"""

from __future__ import annotations

import logging
from typing import Any

from nano import events, gemini, history, purchases, receipts, store, whatsapp
from nano.config import Settings, get_settings
from nano.logging_setup import bind_request, log_with, redact_number
from nano.whatsapp import InboundMessage

logger = logging.getLogger(__name__)

UNSUPPORTED_REPLY = "I can only read text and receipt photos for now."
RECEIPT_FAILED_REPLY = "I couldn't read that receipt clearly — could you try a clearer photo?"


def format_list(items: list[str]) -> str:
    if not items:
        return "The list is empty right now \U0001f389"
    bullets = "\n".join(f"- {item}" for item in items)
    return f"Here's the current list ({len(items)} items):\n{bullets}"


def _broadcast(text: str, settings: Settings) -> None:
    """Send to the whole household; a failed recipient never affects the others."""
    results = whatsapp.notify(settings.household_numbers, text, settings)
    failed = [number for number, ok in results.items() if not ok]
    if failed:
        log_with(
            logger,
            logging.WARNING,
            "some household recipients could not be reached",
            failed=[redact_number(n) for n in failed],
            delivered=len(results) - len(failed),
        )


def _reply(sender: str, text: str, settings: Settings) -> None:
    whatsapp.send_text(sender, text, settings)


def handle_message(message: InboundMessage, settings: Settings | None = None) -> str:
    """Process one message end to end. Returns the intent, for logging."""
    settings = settings or get_settings()

    if message.is_image:
        _handle_receipt(message, settings)
        return "log_receipt"

    if not message.is_text:
        _reply(message.sender, UNSUPPORTED_REPLY, settings)
        return "unsupported"

    return _handle_text(message, settings)


def _handle_text(message: InboundMessage, settings: Settings) -> str:
    intent, args = gemini.classify_message(message.text)
    sender_name = settings.name_for(message.sender)
    log_with(logger, logging.INFO, "classified message", intent=intent, sender=sender_name)

    if intent == "add_items":
        requested = [str(i) for i in (args.get("items") or [])]
        items = store.add_items(requested, settings)
        events.record(events.ADD_ITEM, requested, sender_name, settings, message.id)
        _broadcast(f"Added! {format_list(items)}", settings)

    elif intent == "remove_items":
        requested = [str(i) for i in (args.get("items") or [])]
        items = store.remove_items(requested, settings)
        events.record(events.REMOVE_ITEM, requested, sender_name, settings, message.id)
        _broadcast(f"Removed. {format_list(items)}", settings)

    elif intent == "close_list":
        cleared = store.get_items(settings)
        store.close_list(settings)
        events.record(events.CLOSE_LIST, cleared, sender_name, settings, message.id)
        _broadcast("List cleared — happy shopping! 🛒", settings)

    elif intent == "query_history":
        answer = history.answer_history_query(
            args.get("query_type"),
            args.get("period", "this_month"),
            args.get("item_name"),
            settings,
        )
        _reply(message.sender, answer, settings)

    else:
        _reply(message.sender, format_list(store.get_items(settings)), settings)

    return intent


def _handle_receipt(message: InboundMessage, settings: Settings) -> None:
    sender_name = settings.name_for(message.sender)

    try:
        image_bytes, mime_type = whatsapp.download_media(message.media_id, settings)
        extracted = gemini.extract_receipt(image_bytes, mime_type)
    except Exception as exc:
        log_with(logger, logging.ERROR, "receipt extraction failed", error=str(exc))
        _reply(message.sender, RECEIPT_FAILED_REPLY, settings)
        return

    purchased_on, note = receipts.sanitise_purchase_date(
        str(extracted.get("purchase_date", "")),
        today=history.today_in_household_tz(settings),
    )
    if note:
        log_with(logger, logging.WARNING, "adjusted the receipt date", detail=note)

    currency = receipts.normalise_currency(str(extracted.get("currency", "")), settings)
    store_name = str(extracted.get("store_name") or "unknown")
    items: list[dict[str, Any]] = list(extracted.get("items") or [])

    purchase_id = purchases.purchase_id_for(message.id)
    receipt_uri = receipts.archive_receipt(
        image_bytes, mime_type, purchase_id, purchased_on, settings
    )

    try:
        purchase_id, written = purchases.log_purchase(
            message_id=message.id,
            store_name=store_name,
            purchase_date=purchased_on.isoformat(),
            currency=currency,
            total_amount=extracted.get("total_amount", 0),
            items=items,
            receipt_uri=receipt_uri,
            settings=settings,
        )
    except Exception as exc:
        log_with(logger, logging.ERROR, "could not record purchase", error=str(exc))
        _reply(message.sender, RECEIPT_FAILED_REPLY, settings)
        return

    if not written:
        # A retry that got past both dedupe layers. Stay silent rather than
        # confirming the same receipt twice.
        log_with(
            logger,
            logging.INFO,
            "receipt already logged; suppressing duplicate confirmation",
            purchase_id=purchase_id,
        )
        return

    item_names = [str(item.get("item_name", "")) for item in items]
    remaining = store.clear_matched_items(item_names, settings)
    events.record(events.PURCHASE_CLEARED, item_names, sender_name, settings, message.id)

    total = extracted.get("total_amount", 0)
    _broadcast(
        f"Logged your receipt from {store_name} ({currency} {total}) "
        f"on {purchased_on:%-d %b}. {format_list(remaining)}",
        settings,
    )


def process_payload(payload: dict[str, Any], settings: Settings | None = None) -> str:
    """Entry point for both task backends: claim, then handle.

    The Firestore claim sits here rather than in the webhook so that Cloud Tasks'
    own name-based dedupe and this one are independent layers, and so that a
    task retried by Cloud Tasks after a genuine failure is still deduped.
    """
    settings = settings or get_settings()
    message = whatsapp.parse_inbound(payload)
    if message is None:
        return "ignored"

    bind_request(message_id=message.id)

    if not store.claim_message(message.id, settings):
        return "duplicate"

    try:
        whatsapp.mark_as_read(message.id, settings)
        intent = handle_message(message, settings)
        log_with(logger, logging.INFO, "message handled", intent=intent)
        return intent
    except Exception:
        # Release the claim so a Cloud Tasks retry can have another go; without
        # this an unexpected failure would swallow the message permanently.
        store.release_claim(message.id, settings)
        logger.exception("unhandled error while processing message")
        raise

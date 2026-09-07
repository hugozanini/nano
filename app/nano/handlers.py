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

from nano import canonicalise, events, gemini, history, purchases, receipts, store, whatsapp
from nano.config import Settings, get_settings
from nano.logging_setup import bind_request, log_with, redact_number
from nano.whatsapp import InboundMessage

logger = logging.getLogger(__name__)

UNSUPPORTED_REPLY = "I can only read text and receipt photos for now."
RECEIPT_FAILED_REPLY = "I couldn't read that receipt clearly — could you try a clearer photo?"

HELP_REPLY = """I'm Nano, your shared grocery list \U0001f6d2

*The list*
- "we're out of coffee" or "add tomatoes and basil" — I'll add them
- "what's on the list?" — I'll show it
- "remove milk" — I'll take it off
- "clear the list" — after a shop (I'll check first)
- "undo" — reverses my last change

*Receipts*
Send me a photo and I'll log the store, date and every line item, then tick off
whatever you'd already listed.

*Spending*
- "how much did we spend this month?"
- "what did we buy?"
- "when did we last buy olive oil?"
- "how much do we spend on milk?"

Changes go to both of you; answers to questions come back just to whoever asked."""

# Checked before the model sees the message, but only while a confirmation is
# outstanding. "yes" is unambiguous in that moment and does not need an LLM.
AFFIRMATIVE = {
    "yes",
    "y",
    "yeah",
    "yep",
    "yup",
    "ok",
    "okay",
    "sure",
    "confirm",
    "confirmed",
    "do it",
    "go ahead",
    "please",
    "si",
    "sí",
    "sim",
    "vale",
    "dale",
}
NEGATIVE = {
    "no",
    "n",
    "nope",
    "nah",
    "cancel",
    "stop",
    "don't",
    "dont",
    "wait",
    "nao",
    "não",
    "no gracias",
}


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
    sender_name = settings.name_for(message.sender)
    text = message.text.strip()

    # A confirmation only means "yes" while something is actually pending, so
    # this is checked first and only then.
    pending = store.take_pending(message.sender, settings)
    if pending:
        resolved = _resolve_pending(message, text, pending, sender_name, settings)
        if resolved is not None:
            return resolved

    conversation = store.get_conversation(message.sender, settings)
    current = store.get_items(settings)

    intent, args = gemini.classify_message(
        text,
        turns=conversation.get("turns"),
        current_items=current,
    )
    log_with(logger, logging.INFO, "classified message", intent=intent, sender=sender_name)

    reply = _apply_intent(intent, args, message, current, sender_name, settings)
    store.record_turn(message.sender, text, intent, settings, reply)
    return intent


def _resolve_pending(
    message: InboundMessage,
    text: str,
    pending: dict[str, Any],
    sender_name: str,
    settings: Settings,
) -> str | None:
    """Handle a yes/no answer to an outstanding confirmation.

    Returns the intent when the message was an answer, or None to let it be
    classified normally — someone may simply have changed the subject.
    """
    answer = text.lower().strip(" .!?")

    if answer in AFFIRMATIVE and pending.get("action") == "close_list":
        previous = list(pending.get("items") or [])
        store.close_list(settings)
        store.record_action(message.sender, "close_list", [], previous, settings)
        events.record(events.CLOSE_LIST, previous, sender_name, settings, message.id)
        _broadcast("List cleared — happy shopping! \U0001f6d2", settings)
        return "close_list"

    if answer in NEGATIVE:
        _reply(message.sender, "Left the list as it is.", settings)
        return "cancelled"

    return None


def _apply_intent(
    intent: str,
    args: dict[str, Any],
    message: InboundMessage,
    current: list[str],
    sender_name: str,
    settings: Settings,
) -> str:
    """Carry out one classified intent and return the reply that was sent."""
    if intent == "add_items":
        requested = [str(i) for i in (args.get("items") or [])]
        items = store.add_items(requested, settings)
        events.record(events.ADD_ITEM, requested, sender_name, settings, message.id)
        store.record_action(message.sender, "add_items", requested, current, settings)
        reply = f"Added! {format_list(items)}"
        _broadcast(reply, settings)
        return reply

    if intent == "remove_items":
        requested = [str(i) for i in (args.get("items") or [])]
        items = store.remove_items(requested, settings)
        events.record(events.REMOVE_ITEM, requested, sender_name, settings, message.id)
        store.record_action(message.sender, "remove_items", requested, current, settings)
        reply = f"Removed. {format_list(items)}"
        _broadcast(reply, settings)
        return reply

    if intent == "close_list":
        # Destructive and easy to say by accident, so confirm when there is
        # something to lose.
        if not current:
            reply = "The list is already empty \U0001f389"
            _reply(message.sender, reply, settings)
            return reply
        store.set_pending(message.sender, "close_list", current, settings)
        reply = (
            f"That'll clear all {len(current)} items "
            f"({', '.join(current[:3])}{'…' if len(current) > 3 else ''}). "
            "Reply 'yes' to confirm."
        )
        _reply(message.sender, reply, settings)
        return reply

    if intent == "undo":
        reply = _undo(message.sender, sender_name, message.id, settings)
        _broadcast(reply, settings)
        return reply

    if intent == "help":
        _reply(message.sender, HELP_REPLY, settings)
        return HELP_REPLY

    if intent == "query_history":
        raw_item = args.get("item_name")
        item = canonicalise.canonicalise_query(raw_item) if raw_item else None
        reply = history.answer_history_query(
            args.get("query_type"),
            args.get("period", "this_month"),
            item or raw_item,
            settings,
            display_name=raw_item,
        )
        _reply(message.sender, reply, settings)
        return reply

    reply = format_list(current)
    _reply(message.sender, reply, settings)
    return reply


def _undo(sender: str, sender_name: str, message_id: str, settings: Settings) -> str:
    """Restore the list to what it was before the last change.

    Restoring the whole previous list, rather than inverting the operation, is
    deliberate: it is correct even when the original change partly no-opped, for
    instance adding an item that was already there.
    """
    action = store.take_last_action(sender, settings)
    if not action:
        return "There's nothing for me to undo."

    previous = list(action.get("previous") or [])
    store.replace_items(previous, settings)
    events.record(events.REMOVE_ITEM, list(action.get("items") or []), sender_name, settings, "")

    verb = {
        "add_items": "adding",
        "remove_items": "removing",
        "close_list": "clearing the list",
    }.get(str(action.get("action")), "that")
    return f"Undone — reversed {verb}. {format_list(previous)}"


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

    # One batched call maps every line to an English term plus a category, so
    # "how much on apples?" can reach a row printed as POMA GOLDEN 1KG.
    mapped = canonicalise.canonicalise_items([str(i.get("item_name", "")) for i in items])
    for item, canon in zip(items, mapped, strict=False):
        item["normalized_name"] = canon["canonical"]
        item["category"] = canon["category"]

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

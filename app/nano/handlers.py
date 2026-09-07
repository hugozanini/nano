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

from nano import (
    canonicalise,
    events,
    fx,
    gemini,
    history,
    locale,
    purchases,
    question_log,
    receipts,
    store,
    stores,
    whatsapp,
)
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
        # A photo can carry a caption. Answer it once the receipt is in, so
        # "what did we buy?" attached to a receipt works in one message.
        if message.text.strip():
            log_with(logger, logging.INFO, "handling the caption on an image")
            _handle_text(message, settings)
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

    if intent == "cannot_answer":
        # The most direct product signal there is: someone wanted something and
        # did not get it. Recorded so the backlog can be built from real demand.
        with question_log.track(message.text, sender_name, settings) as entry:
            entry.resolution = question_log.UNANSWERABLE
            entry.answered = False
            entry.failure_reason = str(args.get("request") or "")
        reply = _cannot_answer_reply(str(args.get("request") or ""))
        _reply(message.sender, reply, settings)
        return reply

    if intent == "request_feature":
        reply = _feature_request_reply(str(args.get("request") or ""))
        _reply(message.sender, reply, settings)
        return reply

    if intent == "unclear":
        reply = (
            "Sorry, I didn't follow that. You can add things to the list, send me a "
            "receipt photo, or ask about past spending."
        )
        _reply(message.sender, reply, settings)
        return reply

    if intent == "query_history":
        with question_log.track(message.text, sender_name, settings) as entry:
            raw_item = args.get("item_name")
            item = canonicalise.canonicalise_query(raw_item) if raw_item else None
            reply = history.answer_history_query(
                args.get("query_type"),
                args.get("period", "this_month"),
                item or raw_item,
                settings,
                display_name=raw_item,
            )
            entry.resolution = question_log.TEMPLATE
            entry.template_name = str(args.get("query_type") or "unknown")
            entry.answered = True
        _reply(message.sender, reply, settings)
        return reply

    reply = format_list(current)
    _reply(message.sender, reply, settings)
    return reply


def _describe(request: str) -> str:
    """Tidy the model's phrasing for use mid-sentence.

    Deliberately minimal. An earlier version stripped leading verbs like "know"
    and "find out" to fit the template "I can't {x} yet" — which removed the
    very word that made the sentence grammatical and produced "I can't the main
    place we do groceries yet". The template was the problem, not the phrasing,
    so the template now accepts any phrase and this only trims punctuation.
    """
    return (request or "").strip().rstrip(".?!,;")


def _cannot_answer_reply(request: str) -> str:
    """Say what cannot be done, about the thing that was actually asked.

    The failure this replaces: a real question was answered with a list of
    capabilities, which does not even acknowledge what was asked. Naming the
    request back is the difference between a brush-off and an answer.
    """
    described = _describe(request)
    if described:
        # "you're asking about X" accepts whatever phrasing the model returns —
        # noun phrase, question fragment, or a verb-led one if it slips — and
        # stays grammatical. The previous template was "I can't {X} yet", which
        # needed X to be a verb phrase and produced "I can't the main place we
        # do groceries yet".
        return (
            f"I can't work that out yet — you're asking about {described}, and that's "
            "beyond what I can get from the receipts I've logged so far. I've noted it "
            "as a feature request, so it can be added later."
        )
    return "I can't do that yet, but I've noted it as a feature request so it can be added later."


def _feature_request_reply(request: str) -> str:
    described = _describe(request)
    if described:
        return f'Noted — I\'ve added "{described}" to the backlog as a feature request.'
    return "Noted — I've added that to the backlog as a feature request."


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


FORCE_PHRASES = {"log it anyway", "log anyway", "yes log it", "different trip"}


def _handle_receipt(message: InboundMessage, settings: Settings) -> None:
    sender_name = settings.name_for(message.sender)
    force = message.text.strip().lower() in FORCE_PHRASES

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

    # The model reports what it saw; this decides what to believe. A tax table
    # of 4/10/21 outranks "the text looks Catalan", and a city is only recorded
    # when it was actually printed.
    where = locale.infer_location(
        language=str(extracted.get("receipt_language", "")),
        currency=currency,
        tax_rates=extracted.get("tax_rates"),
        tax_id=str(extracted.get("tax_id", "")),
        printed_city=str(extracted.get("store_city", "")),
        model_country=str(extracted.get("country_guess", "")),
    )
    log_with(
        logger,
        logging.INFO,
        "inferred receipt origin",
        country=where.country_code,
        region=where.region,
        confidence=where.confidence,
        evidence=where.evidence_text,
    )

    store_name = str(extracted.get("store_name") or "unknown")
    items: list[dict[str, Any]] = list(extracted.get("items") or [])

    # Resolve the shop once and reuse it. Repeat visits cost no model call, and
    # the same shop always lands on the same dimension row, so a GROUP BY does
    # not split one supermarket across several.
    store_id = stores.resolve(
        store_name=store_name,
        location=where,
        address=str(extracted.get("store_address", "")),
        tax_id=str(extracted.get("tax_id", "")),
        settings=settings,
    )

    # One batched call maps every line to an English term plus a category, so
    # "how much on apples?" can reach a row printed as POMA GOLDEN 1KG.
    mapped = canonicalise.canonicalise_items([str(i.get("item_name", "")) for i in items])
    for item, canon in zip(items, mapped, strict=False):
        item["normalized_name"] = canon["canonical"]
        item["category"] = canon["category"]
        item["item_name_en"] = canon.get("name_en", "")

    # One comparable money column, at the rate that applied on the purchase
    # date rather than today's, so last year's totals still reconcile.
    total_eur, rate, rate_date = fx.to_base(
        extracted.get("total_amount", 0), currency, purchased_on, settings
    )
    if rate is not None:
        for item in items:
            line_eur, _, _ = fx.to_base(item.get("line_total", 0), currency, purchased_on, settings)
            item["line_total_eur"] = line_eur
        log_with(
            logger,
            logging.INFO,
            "converted purchase to EUR",
            currency=currency,
            rate=str(rate),
            rate_date=str(rate_date),
        )
    else:
        log_with(
            logger,
            logging.WARNING,
            "no exchange rate available; EUR columns left empty",
            currency=currency,
        )

    # A person re-photographing the same receipt is a different message, so
    # message-id idempotency does not catch it, and the spend silently doubles.
    already = purchases.find_matching_purchase(
        store_name, purchased_on.isoformat(), extracted.get("total_amount", 0), settings
    )
    if already and not force and already != purchases.purchase_id_for(message.id):
        total = extracted.get("total_amount", 0)
        _reply(
            message.sender,
            f"I've already logged a receipt from {store_name} on {purchased_on:%-d %b} "
            f"for {currency} {total}, so I haven't logged this one again — it would "
            f"have doubled your total. Send it with the caption 'log it anyway' if "
            f"this really was a separate trip.",
            settings,
        )
        log_with(
            logger,
            logging.INFO,
            "skipped a receipt matching an existing purchase",
            existing=already,
        )
        return

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
            location=where,
            store_id=store_id,
            store_address=str(extracted.get("store_address", "")),
            tax_id=str(extracted.get("tax_id", "")),
            tax_rates=locale.parse_rates(extracted.get("tax_rates")),
            amount_eur=total_eur,
            fx_rate=rate,
            fx_rate_date=rate_date,
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

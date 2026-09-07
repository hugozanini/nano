"""Firestore: the live shopping list and the message-deduplication ledger.

`claim_message` is the important one. Meta retries a webhook it considers failed,
and in production it retried the same receipt seven times over 45 minutes, each
retry writing a full duplicate purchase. `create()` on a document keyed by the
WhatsApp message id is atomic and fails if the key already exists, so exactly one
delivery of any message can win — however many arrive, in parallel or an hour
apart.

List mutations run inside a transaction. The previous implementation read the
whole document, edited it in Python and wrote it back, so two people adding items
at the same moment silently lost one.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any

from nano.clients import firestore_client
from nano.config import Settings, get_settings
from nano.logging_setup import log_with

logger = logging.getLogger(__name__)

LIST_DOC_ID = "shared_list"

# How long a processed-message record is kept. Meta's retries taper off well
# inside a day; a week is a comfortable margin. A Firestore TTL policy on
# `expires_at` reclaims them — see scripts/setup_firestore_ttl.sh.
CLAIM_TTL = datetime.timedelta(days=7)


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _lists(settings: Settings):
    return firestore_client().collection(settings.collection("lists"))


def _claims(settings: Settings):
    return firestore_client().collection(settings.collection("processed_messages"))


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------


def claim_message(message_id: str, settings: Settings | None = None) -> bool:
    """Try to claim a message for processing.

    Returns True exactly once per message id; every later call returns False,
    including from a concurrent request, because `create()` is atomic and raises
    AlreadyExists on conflict.

    On an unexpected Firestore error this returns True: processing a message
    twice is a smaller failure than dropping it silently, and the deterministic
    purchase id downstream still prevents duplicate rows.
    """
    from google.api_core import exceptions

    settings = settings or get_settings()
    if not message_id:
        return True

    try:
        _claims(settings).document(message_id).create(
            {"claimed_at": _now(), "expires_at": _now() + CLAIM_TTL}
        )
        return True
    except exceptions.AlreadyExists:
        log_with(logger, logging.INFO, "duplicate delivery ignored", message_id=message_id)
        return False
    except Exception as exc:  # pragma: no cover - defensive
        log_with(
            logger,
            logging.ERROR,
            "could not claim message; processing anyway",
            message_id=message_id,
            error=str(exc),
        )
        return True


def release_claim(message_id: str, settings: Settings | None = None) -> None:
    """Drop a claim so a message can be retried.

    Called when processing fails before anything durable happened, so a
    transient error does not permanently swallow the message.
    """
    settings = settings or get_settings()
    try:
        _claims(settings).document(message_id).delete()
    except Exception as exc:  # pragma: no cover - defensive
        log_with(
            logger,
            logging.WARNING,
            "could not release claim",
            message_id=message_id,
            error=str(exc),
        )


# --------------------------------------------------------------------------
# The shopping list
# --------------------------------------------------------------------------


def _normalise(raw: Any) -> list[str]:
    """Read stored items, tolerating both the plain-string and dict shapes."""
    if not raw:
        return []
    items: list[str] = []
    for entry in raw:
        if isinstance(entry, str):
            items.append(entry)
        elif isinstance(entry, dict) and entry.get("name"):
            items.append(str(entry["name"]))
    return items


def get_items(settings: Settings | None = None) -> list[str]:
    settings = settings or get_settings()
    snapshot = _lists(settings).document(LIST_DOC_ID).get()
    if not snapshot.exists:
        return []
    return _normalise((snapshot.to_dict() or {}).get("items"))


def _run_in_transaction(client, fn):
    """Run ``fn(transaction)`` inside a real Firestore transaction.

    The obvious-looking ``with client.transaction():`` form does **not** work:
    ``Transaction.__enter__`` is just ``return self`` and never calls
    ``_begin()``, so the first ``get(transaction=...)`` fails with
    "Transaction not in progress". Only ``firestore.transactional`` begins,
    commits, and retries on contention.

    An integration test against real Firestore caught this; every unit test had
    passed, because the fake had been written to match the wrong assumption.
    The isinstance check keeps that fake usable without teaching it the
    library's private transaction protocol.
    """
    transaction = client.transaction()

    try:
        from google.cloud import firestore
        from google.cloud.firestore_v1.transaction import Transaction as RealTransaction
    except ImportError:  # pragma: no cover - google libs always present in prod
        return transaction(fn)

    if isinstance(transaction, RealTransaction):
        return firestore.transactional(fn)(transaction)
    return transaction(fn)


def _mutate(settings: Settings, change) -> list[str]:
    """Apply ``change(current_items) -> new_items`` atomically."""
    doc_ref = _lists(settings).document(LIST_DOC_ID)

    def apply(transaction) -> list[str]:
        snapshot = doc_ref.get(transaction=transaction)
        current = _normalise((snapshot.to_dict() or {}).get("items")) if snapshot.exists else []
        updated = change(current)
        transaction.set(doc_ref, {"items": updated, "updated_at": _now()})
        return updated

    return _run_in_transaction(firestore_client(), apply)


def add_items(new_items: list[str], settings: Settings | None = None) -> list[str]:
    """Append items, skipping case-insensitive duplicates."""
    settings = settings or get_settings()

    def change(current: list[str]) -> list[str]:
        seen = {item.lower() for item in current}
        additions = []
        for item in new_items:
            cleaned = (item or "").strip()
            if cleaned and cleaned.lower() not in seen:
                additions.append(cleaned)
                seen.add(cleaned.lower())
        return current + additions

    return _mutate(settings, change)


def remove_items(targets: list[str], settings: Settings | None = None) -> list[str]:
    settings = settings or get_settings()
    lowered = {t.strip().lower() for t in targets if t and t.strip()}

    def change(current: list[str]) -> list[str]:
        return [item for item in current if item.lower() not in lowered]

    return _mutate(settings, change)


def close_list(settings: Settings | None = None) -> list[str]:
    settings = settings or get_settings()
    return _mutate(settings, lambda _current: [])


def replace_items(items: list[str], settings: Settings | None = None) -> list[str]:
    """Set the list to exactly these items.

    Used by undo, which restores the whole previous list rather than inverting
    the last operation — correct even when the original change partly no-opped,
    such as adding an item that was already there.
    """
    settings = settings or get_settings()
    return _mutate(settings, lambda _current: list(items))


def clear_matched_items(purchased: list[str], settings: Settings | None = None) -> list[str]:
    """Remove list entries that a receipt shows were bought.

    Matching is whole-word rather than substring. The previous bidirectional
    `in` test meant a purchased "graham crackers" cleared "ham" off the list.
    Cross-language matching arrives with item canonicalisation; this only fixes
    the false positives.
    """
    settings = settings or get_settings()
    purchased_tokens = [set(_tokenise(name)) for name in purchased if name]

    def change(current: list[str]) -> list[str]:
        remaining = []
        for entry in current:
            tokens = set(_tokenise(entry))
            if tokens and any(tokens & bought for bought in purchased_tokens):
                continue
            remaining.append(entry)
        return remaining

    return _mutate(settings, change)


def _tokenise(value: str) -> list[str]:
    return [
        token
        for token in "".join(ch.lower() if ch.isalnum() else " " for ch in value).split()
        if len(token) > 2
    ]


# --------------------------------------------------------------------------
# Conversation memory
# --------------------------------------------------------------------------

# How many exchanges to carry into the next classification. Enough for a
# correction to refer back to what it is correcting, short enough that the
# prompt stays cheap and the model is not tempted to re-answer old messages.
MEMORY_TURNS = 6

# A pending confirmation older than this is stale — answering "yes" twenty
# minutes later almost certainly means something else.
PENDING_TTL = datetime.timedelta(minutes=10)


def _conversations(settings: Settings):
    return firestore_client().collection(settings.collection("conversations"))


def get_conversation(sender: str, settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or get_settings()
    try:
        snapshot = _conversations(settings).document(sender).get()
    except Exception as exc:  # pragma: no cover - defensive
        log_with(logger, logging.WARNING, "could not read conversation", error=str(exc))
        return {}
    return (snapshot.to_dict() or {}) if snapshot.exists else {}


def _save_conversation(sender: str, data: dict[str, Any], settings: Settings) -> None:
    try:
        _conversations(settings).document(sender).set(data)
    except Exception as exc:  # pragma: no cover - defensive
        log_with(logger, logging.WARNING, "could not save conversation", error=str(exc))


def record_turn(
    sender: str,
    text: str,
    intent: str,
    settings: Settings | None = None,
    reply: str = "",
    topic: str = "",
    by: str = "",
) -> None:
    """Append an exchange to one person's memory.

    `reply` is stored because the next message often refers to it — "the list"
    after Nano has printed a list of purchases means that list. `topic` records
    the subject the exchange left the conversation on, so a bare referent has
    something to bind to.
    """
    settings = settings or get_settings()
    conversation = get_conversation(sender, settings)
    turns = list(conversation.get("turns") or [])
    turns.append(
        {
            "text": text[:400],
            "intent": intent,
            "reply": reply[:400],
            "by": by,
            "at": _now(),
        }
    )
    conversation["turns"] = turns[-MEMORY_TURNS:]
    if topic:
        conversation["last_topic"] = topic
    _save_conversation(sender, conversation, settings)


def record_shared_turn(
    text: str,
    intent: str,
    reply: str,
    actor: str,
    settings: Settings | None = None,
    topic: str = "",
) -> None:
    """Record an exchange in every household member's memory.

    Only for changes that were broadcast to both phones. Both people saw the
    message arrive, so "remove that" should resolve against it whichever of them
    says it — memory should match what each person can actually see.

    Answers to questions deliberately do not come through here: they go only to
    whoever asked, so the other person never saw them and must not be able to
    refer back to something invisible to them.
    """
    settings = settings or get_settings()
    for number in settings.household_numbers:
        record_turn(number, text, intent, settings, reply, topic=topic, by=actor)


def last_topic(sender: str, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    return str(get_conversation(sender, settings).get("last_topic") or "")


def record_action(
    sender: str,
    action: str,
    items: list[str],
    previous: list[str],
    settings: Settings | None = None,
) -> None:
    """Remember the last reversible action, and what to restore to undo it."""
    settings = settings or get_settings()
    conversation = get_conversation(sender, settings)
    conversation["last_action"] = {
        "action": action,
        "items": items,
        "previous": previous,
        "at": _now(),
    }
    _save_conversation(sender, conversation, settings)


def take_last_action(sender: str, settings: Settings | None = None) -> dict[str, Any] | None:
    """Read and clear the last action, so an undo cannot be applied twice."""
    settings = settings or get_settings()
    conversation = get_conversation(sender, settings)
    action = conversation.get("last_action")
    if not action:
        return None
    conversation.pop("last_action", None)
    _save_conversation(sender, conversation, settings)
    return action


def set_pending(
    sender: str, kind: str, payload: dict[str, Any] | None = None, settings: Settings | None = None
) -> None:
    """Park a question Nano is waiting on an answer to.

    `kind` says what sort of answer would resolve it — a close-list confirmation
    wants yes or no, a clarification wants one of its options — and `payload`
    carries whatever that resolution needs.
    """
    settings = settings or get_settings()
    conversation = get_conversation(sender, settings)
    conversation["pending"] = {"kind": kind, "payload": payload or {}, "at": _now()}
    _save_conversation(sender, conversation, settings)


def take_pending(sender: str, settings: Settings | None = None) -> dict[str, Any] | None:
    """Read and clear a pending question, ignoring stale ones."""
    settings = settings or get_settings()
    conversation = get_conversation(sender, settings)
    pending = conversation.get("pending")
    if not pending:
        return None

    conversation.pop("pending", None)
    _save_conversation(sender, conversation, settings)

    raised_at = pending.get("at")
    if isinstance(raised_at, datetime.datetime):
        age = _now() - raised_at.replace(tzinfo=raised_at.tzinfo or datetime.UTC)
        if age > PENDING_TTL:
            log_with(logger, logging.INFO, "ignoring a stale pending question")
            return None

    if "kind" not in pending:
        # Written by the previous close-list-only shape. A deploy can land while
        # one of these is parked in Firestore, and dropping it would silently
        # eat someone's "yes". Self-heals within PENDING_TTL.
        return {
            "kind": pending.get("action", ""),
            "payload": {"items": pending.get("items") or []},
            "at": raised_at,
        }
    return pending

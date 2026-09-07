"""Append-only log of every change to the shopping list.

`nano.list_events` has existed since the bootstrap and has never held a single
row — the table was designed, created, and then nothing ever wrote to it. It is
the input the Phase 4 shopping-pattern job needs, so the pattern work cannot
start until this has been accumulating for a few weeks.

Writes here are deliberately best-effort. Failing to record that coffee was
added must never stop coffee being added: the list in Firestore is the source of
truth for what to buy, and this is analytics. Every failure is logged loudly and
swallowed.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from typing import Any

from nano.clients import bigquery_client
from nano.config import Settings, get_settings
from nano.logging_setup import log_with

logger = logging.getLogger(__name__)

ADD_ITEM = "add_item"
REMOVE_ITEM = "remove_item"
CLOSE_LIST = "close_list"
PURCHASE_CLEARED = "purchase_cleared"

VALID_EVENT_TYPES = {ADD_ITEM, REMOVE_ITEM, CLOSE_LIST, PURCHASE_CLEARED}


def _rows_parameter(events: list[dict[str, Any]]):
    from google.cloud import bigquery

    struct_type = bigquery.StructQueryParameterType(
        bigquery.ScalarQueryParameterType("STRING", name="event_id"),
        bigquery.ScalarQueryParameterType("STRING", name="event_type"),
        bigquery.ScalarQueryParameterType("STRING", name="item_name"),
        bigquery.ScalarQueryParameterType("STRING", name="sender"),
        bigquery.ScalarQueryParameterType("TIMESTAMP", name="event_ts"),
    )
    values = [
        bigquery.StructQueryParameter(
            None,
            bigquery.ScalarQueryParameter("event_id", "STRING", event["event_id"]),
            bigquery.ScalarQueryParameter("event_type", "STRING", event["event_type"]),
            bigquery.ScalarQueryParameter("item_name", "STRING", event["item_name"]),
            bigquery.ScalarQueryParameter("sender", "STRING", event["sender"]),
            bigquery.ScalarQueryParameter("event_ts", "TIMESTAMP", event["event_ts"]),
        )
        for event in events
    ]
    return bigquery.ArrayQueryParameter("rows", struct_type, values)


def record(
    event_type: str,
    items: list[str],
    sender: str,
    settings: Settings | None = None,
    message_id: str = "",
) -> int:
    """Record one event per item in a single statement. Returns rows written.

    `event_id` is derived from the message id and the item, so a reprocessed
    message cannot double-count an item in the pattern analysis. Falls back to a
    random id when there is no message to key on.
    """
    from google.cloud import bigquery

    settings = settings or get_settings()

    if event_type not in VALID_EVENT_TYPES:
        logger.error("refusing to record unknown event type %r", event_type)
        return 0

    # close_list is a single event with no item; everything else is per-item.
    names = [n.strip() for n in items if n and n.strip()] if items else [""]
    if event_type == CLOSE_LIST:
        names = [""]
    if not names:
        return 0

    now = datetime.datetime.now(datetime.UTC)
    events = [
        {
            "event_id": _event_id(message_id, event_type, name, index),
            "event_type": event_type,
            "item_name": name,
            "sender": sender or "unknown",
            "event_ts": now,
        }
        for index, name in enumerate(names)
    ]

    try:
        bigquery_client().query(
            f"""
            INSERT INTO `{settings.table("list_events")}`
            (event_id, event_type, item_name, sender, event_ts)
            SELECT event_id, event_type, item_name, sender, event_ts
            FROM UNNEST(@rows)
            """,
            job_config=bigquery.QueryJobConfig(query_parameters=[_rows_parameter(events)]),
        ).result()
    except Exception as exc:
        # Analytics must never break the user's action.
        log_with(
            logger,
            logging.ERROR,
            "could not record list events",
            event_type=event_type,
            count=len(events),
            error=str(exc),
        )
        return 0

    log_with(
        logger,
        logging.INFO,
        "recorded list events",
        event_type=event_type,
        count=len(events),
        sender=sender,
    )
    return len(events)


_EVENT_NAMESPACE = uuid.UUID("2b7c1f18-9a4d-4f2b-8c53-71e0d9a6b4c1")


def _event_id(message_id: str, event_type: str, item_name: str, index: int) -> str:
    if not message_id:
        return str(uuid.uuid4())
    return str(uuid.uuid5(_EVENT_NAMESPACE, f"{message_id}|{event_type}|{index}|{item_name}"))

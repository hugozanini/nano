"""Answering questions about past purchases.

The query shapes stay as the bootstrap designed them: fixed SQL templates
parameterised by Gemini's classification, never freeform text-to-SQL, with date
ranges computed in Python. That was a deliberate reliability decision and it
holds up.

Three corrections, each from an observed failure:

**Empty results must carry context.** Asked "how much did we spend this month?"
moments after logging a receipt, the service answered `You've spent 0.00 across
0 shopping trip(s)`. The arithmetic was perfect — the receipt was from 29 August
and the question was asked on 6 September — but a bare zero right after logging
something reads as *the system lost my receipt*. An empty period now says what
the most recent trip actually was.

**Totals are per currency.** `SUM(total_amount)` across a euro shop in Spain and
a real shop in Brazil produces a number that means nothing.

**Timezone.** `date.today()` on Cloud Run is UTC, so an evening shop in Madrid
lands on the following day and "today" answers about the wrong one.
"""

from __future__ import annotations

import datetime
import logging
from decimal import Decimal
from zoneinfo import ZoneInfo

from nano.clients import bigquery_client
from nano.config import Settings, get_settings

logger = logging.getLogger(__name__)

PERIOD_LABELS = {
    "today": "today",
    "this_week": "this week",
    "this_month": "this month",
    "last_month": "last month",
    "all_time": "ever",
}


def today_in_household_tz(settings: Settings | None = None) -> datetime.date:
    settings = settings or get_settings()
    try:
        tz = ZoneInfo(settings.household_timezone)
    except Exception:
        logger.warning("unknown timezone %s; falling back to UTC", settings.household_timezone)
        tz = datetime.UTC
    return datetime.datetime.now(tz).date()


def period_to_date_range(
    period: str, settings: Settings | None = None
) -> tuple[datetime.date, datetime.date]:
    today = today_in_household_tz(settings)

    if period == "today":
        return today, today
    if period == "this_week":
        return today - datetime.timedelta(days=today.weekday()), today
    if period == "last_month":
        first_this_month = today.replace(day=1)
        last_month_end = first_this_month - datetime.timedelta(days=1)
        return last_month_end.replace(day=1), last_month_end
    if period == "all_time":
        return datetime.date(2000, 1, 1), today
    return today.replace(day=1), today  # this_month, and the default


def _money(amount: Decimal | float | int, currency: str) -> str:
    return f"{currency} {Decimal(str(amount)):.2f}"


def _query(sql: str, params: list, settings: Settings):
    from google.cloud import bigquery

    return list(
        bigquery_client()
        .query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params))
        .result()
    )


def most_recent_purchase(settings: Settings) -> dict | None:
    """The latest trip, used to give an empty result something to say."""
    rows = _query(
        f"""
        SELECT purchased_at, store_name, total_amount, currency
        FROM `{settings.table("purchases")}`
        ORDER BY purchased_at DESC, created_at DESC
        LIMIT 1
        """,
        [],
        settings,
    )
    if not rows:
        return None
    row = rows[0]
    return {
        "date": row.purchased_at.date(),
        "store": row.store_name,
        "total": row.total_amount,
        "currency": row.currency,
    }


def _total_spend(period: str, settings: Settings) -> str:
    from google.cloud import bigquery

    start, end = period_to_date_range(period, settings)
    label = PERIOD_LABELS.get(period, "in that period")

    rows = _query(
        f"""
        SELECT currency, SUM(total_amount) AS total, COUNT(*) AS trips
        FROM `{settings.table("purchases")}`
        WHERE DATE(purchased_at) BETWEEN @start AND @end
        GROUP BY currency
        ORDER BY total DESC
        """,
        [
            bigquery.ScalarQueryParameter("start", "DATE", start),
            bigquery.ScalarQueryParameter("end", "DATE", end),
        ],
        settings,
    )

    if not rows:
        # The bug this exists to fix: never answer a bare zero.
        latest = most_recent_purchase(settings)
        if latest is None:
            return "I haven't logged any receipts yet — send me a photo of one and I'll start tracking."
        return (
            f"Nothing logged {label} yet. Your most recent trip was "
            f"{latest['date']:%-d %b} at {latest['store']} "
            f"({_money(latest['total'], latest['currency'])})."
        )

    trips = sum(row.trips for row in rows)
    amounts = ", ".join(_money(row.total, row.currency) for row in rows)
    trip_word = "trip" if trips == 1 else "trips"
    return f"You've spent {amounts} {label}, across {trips} shopping {trip_word}."


def _last_purchase(item_name: str, settings: Settings) -> str:
    from google.cloud import bigquery

    rows = _query(
        f"""
        SELECT p.purchased_at, p.store_name, pi.item_name, pi.line_total, p.currency
        FROM `{settings.table("purchase_items")}` pi
        JOIN `{settings.table("purchases")}` p ON p.purchase_id = pi.purchase_id
        WHERE LOWER(pi.normalized_name) LIKE @pattern
        ORDER BY p.purchased_at DESC
        LIMIT 1
        """,
        [bigquery.ScalarQueryParameter("pattern", "STRING", f"%{item_name.lower()}%")],
        settings,
    )
    if not rows:
        return f"I don't see any past purchases of {item_name}."
    row = rows[0]
    return (
        f"You last bought {item_name} on {row.purchased_at.date():%-d %b} at {row.store_name} "
        f"— {row.item_name}, {_money(row.line_total, row.currency)}."
    )


def _spend_by_item(item_name: str, settings: Settings) -> str:
    from google.cloud import bigquery

    rows = _query(
        f"""
        SELECT p.currency, SUM(pi.line_total) AS total, COUNT(*) AS times
        FROM `{settings.table("purchase_items")}` pi
        JOIN `{settings.table("purchases")}` p ON p.purchase_id = pi.purchase_id
        WHERE LOWER(pi.normalized_name) LIKE @pattern
        GROUP BY p.currency
        ORDER BY total DESC
        """,
        [bigquery.ScalarQueryParameter("pattern", "STRING", f"%{item_name.lower()}%")],
        settings,
    )
    if not rows:
        return f"I haven't seen {item_name} on any receipt yet."

    times = sum(row.times for row in rows)
    amounts = ", ".join(_money(row.total, row.currency) for row in rows)
    time_word = "time" if times == 1 else "times"
    return f"You've spent {amounts} on {item_name}, across {times} {time_word}."


def _items_in_last_purchase(settings: Settings) -> str:
    """ "What did we buy?" — the obvious follow-up after logging a receipt.

    No intent covered this, so the classifier fell through to `get_list` and
    Nano answered with the shopping list instead of the receipt.
    """
    from google.cloud import bigquery

    latest = most_recent_purchase(settings)
    if latest is None:
        return "I haven't logged any receipts yet — send me a photo of one and I'll start tracking."

    rows = _query(
        f"""
        SELECT pi.item_name, pi.line_total
        FROM `{settings.table("purchase_items")}` pi
        JOIN `{settings.table("purchases")}` p ON p.purchase_id = pi.purchase_id
        WHERE DATE(p.purchased_at) = @purchased_on
        ORDER BY pi.line_total DESC
        """,
        [bigquery.ScalarQueryParameter("purchased_on", "DATE", latest["date"])],
        settings,
    )
    if not rows:
        return (
            f"Your last trip was {latest['date']:%-d %b} at {latest['store']} "
            f"({_money(latest['total'], latest['currency'])}), but I don't have the line items."
        )

    lines = "\n".join(
        f"- {row.item_name} ({_money(row.line_total, latest['currency'])})" for row in rows
    )
    return (
        f"Your last trip was {latest['date']:%-d %b} at {latest['store']}, "
        f"{_money(latest['total'], latest['currency'])} for {len(rows)} items:\n{lines}"
    )


def answer_history_query(
    query_type: str,
    period: str = "this_month",
    item_name: str | None = None,
    settings: Settings | None = None,
) -> str:
    settings = settings or get_settings()

    try:
        if query_type == "total_spend":
            return _total_spend(period or "this_month", settings)
        if query_type == "items_in_last_purchase":
            return _items_in_last_purchase(settings)
        if query_type == "last_purchase" and item_name:
            return _last_purchase(item_name, settings)
        if query_type == "spend_by_item" and item_name:
            return _spend_by_item(item_name, settings)
    except Exception:
        logger.exception("history query failed", extra={"extra_fields": {"query": query_type}})
        return "I couldn't reach the purchase history just now — try again in a moment?"

    if query_type in {"last_purchase", "spend_by_item"} and not item_name:
        return "Which item did you mean?"

    return "I couldn't quite figure out how to answer that — try rephrasing?"

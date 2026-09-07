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
        SELECT p.purchase_id, p.purchased_at, p.total_amount, p.currency,
               COALESCE(NULLIF(s.store_brand, ''), p.store_name) AS store_name
        FROM `{settings.table("purchases")}` p
        LEFT JOIN `{settings.table("stores")}` s USING (store_id)
        ORDER BY p.purchased_at DESC, p.created_at DESC
        LIMIT 1
        """,
        [],
        settings,
    )
    if not rows:
        return None
    row = rows[0]
    return {
        "purchase_id": row.purchase_id,
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


def _last_purchase(item_name: str, settings: Settings, display: str) -> str:
    from google.cloud import bigquery

    rows = _query(
        f"""
        SELECT p.purchased_at, pi.item_name, pi.item_name_en,
               pi.line_total, p.currency,
               COALESCE(NULLIF(s.store_brand, ''), p.store_name) AS store_name
        FROM `{settings.table("purchase_items")}` pi
        JOIN `{settings.table("purchases")}` p ON p.purchase_id = pi.purchase_id
        LEFT JOIN `{settings.table("stores")}` s ON s.store_id = p.store_id
        WHERE LOWER(pi.normalized_name) LIKE @pattern
        ORDER BY p.purchased_at DESC
        LIMIT 1
        """,
        [bigquery.ScalarQueryParameter("pattern", "STRING", f"%{item_name.lower()}%")],
        settings,
    )
    if not rows:
        return f"I don't see any past purchases of {display}."
    row = rows[0]
    return (
        f"You last bought {display} on {row.purchased_at.date():%-d %b} at {row.store_name} "
        f"— {_item_label(row)}, {_money(row.line_total, row.currency)}."
    )


def _spend_by_item(item_name: str, settings: Settings, display: str) -> str:
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
        return f"I haven't seen {display} on any receipt yet."

    times = sum(row.times for row in rows)
    amounts = ", ".join(_money(row.total, row.currency) for row in rows)
    time_word = "time" if times == 1 else "times"
    return f"You've spent {amounts} on {display}, across {times} {time_word}."


def _item_label(row) -> str:
    """Show the English name, with what the receipt printed beside it.

    A list of `MADUIXA`, `PIT FAM.` and `GREC NATURAL P-6` is unreadable to
    anyone who does not shop in Catalan, which was the complaint. English leads.

    The printed name is kept in brackets rather than dropped, because it is what
    is actually on the paper receipt and the till display — useful when checking
    a total or looking for the line again. It is omitted when the translation
    adds nothing.
    """
    printed = (getattr(row, "item_name", "") or "").strip()
    english = (getattr(row, "item_name_en", "") or "").strip()

    if not english:
        return printed
    if english.casefold() == printed.casefold():
        return english
    return f"{english} ({printed})"


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
        SELECT item_name, item_name_en, line_total
        FROM `{settings.table("purchase_items")}`
        WHERE purchase_id = @purchase_id
        ORDER BY line_total DESC
        """,
        [bigquery.ScalarQueryParameter("purchase_id", "STRING", latest["purchase_id"])],
        settings,
    )
    if not rows:
        return (
            f"Your last trip was {latest['date']:%-d %b} at {latest['store']} "
            f"({_money(latest['total'], latest['currency'])}), but I don't have the line items."
        )

    # The price is separated rather than bracketed: the printed name is already
    # in brackets, and nesting them reads badly on a phone.
    lines = "\n".join(
        f"- {_item_label(row)} \u2014 {_money(row.line_total, latest['currency'])}" for row in rows
    )
    return (
        f"Your last trip was {latest['date']:%-d %b} at {latest['store']}, "
        f"{_money(latest['total'], latest['currency'])} for {len(rows)} items:\n{lines}"
    )


def _explain_item(item_name: str, settings: Settings, display: str) -> str:
    """Say what a line on a receipt actually was.

    From the live chat, after Nano listed a purchase in English:

        You:  What is family pack breast? Chicken breast?
        Nano: I can't work that out yet … I've noted it as a feature request.

    Every fact needed was already stored — `PIT FAM.` printed, "family pack
    breast" translated, `meat` categorised, EUR 7.49 — and the question was
    asked *because* Nano's own translation was unclear. Answering "I can't"
    there is wrong twice over: it withholds data it has, and it files a
    capability gap that does not exist, which is worse than silence because the
    backlog is supposed to be the record of what is genuinely missing.

    What it deliberately does not do is answer the question that was actually
    asked. "Chicken breast?" cannot be confirmed: the receipt says `PIT FAM.`
    and nothing about the animal. Saying "yes, chicken" would be the same class
    of error as reporting euros as dollars — filling a gap in the input with
    something plausible. So the reply gives what the receipt gives, and says
    where that stops.
    """
    from google.cloud import bigquery

    pattern = f"%{(item_name or '').strip().casefold()}%"
    rows = _query(
        f"""
        SELECT pi.item_name, pi.item_name_en, pi.category, pi.line_total,
               p.purchased_at, p.currency, s.store_brand,
               COUNT(*) OVER () AS match_count
        FROM `{settings.table("purchase_items")}` pi
        JOIN `{settings.table("purchases")}` p USING (purchase_id)
        LEFT JOIN `{settings.table("stores")}` s USING (store_id)
        WHERE LOWER(pi.item_name_en) LIKE @pattern
           OR LOWER(pi.item_name) LIKE @pattern
           OR LOWER(pi.normalized_name) LIKE @pattern
        ORDER BY p.purchased_at DESC
        LIMIT 25
        """,
        [bigquery.ScalarQueryParameter("pattern", "STRING", pattern)],
        settings,
    )
    if not rows:
        # A lookup miss, not a missing capability. Saying so plainly keeps it
        # out of the backlog.
        return (
            f'I don\'t have anything matching "{display}" in your purchases. '
            "It may be from a receipt I haven't logged."
        )

    row = rows[0]
    printed = (row.item_name or "").strip()
    english = (row.item_name_en or "").strip()
    store = (row.store_brand or "").strip()
    when = row.purchased_at.strftime("%-d %b") if row.purchased_at else ""

    where = f" on your {store} receipt" if store else " on your receipt"
    where += f" from {when}" if when else ""

    if english and english.casefold() != printed.casefold():
        opening = f'"{english}" is how I read {printed}, printed{where}.'
    else:
        opening = f"{printed} appeared{where}."

    facts = []
    if row.category:
        facts.append(f"I filed it under {str(row.category).replace('_', ' ')}")
    if row.line_total is not None:
        facts.append(_money(row.line_total, row.currency))
    detail = f" {' — '.join(facts)}." if facts else ""

    times = int(row.match_count or 1)
    seen = f" You've bought it {times} times." if times > 1 else ""

    # The honest stop. Only worth saying where Nano translated an abbreviation,
    # because that is the reading the user is being asked to trust.
    caveat = ""
    if english and english.casefold() != printed.casefold():
        caveat = (
            "\n\nThat's my reading of the abbreviation on the receipt — it doesn't "
            "record anything beyond the printed name, so I can't confirm more than that."
        )

    return f"{opening}{detail}{seen}{caveat}"


def answer_history_query(
    query_type: str,
    period: str = "this_month",
    item_name: str | None = None,
    settings: Settings | None = None,
    display_name: str | None = None,
) -> str:
    """Answer one history question.

    `item_name` is the canonical term used for matching; `display_name` is the
    word the user actually typed, so the reply reads back in their language
    rather than echoing an internal canonical form.
    """
    settings = settings or get_settings()
    display = display_name or item_name or ""

    try:
        if query_type == "total_spend":
            return _total_spend(period or "this_month", settings)
        if query_type == "items_in_last_purchase":
            return _items_in_last_purchase(settings)
        if query_type == "last_purchase" and item_name:
            return _last_purchase(item_name, settings, display)
        if query_type == "spend_by_item" and item_name:
            return _spend_by_item(item_name, settings, display)
        if query_type == "explain_item" and item_name:
            return _explain_item(item_name, settings, display)
    except Exception:
        logger.exception("history query failed", extra={"extra_fields": {"query": query_type}})
        return "I couldn't reach the purchase history just now — try again in a moment?"

    if query_type in {"last_purchase", "spend_by_item", "explain_item"} and not item_name:
        return "Which item did you mean?"

    return "I couldn't quite figure out how to answer that — try rephrasing?"

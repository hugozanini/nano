"""Answering questions about past purchases.

Moved from the original module with the query shapes preserved: fixed SQL
templates parameterised by Gemini's classification, never freeform text-to-SQL,
and date ranges computed in Python. That was a deliberate reliability decision in
the bootstrap and it holds up.

One correctness fix here: date ranges are computed in the household's timezone
rather than UTC. On Cloud Run ``date.today()`` is UTC, so an evening shop in
Spain lands on the following day and "today" answers about the wrong day.

The empty-result phrasing and the extra query types are the product PR's job.
"""

from __future__ import annotations

import datetime
import logging
from zoneinfo import ZoneInfo

from nano.clients import bigquery_client
from nano.config import Settings, get_settings

logger = logging.getLogger(__name__)


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


def answer_history_query(
    query_type: str,
    period: str = "this_month",
    item_name: str | None = None,
    settings: Settings | None = None,
) -> str:
    from google.cloud import bigquery

    settings = settings or get_settings()
    client = bigquery_client()

    if query_type == "total_spend":
        start, end = period_to_date_range(period, settings)
        rows = list(
            client.query(
                f"""
                SELECT SUM(total_amount) AS total, COUNT(*) AS trips
                FROM `{settings.table("purchases")}`
                WHERE DATE(purchased_at) BETWEEN @start AND @end
                """,
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[
                        bigquery.ScalarQueryParameter("start", "DATE", start),
                        bigquery.ScalarQueryParameter("end", "DATE", end),
                    ]
                ),
            ).result()
        )
        total = rows[0].total or 0 if rows else 0
        trips = rows[0].trips or 0 if rows else 0
        return f"You've spent {total:.2f} across {trips} shopping trip(s) in that period."

    if query_type == "last_purchase" and item_name:
        rows = list(
            client.query(
                f"""
                SELECT p.purchased_at, p.store_name
                FROM `{settings.table("purchase_items")}` pi
                JOIN `{settings.table("purchases")}` p ON p.purchase_id = pi.purchase_id
                WHERE LOWER(pi.normalized_name) LIKE @pattern
                ORDER BY p.purchased_at DESC
                LIMIT 1
                """,
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[
                        bigquery.ScalarQueryParameter("pattern", "STRING", f"%{item_name.lower()}%")
                    ]
                ),
            ).result()
        )
        if not rows:
            return f"I don't see any past purchases of {item_name}."
        row = rows[0]
        return f"You last bought {item_name} on {row.purchased_at.date()} at {row.store_name}."

    if query_type == "spend_by_item" and item_name:
        rows = list(
            client.query(
                f"""
                SELECT SUM(line_total) AS total, COUNT(*) AS times
                FROM `{settings.table("purchase_items")}`
                WHERE LOWER(normalized_name) LIKE @pattern
                """,
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[
                        bigquery.ScalarQueryParameter("pattern", "STRING", f"%{item_name.lower()}%")
                    ]
                ),
            ).result()
        )
        total = rows[0].total or 0 if rows else 0
        times = rows[0].times or 0 if rows else 0
        return f"You've spent {total:.2f} on {item_name} across {times} purchase(s)."

    return "I couldn't quite figure out how to answer that — try rephrasing?"

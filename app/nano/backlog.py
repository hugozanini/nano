"""A backlog built from what people actually asked for.

Two things feed it. An explicit "can you add X" is the obvious one. The more
valuable one is a question Nano could not answer, captured at the moment it
occurred — which is the only time you reliably learn what is missing, because
nobody files a ticket for a grocery bot.

Requests are deduplicated on a canonical form, so the table shows demand rather
than repetition: the same thing asked four times is one row with a count of
four. That distinction is the whole point of storing it, since a backlog ordered
by how often something was wanted is a plan and a list of every utterance is not.

Nothing leaves this table automatically. These are free-text messages from a
private chat, and the repository is going public, so exporting one to GitHub is
a human decision made in `scripts/backlog.py`.

Writes are best-effort, like the rest of the analytics side: failing to record a
request must never cost the user their reply.
"""

from __future__ import annotations

import datetime
import logging
import uuid

from nano.clients import bigquery_client
from nano.config import Settings, get_settings
from nano.logging_setup import log_with
from nano.question_log import canonicalise_question

logger = logging.getLogger(__name__)

EXPLICIT = "explicit"
UNANSWERED = "unanswered"
SOURCES = {EXPLICIT, UNANSWERED}

NEW = "new"
PLANNED = "planned"
DONE = "done"
DECLINED = "declined"
STATUSES = {NEW, PLANNED, DONE, DECLINED}

# Stable namespace: the same request always lands on the same row.
REQUEST_NAMESPACE = uuid.UUID("3f9a7c21-6b48-4d5e-8a13-2c7e9d40b5f6")


def request_id_for(canonical: str) -> str:
    return str(uuid.uuid5(REQUEST_NAMESPACE, canonical))


def record(
    text: str,
    requested_by: str = "",
    source: str = UNANSWERED,
    settings: Settings | None = None,
) -> str:
    """Add a request, or count another vote for one already there.

    Returns the request id, or "" if nothing was recorded. Never raises.
    """
    from google.cloud import bigquery

    settings = settings or get_settings()

    cleaned = (text or "").strip()
    if not cleaned:
        return ""
    if source not in SOURCES:
        logger.error("refusing to record a request with unknown source %r", source)
        return ""

    canonical = canonicalise_question(cleaned)
    if not canonical:
        # Nothing meaningful left after normalising — punctuation, or a single
        # stop word. Not signal.
        return ""

    request_id = request_id_for(canonical)
    now = datetime.datetime.now(datetime.UTC)

    try:
        bigquery_client().query(
            f"""
            MERGE `{settings.table("feature_requests")}` T
            USING (SELECT @request_id AS request_id) S
            ON T.request_id = S.request_id
            WHEN MATCHED THEN UPDATE SET
              request_count     = IFNULL(T.request_count, 0) + 1,
              last_requested_at = @now,
              -- An explicit ask outranks an inferred one as the description.
              text   = IF(@source = 'explicit' AND T.source != 'explicit', @text, T.text),
              source = IF(@source = 'explicit', 'explicit', T.source)
            WHEN NOT MATCHED THEN INSERT (
              request_id, text, canonical_text, requested_by, source, status,
              request_count, first_requested_at, last_requested_at,
              github_issue_url, notes
            ) VALUES (
              @request_id, @text, @canonical, @requested_by, @source, 'new',
              1, @now, @now, '', ''
            )
            """,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("request_id", "STRING", request_id),
                    bigquery.ScalarQueryParameter("text", "STRING", cleaned[:1000]),
                    bigquery.ScalarQueryParameter("canonical", "STRING", canonical),
                    bigquery.ScalarQueryParameter("requested_by", "STRING", requested_by),
                    bigquery.ScalarQueryParameter("source", "STRING", source),
                    bigquery.ScalarQueryParameter("now", "TIMESTAMP", now),
                ]
            ),
        ).result()
    except Exception as exc:
        # Never at the cost of the user's reply.
        log_with(logger, logging.WARNING, "could not record a feature request", error=str(exc))
        return ""

    log_with(
        logger,
        logging.INFO,
        "recorded feature request",
        request_id=request_id,
        source=source,
    )
    return request_id


def open_requests(limit: int = 20, settings: Settings | None = None) -> list[dict]:
    """The backlog, most-wanted first."""
    from google.cloud import bigquery

    settings = settings or get_settings()
    try:
        rows = (
            bigquery_client()
            .query(
                f"""
                SELECT request_id, text, status, source, request_count,
                       first_requested_at, last_requested_at, days_open,
                       github_issue_url
                FROM `{settings.table("v_backlog")}`
                ORDER BY request_count DESC, last_requested_at DESC
                LIMIT @limit
                """,
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[bigquery.ScalarQueryParameter("limit", "INT64", limit)]
                ),
            )
            .result()
        )
    except Exception as exc:
        log_with(logger, logging.WARNING, "could not read the backlog", error=str(exc))
        return []
    return [dict(row.items()) for row in rows]


def set_status(
    request_id: str,
    status: str,
    github_issue_url: str = "",
    notes: str = "",
    settings: Settings | None = None,
) -> bool:
    """Move a request through triage. Used by scripts/backlog.py."""
    from google.cloud import bigquery

    settings = settings or get_settings()
    if status not in STATUSES:
        logger.error("unknown status %r", status)
        return False

    try:
        bigquery_client().query(
            f"""
            UPDATE `{settings.table("feature_requests")}`
            SET status = @status,
                github_issue_url = IF(@url != '', @url, github_issue_url),
                notes = IF(@notes != '', @notes, notes)
            WHERE request_id = @request_id
            """,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("request_id", "STRING", request_id),
                    bigquery.ScalarQueryParameter("status", "STRING", status),
                    bigquery.ScalarQueryParameter("url", "STRING", github_issue_url),
                    bigquery.ScalarQueryParameter("notes", "STRING", notes),
                ]
            ),
        ).result()
    except Exception as exc:
        log_with(logger, logging.ERROR, "could not update a request", error=str(exc))
        return False
    return True


def format_for_chat(requests: list[dict]) -> str:
    """Render the backlog as a WhatsApp message."""
    if not requests:
        return "Nothing in the backlog — nobody's asked for anything I couldn't do yet."

    lines = [f"Here's what's been asked for ({len(requests)}):"]
    for entry in requests:
        count = entry.get("request_count") or 1
        times = "" if count == 1 else f" (asked {count} times)"
        status = entry.get("status") or "new"
        marker = "" if status == "new" else f" [{status}]"
        lines.append(f"- {entry.get('text', '')}{times}{marker}")
    return "\n".join(lines)

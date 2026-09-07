"""Recording what was asked and how it was answered.

Not for auditing — for deciding what to build next. Nano answers common
questions from hand-written SQL templates, which are fast, free and
deterministic, and everything else from generated SQL, which is flexible but
slower and billable. Knowing which open questions keep recurring is what tells
us when one has earned a template.

It also records the questions nothing could answer, which is the most direct
product signal there is: someone wanted something and did not get it.

Writes are best-effort throughout. Telemetry that can break a user's answer is
worse than no telemetry, so every failure here is logged and swallowed.
"""

from __future__ import annotations

import datetime
import logging
import re
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field

from nano.clients import bigquery_client
from nano.config import Settings, get_settings
from nano.logging_setup import log_with

logger = logging.getLogger(__name__)

TEMPLATE = "template"
GENERATED_SQL = "generated_sql"
UNANSWERABLE = "unanswerable"
FAILED = "failed"
# Asked back rather than answered. Recorded so a chronically ambiguous phrasing
# shows up in v_question_patterns the same way an unserved question does — and
# one that always resolves the same way is a candidate for becoming the default.
CLARIFIED = "clarified"

RESOLUTIONS = {TEMPLATE, GENERATED_SQL, UNANSWERABLE, FAILED, CLARIFIED}

# Words that carry no meaning for grouping questions. Kept small on purpose:
# over-stripping merges genuinely different questions, which is worse than
# leaving a couple of near-duplicates apart.
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "do",
        "does",
        "did",
        "we",
        "i",
        "you",
        "my",
        "our",
        "us",
        "please",
        "can",
        "could",
        "would",
        "tell",
        "me",
        "show",
        "much",
        "many",
        "what",
        "which",
        "where",
        "when",
        "who",
        "how",
        "on",
        "in",
        "at",
        "of",
        "for",
        "to",
        "from",
        "and",
        "or",
    ]
)


def canonicalise_question(text: str) -> str:
    """Reduce a question to a comparable form.

    "How much did we spend on milk?" and "how much do we spend on milk" become
    the same string, so the pattern view counts them as one question asked
    twice rather than two asked once.

    Deliberately deterministic and model-free: this decides grouping, and a
    grouping that shifts between runs makes the telemetry useless.
    """
    lowered = re.sub(r"[^a-z0-9\s]", " ", str(text or "").lower())
    # Single characters are dropped: splitting on punctuation turns "what's"
    # into "what" plus a stray "s", which would otherwise stop it grouping with
    # "what is".
    words = [w for w in lowered.split() if len(w) > 1 and w not in _STOPWORDS]
    return " ".join(sorted(set(words))) if words else ""


@dataclass
class QuestionRecord:
    question: str
    asked_by: str = ""
    resolution: str = FAILED
    template_name: str = ""
    generated_sql: str = ""
    row_count: int = 0
    bytes_scanned: int = 0
    latency_ms: int = 0
    answered: bool = False
    failure_reason: str = ""
    question_id: str = field(default_factory=lambda: str(uuid.uuid4()))


def record(entry: QuestionRecord, settings: Settings | None = None) -> bool:
    """Write one question record. Returns whether it landed; never raises."""
    from google.cloud import bigquery

    settings = settings or get_settings()

    if entry.resolution not in RESOLUTIONS:
        logger.error("refusing to log unknown resolution %r", entry.resolution)
        return False

    try:
        bigquery_client().query(
            f"""
            INSERT INTO `{settings.table("question_log")}`
            (question_id, asked_at, asked_by, question, canonical, resolution,
             template_name, generated_sql, row_count, bytes_scanned, latency_ms,
             answered, failure_reason)
            SELECT @question_id, @asked_at, @asked_by, @question, @canonical,
                   @resolution, @template_name, @generated_sql, @row_count,
                   @bytes_scanned, @latency_ms, @answered, @failure_reason
            """,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("question_id", "STRING", entry.question_id),
                    bigquery.ScalarQueryParameter(
                        "asked_at", "TIMESTAMP", datetime.datetime.now(datetime.UTC)
                    ),
                    bigquery.ScalarQueryParameter("asked_by", "STRING", entry.asked_by),
                    bigquery.ScalarQueryParameter("question", "STRING", entry.question[:1000]),
                    bigquery.ScalarQueryParameter(
                        "canonical", "STRING", canonicalise_question(entry.question)
                    ),
                    bigquery.ScalarQueryParameter("resolution", "STRING", entry.resolution),
                    bigquery.ScalarQueryParameter("template_name", "STRING", entry.template_name),
                    bigquery.ScalarQueryParameter(
                        "generated_sql", "STRING", entry.generated_sql[:8000]
                    ),
                    bigquery.ScalarQueryParameter("row_count", "INT64", entry.row_count),
                    bigquery.ScalarQueryParameter("bytes_scanned", "INT64", entry.bytes_scanned),
                    bigquery.ScalarQueryParameter("latency_ms", "INT64", entry.latency_ms),
                    bigquery.ScalarQueryParameter("answered", "BOOL", entry.answered),
                    bigquery.ScalarQueryParameter(
                        "failure_reason", "STRING", entry.failure_reason[:500]
                    ),
                ]
            ),
        ).result()
    except Exception as exc:
        # Telemetry must never cost the user their answer.
        log_with(
            logger,
            logging.WARNING,
            "could not record the question",
            resolution=entry.resolution,
            error=str(exc),
        )
        return False

    log_with(
        logger,
        logging.INFO,
        "recorded question",
        resolution=entry.resolution,
        answered=entry.answered,
        template=entry.template_name or None,
    )
    return True


@contextmanager
def track(question: str, asked_by: str = "", settings: Settings | None = None):
    """Time a question and record it, whatever happens.

    Used as:

        with question_log.track(text, sender) as entry:
            entry.resolution = question_log.TEMPLATE
            entry.template_name = "total_spend"
            entry.answered = True

    An exception inside the block is recorded as a failure and re-raised, so the
    telemetry reflects what actually happened rather than only the happy path.
    """
    entry = QuestionRecord(question=question, asked_by=asked_by)
    started = time.perf_counter()
    try:
        yield entry
    except Exception as exc:
        entry.resolution = FAILED
        entry.answered = False
        entry.failure_reason = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        entry.latency_ms = int((time.perf_counter() - started) * 1000)
        record(entry, settings)


def template_candidates(limit: int = 20, settings: Settings | None = None) -> list[dict]:
    """Recurring questions that no template serves — what to build next."""
    from google.cloud import bigquery

    settings = settings or get_settings()
    try:
        rows = (
            bigquery_client()
            .query(
                f"""
                SELECT canonical, example_question, times_asked, unanswered,
                       served_by_generated_sql, last_asked
                FROM `{settings.table("v_question_patterns")}`
                WHERE template_candidate
                ORDER BY times_asked DESC, last_asked DESC
                LIMIT @limit
                """,
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[bigquery.ScalarQueryParameter("limit", "INT64", limit)]
                ),
            )
            .result()
        )
    except Exception as exc:
        log_with(logger, logging.WARNING, "could not read question patterns", error=str(exc))
        return []
    return [dict(row.items()) for row in rows]

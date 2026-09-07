"""Answering open questions by writing SQL for them.

The four hand-written query templates in `history.py` cover the questions people
ask most, and they stay: instant, free, deterministic and already tested.
Everything else — "which shop do we use most?", "is milk getting more
expensive?", "where should I buy in bulk?" — cannot be enumerated in advance, so
this writes SQL for it instead.

Giving a language model SQL access to the household's purchase history needs
real containment, not a prompt asking it nicely. Five layers, each independent
and each verified:

1. **A read-only identity.** Queries run as `nano-analytics`, impersonated from
   the runtime account, holding `bigquery.jobUser` and READER on the `nano`
   dataset and nothing else. Verified: a `DELETE` is refused with
   `bigquery.tables.updateData denied`, and another dataset is unreachable. This
   holds even if every check below is bypassed, because IAM enforces it rather
   than our code.
2. **Statement type, from BigQuery's own parser.** A dry run reports
   `statement_type`, so a non-SELECT is refused before execution. This is
   authoritative, unlike scanning the SQL text for "DELETE", which is trivially
   evaded.
3. **Dataset boundary.** The dry run also lists referenced tables; anything
   outside the allowed dataset is refused.
4. **A cost ceiling.** `maximum_bytes_billed` is enforced by BigQuery itself.
5. **Row and time limits**, so a runaway aggregate cannot hang a task.

The other half of being useful here is refusing to invent. Asked for a trend
with one data point, the honest answer is that there isn't one yet — a
confidently wrong answer about money is the worst outcome available, and it is
the easy failure mode for this kind of feature.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from nano.config import Settings, get_settings
from nano.logging_setup import log_with

logger = logging.getLogger(__name__)

# Only these are described to the model, and only these should appear in the SQL
# it writes. They encode the correct joins; the base tables do not.
ANALYSIS_VIEWS = (
    "v_purchase_items_normalized",
    "v_spend_by_category_month",
    "v_stores",
)

MAX_ROWS = 200
QUERY_TIMEOUT_SECONDS = 30

SCHEMA = """
`{dataset}.v_purchase_items_normalized` — one row per line item on a receipt.
    purchase_id STRING, purchased_at TIMESTAMP, purchased_on DATE,
    purchased_month STRING ('2026-08'),
    store_brand STRING, store_name_original STRING,
    country_code STRING, region STRING,
    city_printed STRING (only when the receipt showed an address),
    city_inferred STRING (a guess; never mix it with city_printed),
    location_confidence STRING ('high'|'medium'|'low'), receipt_language STRING,
    item_name_original STRING (as printed, often abbreviated and not English),
    item_name_english STRING (translation),
    item_canonical STRING (the concept: 'milk', 'egg' — GROUP BY this),
    category STRING ('produce'|'dairy'|'meat'|'bakery'|'pantry'|'household'|'personal_care'|...),
    quantity NUMERIC, unit_price NUMERIC, line_total NUMERIC,
    currency STRING, line_total_eur NUMERIC (comparable across countries)

`{dataset}.v_spend_by_category_month` — monthly rollup.
    purchased_month STRING, country_code STRING, category STRING,
    trips INT64, line_items INT64, spend_eur NUMERIC

`{dataset}.v_stores` — one row per shop.
    store_id STRING, store_brand STRING, country_code STRING, region STRING,
    city_printed STRING, city_inferred STRING, location_confidence STRING,
    receipt_count INT64, first_seen_at TIMESTAMP, last_seen_at TIMESTAMP,
    total_spend_eur NUMERIC
"""

SQL_PROMPT = """You write BigQuery Standard SQL to answer questions about a household's grocery purchases.

Available views — use ONLY these, never any other table:
{schema}

Rules:
- Return ONE SELECT statement. No DML, no DDL, no scripting, no semicolon-separated statements.
- Reference only the views listed above.
- Always include a LIMIT of at most {max_rows}.
- Use `item_canonical` to group products, because `item_name_original` is printed in the shop's own language and abbreviations.
- Use `line_total_eur` when comparing or summing money across shops or countries; `line_total` is in the receipt's own currency.
- Alias EVERY selected expression with a clear name. An unnamed column arrives as `f0_` and the answer cannot describe it.
- When returning an amount in euros, alias it so the unit is unmistakable, e.g. `SUM(line_total_eur) AS total_eur`. When returning an amount in the receipt's own currency, also select `currency`.
- For "where is cheapest" style questions, compare unit_price for the same item_canonical across store_brand.
- Never use `city_inferred` as though it were a fact; prefer `city_printed`, or `region`.

Return ONLY the SQL. No explanation, no markdown fences.

{context}Question: {question}"""

RETRY_PROMPT = """That SQL was rejected: {error}

Rewrite it to fix that problem. Same rules as before. Return ONLY the corrected SQL.

Previous attempt:
{sql}"""

ANSWER_PROMPT = """You are answering a household's question about their grocery spending, in a WhatsApp message.

Question: {question}

The query returned {row_count} row(s):
{rows}

Write a short, direct answer — one or two sentences, plain text, no markdown tables. Quote the actual numbers.

Currency: a column whose name ends in `_eur` is euros — write it as EUR. Otherwise use the `currency` value in the rows. Never introduce a currency symbol that the data does not support; guessing the wrong one is a real error in a spending assistant.

If the data is too thin to support the question, say so plainly instead of inferring anything. For example, a trend needs several purchases over time and a "cheapest shop" comparison needs the same item bought at more than one shop. Being honest that there is not enough history yet is a good answer; guessing is not.

If the rows are empty, say nothing matching was found, briefly."""


class UnsafeQuery(Exception):
    """The generated SQL failed validation and was not run."""


@dataclass
class AnalyticsResult:
    answer: str
    sql: str = ""
    row_count: int = 0
    bytes_scanned: int = 0
    succeeded: bool = False
    failure_reason: str = ""


@lru_cache(maxsize=1)
def analytics_client():
    """A BigQuery client that physically cannot write.

    Impersonates the read-only analytics identity. If impersonation is not
    configured, falls back to the default client — which is correct for local
    development and tests, and is why the validation layers below are not
    optional.
    """
    import google.auth
    from google.cloud import bigquery

    settings = get_settings()
    if not settings.analytics_service_account:
        logger.warning(
            "no ANALYTICS_SERVICE_ACCOUNT set; generated SQL will run as the default "
            "identity. Validation still applies, but IAM containment does not."
        )
        return bigquery.Client()

    from google.auth import impersonated_credentials

    source, _ = google.auth.default()
    credentials = impersonated_credentials.Credentials(
        source_credentials=source,
        target_principal=settings.analytics_service_account,
        target_scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    logger.info("analytics queries run as %s", settings.analytics_service_account)
    return bigquery.Client(project=settings.project_id or None, credentials=credentials)


def reset_client() -> None:
    """Test hook."""
    clear = getattr(analytics_client, "cache_clear", None)
    if clear is not None:
        clear()


def strip_fences(text: str) -> str:
    """Models add ```sql fences however firmly you ask them not to."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = "\n".join(
            line for line in raw.splitlines() if not line.strip().startswith("```")
        ).strip()
    return raw.rstrip(";").strip()


def validate(sql: str, settings: Settings | None = None) -> tuple[bool, str, int]:
    """Dry-run the SQL and decide whether it may run.

    Returns ``(ok, reason, bytes)``. Nothing executes here — a dry run compiles
    the query and reports what it would do, which is how a hallucinated column
    or a `DELETE` is caught before it can matter.
    """
    from google.cloud import bigquery

    settings = settings or get_settings()

    if not sql.strip():
        return False, "the model returned no SQL", 0

    try:
        job = analytics_client().query(
            sql,
            job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False),
        )
    except Exception as exc:
        return False, str(exc).splitlines()[0][:300], 0

    # BigQuery's own parser decides what kind of statement this is. Far stronger
    # than scanning the text, which "/*DELETE*/" style tricks defeat.
    if (job.statement_type or "").upper() != "SELECT":
        return False, f"only SELECT is allowed, got {job.statement_type}", 0

    allowed_dataset = settings.bq_dataset.split(".")[-1]
    for table in job.referenced_tables or []:
        if table.dataset_id != allowed_dataset:
            return (
                False,
                f"query reaches outside {allowed_dataset} (found {table.dataset_id})",
                0,
            )

    scanned = int(job.total_bytes_processed or 0)
    if scanned > settings.analytics_max_bytes:
        return (
            False,
            f"would scan {scanned} bytes, over the {settings.analytics_max_bytes} limit",
            scanned,
        )

    return True, "", scanned


def _ensure_limit(sql: str) -> str:
    """Add a LIMIT when the model forgot, so a result set stays sendable."""
    if re.search(r"\blimit\s+\d+\s*$", sql, re.IGNORECASE):
        return sql
    return f"{sql}\nLIMIT {MAX_ROWS}"


def execute(sql: str, settings: Settings | None = None) -> list[dict[str, Any]]:
    """Run validated SQL under a cost ceiling and a timeout."""
    from google.cloud import bigquery

    settings = settings or get_settings()
    job = analytics_client().query(
        sql,
        job_config=bigquery.QueryJobConfig(
            maximum_bytes_billed=settings.analytics_max_bytes,
            use_query_cache=True,
        ),
    )
    rows = job.result(timeout=QUERY_TIMEOUT_SECONDS, max_results=MAX_ROWS)
    return [dict(row.items()) for row in rows]


def _format_rows(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "(no rows)"
    lines = []
    for row in rows[:40]:
        lines.append(", ".join(f"{k}={v}" for k, v in row.items()))
    if len(rows) > 40:
        lines.append(f"... and {len(rows) - 40} more")
    return "\n".join(lines)


def answer(
    question: str,
    context: str = "",
    settings: Settings | None = None,
    model: Any = None,
) -> AnalyticsResult:
    """Answer an open question from the purchase history.

    Writes SQL, validates it without running it, retries once against the
    validation error, executes it under a cost ceiling, and turns the rows into
    a sentence. Never raises: every failure becomes an honest answer.
    """
    settings = settings or get_settings()

    if model is None:
        from nano.gemini import extraction_model

        try:
            model = extraction_model()
        except Exception as exc:
            return AnalyticsResult(
                answer="I can't reach my analysis tools just now — try again in a moment?",
                succeeded=False,
                failure_reason=str(exc),
            )

    dataset = settings.bq_dataset
    prompt = SQL_PROMPT.format(
        schema=SCHEMA.format(dataset=dataset),
        max_rows=MAX_ROWS,
        context=f"{context}\n\n" if context else "",
        question=question,
    )

    sql = ""
    last_error = ""
    scanned = 0

    for attempt in (1, 2):
        try:
            response = model.generate_content(prompt)
            sql = _ensure_limit(strip_fences(getattr(response, "text", "") or ""))
        except Exception as exc:
            log_with(logger, logging.WARNING, "SQL generation failed", error=str(exc))
            return AnalyticsResult(
                answer="I couldn't work that one out — try rephrasing it?",
                succeeded=False,
                failure_reason=f"generation failed: {exc}",
            )

        ok, reason, scanned = validate(sql, settings)
        if ok:
            break

        last_error = reason
        log_with(
            logger,
            logging.WARNING,
            "generated SQL rejected",
            attempt=attempt,
            reason=reason,
        )
        if attempt == 2:
            return AnalyticsResult(
                answer="I couldn't work that one out — try rephrasing it?",
                sql=sql,
                succeeded=False,
                failure_reason=f"validation failed: {last_error}",
            )
        # Feed the error back; most rejections are a wrong column name.
        prompt = RETRY_PROMPT.format(error=reason, sql=sql)

    try:
        rows = execute(sql, settings)
    except Exception as exc:
        log_with(logger, logging.ERROR, "analytics query failed", error=str(exc))
        return AnalyticsResult(
            answer="I couldn't work that one out — try rephrasing it?",
            sql=sql,
            bytes_scanned=scanned,
            succeeded=False,
            failure_reason=f"execution failed: {exc}",
        )

    try:
        summary = model.generate_content(
            ANSWER_PROMPT.format(question=question, row_count=len(rows), rows=_format_rows(rows))
        )
        text = (getattr(summary, "text", "") or "").strip()
    except Exception as exc:
        log_with(logger, logging.WARNING, "answer phrasing failed", error=str(exc))
        text = ""

    if not text:
        return AnalyticsResult(
            answer="I found the data but couldn't phrase an answer — try asking again?",
            sql=sql,
            row_count=len(rows),
            bytes_scanned=scanned,
            succeeded=False,
            failure_reason="empty summary",
        )

    log_with(
        logger,
        logging.INFO,
        "answered from generated SQL",
        rows=len(rows),
        bytes=scanned,
    )
    return AnalyticsResult(
        answer=text,
        sql=sql,
        row_count=len(rows),
        bytes_scanned=scanned,
        succeeded=True,
    )

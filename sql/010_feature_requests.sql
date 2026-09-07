-- The backlog, built from what people actually asked for.
--
-- Two sources feed it: an explicit "can you add X", and — more valuable — a
-- question Nano could not answer. The second is the only moment you reliably
-- learn what is missing, because nobody files a ticket for a grocery bot.
--
-- Deduplicated on a canonical form so the table shows demand rather than
-- repetition: the same request asked four times is one row with a count of
-- four, not four rows.
--
-- Requests are free text from a private WhatsApp chat, so nothing here is
-- exported anywhere automatically. `scripts/backlog.py` can push a chosen entry
-- to GitHub Issues, and that stays a human decision because the repository is
-- going public.
--
-- Substitute ${DATASET}, ${WAREHOUSE} and ${CONNECTION} before running.

CREATE TABLE IF NOT EXISTS `${DATASET}.feature_requests` (
  request_id         STRING,
  text               STRING,   -- as asked, the first time
  canonical_text     STRING,   -- normalised, for deduplication
  requested_by       STRING,
  source             STRING,   -- explicit | unanswered
  status             STRING,   -- new | planned | done | declined
  request_count      INT64,
  first_requested_at TIMESTAMP,
  last_requested_at  TIMESTAMP,
  github_issue_url   STRING,
  notes              STRING
)
WITH CONNECTION `us-central1.${CONNECTION}`
OPTIONS (
  table_format = 'ICEBERG',
  file_format  = 'PARQUET',
  storage_uri  = '${WAREHOUSE}/feature_requests'
);

-- What to build next: most-asked first, still open.
CREATE OR REPLACE VIEW `${DATASET}.v_backlog` AS
SELECT
  request_id,
  text,
  status,
  source,
  request_count,
  first_requested_at,
  last_requested_at,
  github_issue_url,
  DATE_DIFF(CURRENT_DATE(), DATE(first_requested_at), DAY) AS days_open
FROM `${DATASET}.feature_requests`
WHERE status NOT IN ('done', 'declined');

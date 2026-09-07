-- Every question asked, and how it was answered.
--
-- The point is not auditing, it is finding out what to build next. Nano answers
-- common questions from hand-written SQL templates, which are fast, free and
-- deterministic, and everything else from generated SQL, which is flexible but
-- slower and billable. Knowing which open questions keep recurring is what tells
-- us when one deserves promoting into a template.
--
-- It also records the questions that could not be answered at all, which is the
-- most direct product signal available: someone wanted something and did not
-- get it.
--
-- Substitute ${DATASET}, ${WAREHOUSE} and ${CONNECTION} before running.

CREATE TABLE IF NOT EXISTS `${DATASET}.question_log` (
  question_id     STRING,
  asked_at        TIMESTAMP,
  asked_by        STRING,
  question        STRING,   -- as typed
  canonical       STRING,   -- normalised, so re-phrasings group together
  resolution      STRING,   -- template | generated_sql | unanswerable | failed
  template_name   STRING,   -- which template, when one served it
  generated_sql   STRING,   -- what was run, when SQL was written for it
  row_count       INT64,
  bytes_scanned   INT64,
  latency_ms      INT64,
  answered        BOOL,     -- did the user actually get an answer
  failure_reason  STRING
)
WITH CONNECTION `us-central1.${CONNECTION}`
OPTIONS (
  table_format = 'ICEBERG',
  file_format  = 'PARQUET',
  storage_uri  = '${WAREHOUSE}/question_log'
);

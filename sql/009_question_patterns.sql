-- Which questions recur, and how well they are served.
--
-- Read this to decide what to build: a canonical question asked repeatedly that
-- always needs generated SQL is a template waiting to be written, and one that
-- is never answered at all is a feature request in everything but name.
--
-- Substitute ${DATASET} before running.

CREATE OR REPLACE VIEW `${DATASET}.v_question_patterns` AS
SELECT
  canonical,
  COUNT(*)                                        AS times_asked,
  COUNT(DISTINCT asked_by)                        AS askers,
  COUNTIF(resolution = 'template')                AS served_by_template,
  COUNTIF(resolution = 'generated_sql')           AS served_by_generated_sql,
  COUNTIF(resolution = 'unanswerable')            AS unanswered,
  COUNTIF(resolution = 'failed')                  AS failed,
  COUNTIF(answered)                               AS answered,
  ROUND(AVG(latency_ms))                          AS avg_latency_ms,
  ANY_VALUE(question)                             AS example_question,
  ANY_VALUE(generated_sql HAVING MAX asked_at)    AS latest_generated_sql,
  MIN(asked_at)                                   AS first_asked,
  MAX(asked_at)                                   AS last_asked,
  -- A question asked more than once that never uses a template is the clearest
  -- candidate for one.
  COUNT(*) > 1 AND COUNTIF(resolution = 'template') = 0 AS template_candidate
FROM `${DATASET}.question_log`
GROUP BY canonical;

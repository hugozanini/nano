-- Record where a receipt came from, and what that conclusion rests on.
--
-- Receipts arrive in whatever language the shop prints. Analysis needs the
-- country, and the temptation is to read it off the language — but Catalan is
-- also spoken in Andorra, southern France and Sardinia, and Spanish spans two
-- continents. The tax table is far better evidence: a receipt showing IVA of
-- 4/10/21 is Spain and nowhere else (Portugal 6/13/23, France 5.5/10/20).
--
-- Hence two ideas in these columns:
--
--   * `location_confidence` and `location_evidence` travel with every row, so
--     an analyst can tell a fingerprinted country from a linguistic guess and
--     filter accordingly.
--
--   * `city_printed` and `city_inferred` are SEPARATE. A city is only a fact
--     when it appeared on the receipt. "Catalan, therefore Barcelona" is a
--     reasonable hunch and a terrible thing to GROUP BY, so the hunch is kept
--     where it cannot be mistaken for an observation.
--
-- Additive only; BigLake Iceberg schema evolution is safe on tables with data
-- and existing rows get NULL. ARRAY<NUMERIC> is verified to work here, both by
-- DDL and by parameterised insert.
--
-- Substitute ${DATASET} before running.

ALTER TABLE `${DATASET}.purchases`
  ADD COLUMN IF NOT EXISTS receipt_language    STRING,
  ADD COLUMN IF NOT EXISTS country_code        STRING,
  ADD COLUMN IF NOT EXISTS region              STRING,
  ADD COLUMN IF NOT EXISTS city_printed        STRING,
  ADD COLUMN IF NOT EXISTS city_inferred       STRING,
  ADD COLUMN IF NOT EXISTS location_confidence STRING,
  ADD COLUMN IF NOT EXISTS location_evidence   STRING,
  ADD COLUMN IF NOT EXISTS store_address       STRING,
  ADD COLUMN IF NOT EXISTS tax_id              STRING,
  ADD COLUMN IF NOT EXISTS tax_rates           ARRAY<NUMERIC>;

-- Verify.
SELECT column_name, data_type
FROM `${DATASET}.INFORMATION_SCHEMA.COLUMNS`
WHERE table_name = 'purchases'
ORDER BY ordinal_position;

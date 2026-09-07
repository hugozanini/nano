-- A dimension table for shops.
--
-- Inferring a store's location on every receipt gives inconsistent answers for
-- the same shop: "Catalonia" on one photo, NULL on the next when the address
-- was cut off. Inconsistency in a dimension poisons every GROUP BY built on it.
--
-- So a store is resolved once, keyed deterministically on its normalised brand
-- and tax id, and reused. That also means the enrichment model is not called
-- again for a shop already seen, and a wrong location can be corrected in one
-- row rather than across every receipt.
--
-- Upserted with MERGE, which BigLake Iceberg tables support.
--
-- Substitute ${DATASET}, ${WAREHOUSE} and ${CONNECTION} before running.

CREATE TABLE IF NOT EXISTS `${DATASET}.stores` (
  store_id            STRING,   -- uuid5 of brand + tax id; stable across receipts
  store_name_original STRING,   -- as printed, e.g. "SUPERMERCAT EXEMPLE, S.L."
  store_brand         STRING,   -- normalised chain, e.g. "Supermercat Exemple"
  tax_id              STRING,
  country_code        STRING,
  region              STRING,
  city_printed        STRING,   -- only ever from an address on a receipt
  city_inferred       STRING,   -- a guess, kept where it cannot be mistaken for fact
  address             STRING,
  location_confidence STRING,
  location_evidence   STRING,
  first_seen_at       TIMESTAMP,
  last_seen_at        TIMESTAMP,
  receipt_count       INT64
)
WITH CONNECTION `us-central1.${CONNECTION}`
OPTIONS (
  table_format = 'ICEBERG',
  file_format  = 'PARQUET',
  storage_uri  = '${WAREHOUSE}/stores'
);

-- Link purchases to the dimension.
ALTER TABLE `${DATASET}.purchases`
  ADD COLUMN IF NOT EXISTS store_id STRING;

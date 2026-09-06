-- Nano lakehouse schema: BigLake tables for Apache Iceberg in BigQuery.
--
-- Open table format (Iceberg metadata + Parquet data files) stored in a Cloud
-- Storage bucket we own, written through plain GoogleSQL and readable by other
-- engines without copying. Substitute ${DATASET} and ${WAREHOUSE} before running:
--
--   prod: DATASET=nano       WAREHOUSE=gs://<bucket>/warehouse
--   test: DATASET=nano_test  WAREHOUSE=gs://<bucket>/warehouse_test
--
-- Both datasets share one BigQuery connection; its service account needs
-- roles/storage.objectAdmin on the bucket to write data files.

CREATE SCHEMA IF NOT EXISTS `${DATASET}`
OPTIONS (location = 'us-central1');

CREATE TABLE IF NOT EXISTS `${DATASET}.purchases` (
  purchase_id       STRING,
  purchased_at      TIMESTAMP,
  store_name        STRING,
  total_amount      NUMERIC,
  currency          STRING,
  receipt_image_uri STRING,
  created_at        TIMESTAMP
)
WITH CONNECTION `us-central1.${CONNECTION}`
OPTIONS (
  table_format = 'ICEBERG',
  file_format  = 'PARQUET',
  storage_uri  = '${WAREHOUSE}/purchases'
);

CREATE TABLE IF NOT EXISTS `${DATASET}.purchase_items` (
  purchase_id     STRING,
  item_name       STRING,
  normalized_name STRING,
  quantity        NUMERIC,
  unit_price      NUMERIC,
  line_total      NUMERIC
)
WITH CONNECTION `us-central1.${CONNECTION}`
OPTIONS (
  table_format = 'ICEBERG',
  file_format  = 'PARQUET',
  storage_uri  = '${WAREHOUSE}/purchase_items'
);

-- Append-only log of every list mutation. Populated on add/remove/close so the
-- Phase 4 shopping-pattern job has something to read.
CREATE TABLE IF NOT EXISTS `${DATASET}.list_events` (
  event_id   STRING,
  event_type STRING,   -- add_item | remove_item | close_list
  item_name  STRING,
  sender     STRING,
  event_ts   TIMESTAMP
)
WITH CONNECTION `us-central1.${CONNECTION}`
OPTIONS (
  table_format = 'ICEBERG',
  file_format  = 'PARQUET',
  storage_uri  = '${WAREHOUSE}/list_events'
);

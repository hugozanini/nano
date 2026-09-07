-- Add a category to purchase_items, and widen normalized_name's role.
--
-- `normalized_name` has only ever been `lower(item_name)`, which is useless when
-- the shopping happens in Catalan and Spanish and the questions are in English.
-- A line printed `POMA GOLDEN 1KG` cannot be found by asking about apples: the
-- words share no letters.
--
--
-- `spend_by_item` matches with LIKE against that column, so "how much on
-- apples?" could never hit `poma golden 1kg`. Item names are canonicalised to
-- an English term at write time, and the category column exists because a
-- supermarket receipt is not all groceries.
--
-- BigLake Iceberg tables support schema evolution, so this is additive and safe
-- to run against a table with data: existing rows get NULL.
--
-- Substitute ${DATASET} before running.

ALTER TABLE `${DATASET}.purchase_items`
  ADD COLUMN IF NOT EXISTS category STRING;

-- Verify.
SELECT column_name, data_type
FROM `${DATASET}.INFORMATION_SCHEMA.COLUMNS`
WHERE table_name = 'purchase_items'
ORDER BY ordinal_position;
-- Add a category to purchase_items, and widen normalized_name's role.
--
-- `normalized_name` has only ever been `lower(item_name)`, which is useless for
-- a household that shops in Catalan and Spanish and asks questions in English:
--
--   item_name              normalized_name          actually
--   MADUIXA                maduixa                  strawberries
--   PASTANAGA 500 G        pastanaga 500 g          carrots
--   GREC NATURAL P-6       grec natural p-6         greek yogurt
--   MAQUINA VENUS START3   maquina venus start3     a razor, not food
--
-- `spend_by_item` matches with LIKE against that column, so "how much on
-- strawberries?" could never hit `maduixa`. Item names are now canonicalised to
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

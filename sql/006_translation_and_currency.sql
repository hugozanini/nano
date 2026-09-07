-- Original and English side by side, and one comparable money column.
--
-- Three item name columns, deliberately different things:
--
--   item_name        LLET SENCERA 1L        as printed, untouched
--   item_name_en     whole milk, 1L         faithful translation
--   normalized_name  milk                   canonical concept, for grouping
--
-- The translation preserves what the receipt said; the canonical name collapses
-- variants so "how much on milk?" reaches every carton however it was labelled.
-- Analysis wants both: one to read, one to group by.
--
-- Currency: a Spain/Brazil household cannot sum EUR and BRL, so every amount
-- also lands in EUR at the rate that applied on the purchase date. The rate and
-- its date are stored alongside, so a 2024 purchase keeps its 2024 rate and the
-- number can be audited rather than trusted.
--
-- Substitute ${DATASET} before running.

ALTER TABLE `${DATASET}.purchase_items`
  ADD COLUMN IF NOT EXISTS item_name_en   STRING,
  ADD COLUMN IF NOT EXISTS line_total_eur NUMERIC;

ALTER TABLE `${DATASET}.purchases`
  ADD COLUMN IF NOT EXISTS amount_eur   NUMERIC,
  ADD COLUMN IF NOT EXISTS fx_rate      NUMERIC,
  ADD COLUMN IF NOT EXISTS fx_rate_date DATE;

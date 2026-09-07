-- The analysis surface: one row per line item, original and English together.
--
-- This is the table to run data science on. Every row carries what the receipt
-- printed, what it means in English, where it was bought, and one comparable
-- money column — with enough provenance to know what is observed and what is
-- inferred.
--
-- Note what it deliberately does NOT do: it never coalesces city_printed and
-- city_inferred into a single `city`. That would recreate exactly the problem
-- the two columns exist to avoid — being unable to tell a printed address from
-- a guess made off the language. Both are exposed, with the confidence beside
-- them, so a query can decide.
--
-- Substitute ${DATASET} before running.

CREATE OR REPLACE VIEW `${DATASET}.v_purchase_items_normalized` AS
SELECT
  p.purchase_id,
  p.purchased_at,
  DATE(p.purchased_at)                      AS purchased_on,
  FORMAT_DATE('%Y-%m', DATE(p.purchased_at)) AS purchased_month,

  -- Where. `city_printed` is a fact; `city_inferred` is a guess; the
  -- confidence and evidence say which conclusions to trust.
  COALESCE(s.store_brand, p.store_name)     AS store_brand,
  p.store_name                              AS store_name_original,
  COALESCE(s.country_code, p.country_code)  AS country_code,
  COALESCE(s.region, p.region)              AS region,
  COALESCE(s.city_printed, p.city_printed)  AS city_printed,
  COALESCE(s.city_inferred, p.city_inferred) AS city_inferred,
  COALESCE(s.location_confidence, p.location_confidence) AS location_confidence,
  COALESCE(s.location_evidence, p.location_evidence)     AS location_evidence,
  p.receipt_language,

  -- What. Three views of the same line: as printed, translated, canonical.
  pi.item_name                              AS item_name_original,
  pi.item_name_en                           AS item_name_english,
  pi.normalized_name                        AS item_canonical,
  pi.category,

  -- How much, in both the original currency and a comparable one.
  pi.quantity,
  pi.unit_price,
  pi.line_total,
  p.currency,
  pi.line_total_eur,
  p.fx_rate,
  p.fx_rate_date,

  p.receipt_image_uri,
  p.store_id
FROM `${DATASET}.purchases` p
JOIN `${DATASET}.purchase_items` pi USING (purchase_id)
LEFT JOIN `${DATASET}.stores` s USING (store_id);


-- Monthly spend by category, in one currency. The obvious first question.
CREATE OR REPLACE VIEW `${DATASET}.v_spend_by_category_month` AS
SELECT
  purchased_month,
  country_code,
  category,
  COUNT(DISTINCT purchase_id) AS trips,
  COUNT(*)                    AS line_items,
  ROUND(SUM(line_total_eur), 2) AS spend_eur
FROM `${DATASET}.v_purchase_items_normalized`
WHERE line_total_eur IS NOT NULL
GROUP BY purchased_month, country_code, category;


-- Every shop seen, with how confident we are about where it is.
CREATE OR REPLACE VIEW `${DATASET}.v_stores` AS
SELECT
  s.store_id,
  s.store_brand,
  s.country_code,
  s.region,
  s.city_printed,
  s.city_inferred,
  s.location_confidence,
  s.location_evidence,
  s.receipt_count,
  s.first_seen_at,
  s.last_seen_at,
  ROUND(SUM(p.amount_eur), 2) AS total_spend_eur
FROM `${DATASET}.stores` s
LEFT JOIN `${DATASET}.purchases` p USING (store_id)
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11;

-- One-off cleanup of duplicate purchases written before the webhook was made
-- idempotent.
--
-- Meta redelivered one receipt seven times over 45 minutes; each delivery ran
-- the whole handler again and wrote a full purchase plus its line items. The
-- result was 7 purchases and 84 item rows for a single EUR 43.06 shopping trip,
-- so every spend query was inflated roughly sevenfold.
--
-- ORDER MATTERS. Run this only *after* the idempotency fix is deployed —
-- cleaning up while Meta is still redelivering simply gets undone.
--
-- The survivor is the earliest `created_at` in each duplicate group, which is
-- the original delivery. Substitute ${DATASET} before running.

-- 1. Inspect first. Nothing here modifies anything.
SELECT
  store_name,
  DATE(purchased_at) AS purchased_on,
  total_amount,
  COUNT(*)           AS copies,
  MIN(created_at)    AS keeping,
  MAX(created_at)    AS newest_duplicate
FROM `${DATASET}.purchases`
GROUP BY store_name, purchased_on, total_amount
HAVING COUNT(*) > 1
ORDER BY copies DESC;

-- 2. Delete the line items belonging to the losing purchases first, so a
--    failure between the two statements leaves orphaned items rather than
--    items pointing at a purchase that no longer exists.
DELETE FROM `${DATASET}.purchase_items`
WHERE purchase_id IN (
  SELECT purchase_id FROM `${DATASET}.purchases` p
  WHERE p.created_at > (
    SELECT MIN(q.created_at) FROM `${DATASET}.purchases` q
    WHERE q.store_name = p.store_name
      AND DATE(q.purchased_at) = DATE(p.purchased_at)
      AND q.total_amount = p.total_amount
  )
);

-- 3. Then the duplicate purchases themselves.
DELETE FROM `${DATASET}.purchases` p
WHERE p.created_at > (
  SELECT MIN(q.created_at) FROM `${DATASET}.purchases` q
  WHERE q.store_name = p.store_name
    AND DATE(q.purchased_at) = DATE(p.purchased_at)
    AND q.total_amount = p.total_amount
);

-- 4. Verify: one row per trip, and item counts that match the receipts.
SELECT
  (SELECT COUNT(*) FROM `${DATASET}.purchases`)      AS purchases,
  (SELECT COUNT(*) FROM `${DATASET}.purchase_items`) AS items,
  (SELECT ROUND(SUM(total_amount), 2) FROM `${DATASET}.purchases`) AS total_spend;

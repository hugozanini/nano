"""The analysis views, against real Iceberg tables.

The views are the deliverable — the surface someone actually runs analysis on —
so they are checked for shape and for the one rule that must not erode: a
printed city and an inferred city stay distinguishable.
"""

from __future__ import annotations

import uuid

import pytest
from google.cloud import bigquery

pytestmark = pytest.mark.integration

EXPECTED_COLUMNS = {
    "purchase_id",
    "purchased_on",
    "purchased_month",
    "store_brand",
    "country_code",
    "region",
    "city_printed",
    "city_inferred",
    "location_confidence",
    "location_evidence",
    "receipt_language",
    "item_name_original",
    "item_name_english",
    "item_canonical",
    "category",
    "line_total",
    "currency",
    "line_total_eur",
}


class TestNormalizedView:
    def test_the_view_exposes_everything_analysis_needs(self, bq, dataset):
        rows = list(
            bq.query(
                f"SELECT column_name FROM `{dataset}.INFORMATION_SCHEMA.COLUMNS` "
                "WHERE table_name = 'v_purchase_items_normalized'"
            ).result()
        )
        columns = {r.column_name for r in rows}
        missing = EXPECTED_COLUMNS - columns
        assert not missing, f"the analysis view is missing {missing}"

    def test_printed_and_inferred_cities_stay_separate(self, bq, dataset):
        """No coalesced `city` column.

        Merging them would recreate exactly the problem the two columns exist to
        avoid: being unable to tell an address read off a receipt from a guess
        made off the language.
        """
        rows = list(
            bq.query(
                f"SELECT column_name FROM `{dataset}.INFORMATION_SCHEMA.COLUMNS` "
                "WHERE table_name = 'v_purchase_items_normalized'"
            ).result()
        )
        columns = {r.column_name for r in rows}
        assert "city_printed" in columns
        assert "city_inferred" in columns
        assert "city" not in columns, "a coalesced city hides whether it was observed"

    def test_the_view_is_queryable_and_joins_correctly(self, bq, dataset, run_id):
        purchase_id = str(uuid.uuid4())
        try:
            bq.query(
                f"""
                INSERT INTO `{dataset}.purchases`
                (purchase_id, purchased_at, store_name, total_amount, currency,
                 country_code, region, city_printed, city_inferred,
                 location_confidence, receipt_language)
                SELECT @pid, TIMESTAMP('2026-08-29'), @store, NUMERIC '4.65', 'EUR',
                       'ES', 'Catalonia', '', 'Barcelona', 'high', 'ca'
                """,
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[
                        bigquery.ScalarQueryParameter("pid", "STRING", purchase_id),
                        bigquery.ScalarQueryParameter("store", "STRING", f"SHOP {run_id}"),
                    ]
                ),
            ).result()
            bq.query(
                f"""
                INSERT INTO `{dataset}.purchase_items`
                (purchase_id, item_name, item_name_en, normalized_name, category,
                 line_total, line_total_eur)
                SELECT @pid, 'LLET SENCERA 1L', 'whole milk, 1L', 'milk', 'dairy',
                       NUMERIC '1.15', NUMERIC '1.15'
                """,
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[bigquery.ScalarQueryParameter("pid", "STRING", purchase_id)]
                ),
            ).result()

            rows = list(
                bq.query(
                    f"""
                    SELECT item_name_original, item_name_english, item_canonical,
                           country_code, region, city_printed, city_inferred,
                           purchased_month, line_total_eur
                    FROM `{dataset}.v_purchase_items_normalized`
                    WHERE purchase_id = @pid
                    """,
                    job_config=bigquery.QueryJobConfig(
                        query_parameters=[
                            bigquery.ScalarQueryParameter("pid", "STRING", purchase_id)
                        ]
                    ),
                ).result()
            )
            assert len(rows) == 1
            row = rows[0]
            # The three names, side by side, which is the whole point.
            assert row.item_name_original == "LLET SENCERA 1L"
            assert row.item_name_english == "whole milk, 1L"
            assert row.item_canonical == "milk"
            assert row.country_code == "ES"
            assert row.purchased_month == "2026-08"
            assert row.city_printed == ""
            assert row.city_inferred == "Barcelona"
        finally:
            for table in ("purchase_items", "purchases"):
                bq.query(
                    f"DELETE FROM `{dataset}.{table}` WHERE purchase_id = @pid",
                    job_config=bigquery.QueryJobConfig(
                        query_parameters=[
                            bigquery.ScalarQueryParameter("pid", "STRING", purchase_id)
                        ]
                    ),
                ).result()


class TestRollupViews:
    def test_category_rollup_is_queryable(self, bq, dataset):
        list(
            bq.query(
                f"SELECT purchased_month, category, spend_eur "
                f"FROM `{dataset}.v_spend_by_category_month` LIMIT 5"
            ).result()
        )

    def test_store_view_is_queryable(self, bq, dataset):
        list(
            bq.query(
                f"SELECT store_brand, receipt_count FROM `{dataset}.v_stores` LIMIT 5"
            ).result()
        )

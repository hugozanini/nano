"""Real Iceberg round-trip against the nano_test dataset.

This is the tier that justifies its own cost. Everything here is behaviour the
fakes cannot reproduce: whether a BigLake Iceberg table is actually readable
immediately after an INSERT, whether the batched `UNNEST` write really works
against a NUMERIC column, and whether DDL assumptions still hold. All four of
those were guesses during the original debugging; here they become assertions.
"""

from __future__ import annotations

import datetime
import uuid

import pytest
from google.cloud import bigquery

pytestmark = pytest.mark.integration


def _cleanup(bq, dataset: str, purchase_id: str) -> None:
    for table in ("purchases", "purchase_items"):
        bq.query(
            f"DELETE FROM `{dataset}.{table}` WHERE purchase_id = @pid",
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("pid", "STRING", purchase_id)]
            ),
        ).result()


class TestGuardrailsAreActive:
    def test_dataset_is_the_test_dataset(self, dataset: str):
        assert dataset.endswith("_test"), f"refusing to run against `{dataset}`"

    def test_firestore_prefix_is_namespaced(self, firestore_prefix: str):
        assert firestore_prefix.startswith("nano_test_")


class TestIcebergWriteReadPath:
    def test_insert_is_immediately_readable(self, bq, dataset: str, run_id: str):
        """Read-after-write on a BigLake Iceberg table.

        The original 0.00-spend bug was blamed in part on Iceberg consistency
        lag. This settles whether that is a real hazard: if this assertion is
        ever flaky, the app needs a retry, and if it is not, the app does not.
        """
        purchase_id = str(uuid.uuid4())
        now = datetime.datetime.now(datetime.UTC)
        try:
            bq.query(
                f"""
                INSERT INTO `{dataset}.purchases`
                (purchase_id, purchased_at, store_name, total_amount,
                 currency, receipt_image_uri, created_at)
                VALUES (@pid, @purchased_at, @store, @total, @cur, @uri, @created)
                """,
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[
                        bigquery.ScalarQueryParameter("pid", "STRING", purchase_id),
                        bigquery.ScalarQueryParameter("purchased_at", "TIMESTAMP", now),
                        bigquery.ScalarQueryParameter("store", "STRING", f"STORE {run_id}"),
                        bigquery.ScalarQueryParameter("total", "NUMERIC", "21.70"),
                        bigquery.ScalarQueryParameter("cur", "STRING", "EUR"),
                        bigquery.ScalarQueryParameter("uri", "STRING", ""),
                        bigquery.ScalarQueryParameter("created", "TIMESTAMP", now),
                    ]
                ),
            ).result()

            rows = list(
                bq.query(
                    f"SELECT total_amount, currency FROM `{dataset}.purchases` "
                    "WHERE purchase_id = @pid",
                    job_config=bigquery.QueryJobConfig(
                        query_parameters=[
                            bigquery.ScalarQueryParameter("pid", "STRING", purchase_id)
                        ]
                    ),
                ).result()
            )
            assert len(rows) == 1, "Iceberg INSERT was not immediately readable"
            assert float(rows[0].total_amount) == 21.70
            assert rows[0].currency == "EUR"
        finally:
            _cleanup(bq, dataset, purchase_id)

    def test_batched_unnest_insert_writes_all_items_in_one_statement(
        self, bq, dataset: str, receipt
    ):
        """The N+1 fix: one statement for every line item, not one per item.

        The production incident issued 13 sequential DML jobs for a 12-item
        receipt and took ~40s. This is the shape that replaces it.
        """
        purchase_id = str(uuid.uuid4())
        rows = [
            {
                "purchase_id": purchase_id,
                "item_name": item["item_name"],
                "normalized_name": item["item_name"].lower(),
                "quantity": str(item["quantity"]),
                "unit_price": str(item["unit_price"]),
                "line_total": str(item["line_total"]),
            }
            for item in receipt.items
        ]

        struct_type = bigquery.StructQueryParameterType(
            bigquery.ScalarQueryParameterType("STRING", name="purchase_id"),
            bigquery.ScalarQueryParameterType("STRING", name="item_name"),
            bigquery.ScalarQueryParameterType("STRING", name="normalized_name"),
            bigquery.ScalarQueryParameterType("NUMERIC", name="quantity"),
            bigquery.ScalarQueryParameterType("NUMERIC", name="unit_price"),
            bigquery.ScalarQueryParameterType("NUMERIC", name="line_total"),
        )
        array_param = bigquery.ArrayQueryParameter(
            "rows",
            struct_type,
            [
                bigquery.StructQueryParameter(
                    None,
                    bigquery.ScalarQueryParameter("purchase_id", "STRING", r["purchase_id"]),
                    bigquery.ScalarQueryParameter("item_name", "STRING", r["item_name"]),
                    bigquery.ScalarQueryParameter(
                        "normalized_name", "STRING", r["normalized_name"]
                    ),
                    bigquery.ScalarQueryParameter("quantity", "NUMERIC", r["quantity"]),
                    bigquery.ScalarQueryParameter("unit_price", "NUMERIC", r["unit_price"]),
                    bigquery.ScalarQueryParameter("line_total", "NUMERIC", r["line_total"]),
                )
                for r in rows
            ],
        )

        try:
            job = bq.query(
                f"""
                INSERT INTO `{dataset}.purchase_items`
                (purchase_id, item_name, normalized_name, quantity, unit_price, line_total)
                SELECT purchase_id, item_name, normalized_name, quantity, unit_price, line_total
                FROM UNNEST(@rows)
                """,
                job_config=bigquery.QueryJobConfig(query_parameters=[array_param]),
            )
            job.result()

            read = list(
                bq.query(
                    f"SELECT COUNT(*) n, ROUND(SUM(line_total), 2) total "
                    f"FROM `{dataset}.purchase_items` WHERE purchase_id = @pid",
                    job_config=bigquery.QueryJobConfig(
                        query_parameters=[
                            bigquery.ScalarQueryParameter("pid", "STRING", purchase_id)
                        ]
                    ),
                ).result()
            )
            assert read[0].n == len(receipt.items)
            assert float(read[0].total) == receipt.total_amount
        finally:
            _cleanup(bq, dataset, purchase_id)

    def test_list_events_accepts_writes(self, bq, dataset: str, run_id: str):
        """`list_events` has never been written to in production; prove the path works."""
        event_id = str(uuid.uuid4())
        try:
            bq.query(
                f"""
                INSERT INTO `{dataset}.list_events`
                (event_id, event_type, item_name, sender, event_ts)
                VALUES (@eid, 'add_item', @item, 'member_primary', CURRENT_TIMESTAMP())
                """,
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[
                        bigquery.ScalarQueryParameter("eid", "STRING", event_id),
                        bigquery.ScalarQueryParameter("item", "STRING", f"coffee {run_id}"),
                    ]
                ),
            ).result()

            rows = list(
                bq.query(
                    f"SELECT event_type, sender FROM `{dataset}.list_events` WHERE event_id = @eid",
                    job_config=bigquery.QueryJobConfig(
                        query_parameters=[bigquery.ScalarQueryParameter("eid", "STRING", event_id)]
                    ),
                ).result()
            )
            assert len(rows) == 1
            assert rows[0].event_type == "add_item"
        finally:
            bq.query(
                f"DELETE FROM `{dataset}.list_events` WHERE event_id = @eid",
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[bigquery.ScalarQueryParameter("eid", "STRING", event_id)]
                ),
            ).result()


class TestFirestoreNamespacing:
    def test_writes_land_under_the_test_prefix_only(
        self, firestore_client, firestore_prefix, run_id
    ):
        """Proves the namespacing works, so the live shared_list is unreachable."""
        collection = f"{firestore_prefix}lists"
        ref = firestore_client.collection(collection).document(run_id)
        try:
            ref.set({"items": ["coffee"]})
            assert ref.get().to_dict() == {"items": ["coffee"]}
            assert collection != "lists"
        finally:
            ref.delete()

    def test_create_is_atomic_and_fails_on_conflict(
        self, firestore_client, firestore_prefix, run_id
    ):
        """The real-client behaviour the dedupe guard depends on.

        The unit tier asserts this against the fake; here it is verified against
        actual Firestore, so the fake is known to be faithful.
        """
        from google.api_core import exceptions

        ref = firestore_client.collection(f"{firestore_prefix}processed_messages").document(run_id)
        try:
            ref.create({"claimed": True})
            with pytest.raises(exceptions.AlreadyExists):
                ref.create({"claimed": True})
        finally:
            ref.delete()

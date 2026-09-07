"""Real-infrastructure checks for the data path.

`list_events` had never been written to in production, and `receipt_image_uri`
had been an empty string on every purchase ever recorded. Both are new code
paths against real services, so both are exercised here rather than trusted.
"""

from __future__ import annotations

import datetime
import uuid

import pytest
from google.cloud import bigquery

pytestmark = pytest.mark.integration


@pytest.fixture
def wired(monkeypatch, project_id, dataset, firestore_prefix):
    from nano import clients, config, events, history, receipts

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", project_id)
    monkeypatch.setenv("BQ_DATASET", dataset)
    monkeypatch.setenv("FIRESTORE_PREFIX", firestore_prefix)
    monkeypatch.setenv("VERIFY_TOKEN", "integration-token")
    monkeypatch.setenv("PHONE_NUMBER_ID", "000000000000000")
    monkeypatch.setenv("HOUSEHOLD_MEMBERS", "15550100001:member_primary")
    monkeypatch.setenv("TASK_BACKEND", "inline")
    monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)

    config.reset_settings_cache()
    clients.reset_clients()
    yield events, history, receipts, config.get_settings()
    config.reset_settings_cache()
    clients.reset_clients()


class TestListEventsAgainstIceberg:
    def test_events_are_written_and_readable(self, wired, bq, dataset, run_id):
        """The table has existed and stayed empty since the bootstrap."""
        events, _, _, settings = wired
        message_id = f"wamid.{run_id}"

        written = events.record(
            events.ADD_ITEM, ["coffee", "milk"], "member_primary", settings, message_id
        )
        assert written == 2

        try:
            rows = list(
                bq.query(
                    f"""
                    SELECT event_type, item_name, sender
                    FROM `{dataset}.list_events`
                    WHERE item_name IN ('coffee', 'milk')
                      AND event_id IN UNNEST(@ids)
                    ORDER BY item_name
                    """,
                    job_config=bigquery.QueryJobConfig(
                        query_parameters=[
                            bigquery.ArrayQueryParameter(
                                "ids",
                                "STRING",
                                [
                                    events._event_id(message_id, events.ADD_ITEM, name, i)
                                    for i, name in enumerate(["coffee", "milk"])
                                ],
                            )
                        ]
                    ),
                ).result()
            )
            assert len(rows) == 2
            assert {r.item_name for r in rows} == {"coffee", "milk"}
            assert all(r.sender == "member_primary" for r in rows)
            assert all(r.event_type == "add_item" for r in rows)
        finally:
            bq.query(
                f"DELETE FROM `{dataset}.list_events` WHERE sender = 'member_primary' "
                f"AND item_name IN ('coffee', 'milk')"
            ).result()

    def test_reprocessing_a_message_reuses_event_ids(self, wired, bq, dataset, run_id):
        """A redelivery must not double-count in the pattern data."""
        events, _, _, settings = wired
        message_id = f"wamid.{run_id}"
        try:
            events.record(events.ADD_ITEM, ["coffee"], "member_primary", settings, message_id)
            events.record(events.ADD_ITEM, ["coffee"], "member_primary", settings, message_id)

            rows = list(
                bq.query(
                    f"""
                    SELECT COUNT(DISTINCT event_id) AS unique_ids, COUNT(*) AS total
                    FROM `{dataset}.list_events` WHERE item_name = 'coffee'
                    """
                ).result()
            )
            # Two inserts, but only one distinct id: the duplicate is
            # identifiable and can be collapsed by the pattern job.
            assert rows[0].unique_ids == 1
        finally:
            bq.query(f"DELETE FROM `{dataset}.list_events` WHERE item_name = 'coffee'").result()


class TestReceiptArchivalAgainstGCS:
    def test_image_uploads_and_the_uri_resolves(self, wired, run_id):
        """`receipt_image_uri` was '' on every purchase ever recorded."""
        from google.cloud import storage

        _, _, receipts, settings = wired
        if not settings.receipts_bucket:
            pytest.skip("RECEIPTS_BUCKET not configured")

        purchase_id = f"itest-{uuid.uuid4()}"
        content = b"\xff\xd8\xff\xe0 not a real jpeg, just bytes"
        uri = receipts.archive_receipt(
            content, "image/jpeg", purchase_id, datetime.date(2026, 8, 29), settings
        )

        assert uri.startswith(f"gs://{settings.receipts_bucket}/receipts/2026/08/")
        blob_path = uri.split(f"{settings.receipts_bucket}/", 1)[1]
        blob = storage.Client().bucket(settings.receipts_bucket).blob(blob_path)
        try:
            assert blob.exists(), "archived object is not readable back"
            assert blob.download_as_bytes() == content
        finally:
            blob.delete()

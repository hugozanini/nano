"""The store dimension against real Iceberg.

MERGE semantics are a platform behaviour, and the upgrade-not-overwrite rule is
the part that matters: a later receipt with a cropped tax table must not blank a
country an earlier one established.
"""

from __future__ import annotations

import pytest
from google.cloud import bigquery
from nano import stores
from nano.locale import HIGH, LOW, LocationInference

pytestmark = pytest.mark.integration


@pytest.fixture
def wired(monkeypatch, project_id, dataset):
    from nano import clients, config

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", project_id)
    monkeypatch.setenv("BQ_DATASET", dataset)
    monkeypatch.setenv("FIRESTORE_PREFIX", "nano_test_")
    monkeypatch.setenv("VERIFY_TOKEN", "integration-token")
    monkeypatch.setenv("PHONE_NUMBER_ID", "000000000000000")
    monkeypatch.setenv("HOUSEHOLD_MEMBERS", "15550100001:member_primary")
    monkeypatch.setenv("TASK_BACKEND", "inline")
    monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)
    config.reset_settings_cache()
    clients.reset_clients()
    yield config.get_settings()
    config.reset_settings_cache()
    clients.reset_clients()


def _cleanup(bq, dataset, store_id):
    bq.query(
        f"DELETE FROM `{dataset}.stores` WHERE store_id = @sid",
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("sid", "STRING", store_id)]
        ),
    ).result()


class TestMergeUpsert:
    def test_a_new_store_is_created_then_updated_not_duplicated(self, wired, bq, dataset, run_id):
        """Two receipts, one dimension row — the whole reason this table exists."""
        settings = wired
        brand = f"SHOP {run_id}"
        rich = LocationInference(
            country_code="ES",
            region="Catalonia",
            language="ca",
            confidence=HIGH,
            evidence=["VAT rates 4/10/21 match ES exactly"],
        )
        store_id = stores.resolve(brand, rich, tax_id="A12345674", settings=settings)
        try:
            stores.resolve(brand, rich, tax_id="A12345674", settings=settings)

            rows = list(
                bq.query(
                    f"SELECT COUNT(*) n, ANY_VALUE(receipt_count) c "
                    f"FROM `{dataset}.stores` WHERE store_id = @sid",
                    job_config=bigquery.QueryJobConfig(
                        query_parameters=[bigquery.ScalarQueryParameter("sid", "STRING", store_id)]
                    ),
                ).result()
            )
            assert rows[0].n == 1, "MERGE must upsert, not append"
            assert rows[0].c == 2, "the second sighting should increment the count"
        finally:
            _cleanup(bq, dataset, store_id)

    def test_a_later_poorer_receipt_does_not_erase_a_known_country(
        self, wired, bq, dataset, run_id
    ):
        """The upgrade-not-overwrite rule, against the real database."""
        settings = wired
        brand = f"SHOP {run_id}"
        known = LocationInference(
            country_code="ES", region="Catalonia", confidence=HIGH, evidence=["VAT match"]
        )
        blank = LocationInference(country_code="", region="", confidence=LOW, evidence=[])

        store_id = stores.resolve(brand, known, tax_id="A12345674", settings=settings)
        try:
            stores.resolve(brand, blank, tax_id="A12345674", settings=settings)

            store = stores.lookup(store_id, settings)
            assert store.country_code == "ES", "a cropped receipt must not blank the country"
            assert store.region == "Catalonia"
        finally:
            _cleanup(bq, dataset, store_id)

    def test_a_later_receipt_fills_a_gap(self, wired, bq, dataset, run_id):
        """A receipt that finally shows an address should add the city."""
        settings = wired
        brand = f"SHOP {run_id}"
        without = LocationInference(country_code="PT", confidence=HIGH, evidence=["VAT match"])
        with_city = LocationInference(
            country_code="PT", city_printed="Lisboa", confidence=HIGH, evidence=["VAT match"]
        )

        store_id = stores.resolve(brand, without, tax_id="123456789", settings=settings)
        try:
            stores.resolve(brand, with_city, tax_id="123456789", settings=settings)
            assert stores.lookup(store_id, settings).city_printed == "Lisboa"
        finally:
            _cleanup(bq, dataset, store_id)

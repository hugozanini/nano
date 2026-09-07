"""The containment, against real IAM and a real BigQuery parser.

The unit tests prove the module's logic against a fake that reports whatever a
test scripts. They cannot prove the two claims that matter most: that BigQuery
really refuses a non-SELECT at dry-run time, and that the analytics identity
really cannot write. Both are properties of the platform, not of our code.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

ANALYTICS_SA = "nano-analytics@nano-grocery-bot.iam.gserviceaccount.com"


@pytest.fixture
def wired(monkeypatch, project_id, dataset):
    from nano import analytics, clients, config

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", project_id)
    monkeypatch.setenv("BQ_DATASET", dataset)
    monkeypatch.setenv("FIRESTORE_PREFIX", "nano_test_")
    monkeypatch.setenv("VERIFY_TOKEN", "integration-token")
    monkeypatch.setenv("PHONE_NUMBER_ID", "000000000000000")
    monkeypatch.setenv("HOUSEHOLD_MEMBERS", "15550100001:member_primary")
    monkeypatch.setenv("TASK_BACKEND", "inline")
    monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)
    monkeypatch.delenv("ANALYTICS_SERVICE_ACCOUNT", raising=False)

    config.reset_settings_cache()
    clients.reset_clients()
    analytics.reset_client()
    yield config.get_settings()
    config.reset_settings_cache()
    clients.reset_clients()
    analytics.reset_client()


class TestBigQueryRefusesBeforeExecuting:
    """A dry run compiles the statement and reports what it is."""

    def test_a_delete_is_identified_as_a_delete(self, wired, dataset):
        from nano import analytics

        ok, reason, _ = analytics.validate(f"DELETE FROM `{dataset}.purchases` WHERE FALSE", wired)
        assert not ok
        assert "only SELECT" in reason

    def test_an_insert_is_identified(self, wired, dataset):
        from nano import analytics

        ok, reason, _ = analytics.validate(
            f"INSERT INTO `{dataset}.purchases` (purchase_id) VALUES ('x')", wired
        )
        assert not ok
        assert "only SELECT" in reason

    def test_a_hallucinated_column_is_caught_before_running(self, wired, dataset):
        from nano import analytics

        ok, reason, _ = analytics.validate(
            f"SELECT no_such_column FROM `{dataset}.v_purchase_items_normalized`", wired
        )
        assert not ok
        assert reason

    def test_a_query_against_another_dataset_is_refused(self, wired):
        """The production dataset must be unreachable from a test run."""
        from nano import analytics

        ok, reason, _ = analytics.validate("SELECT COUNT(*) FROM `nano.purchases`", wired)
        assert not ok
        assert "outside" in reason

    def test_a_legitimate_query_passes(self, wired, dataset):
        from nano import analytics

        ok, reason, _ = analytics.validate(
            f"SELECT store_brand FROM `{dataset}.v_stores` LIMIT 5", wired
        )
        assert ok, reason


class TestTheAnalyticsIdentityCannotWrite:
    """The layer that holds even if every check above is bypassed."""

    @pytest.fixture
    def read_only_client(self, project_id):
        import google.auth
        from google.auth import impersonated_credentials
        from google.cloud import bigquery

        source, _ = google.auth.default()
        try:
            credentials = impersonated_credentials.Credentials(
                source_credentials=source,
                target_principal=ANALYTICS_SA,
                target_scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
            client = bigquery.Client(project=project_id, credentials=credentials)
            list(client.query("SELECT 1").result())
        except Exception as exc:
            pytest.skip(f"cannot impersonate {ANALYTICS_SA}: {exc}")
        return client

    def test_it_can_read_the_analysis_views(self, read_only_client):
        rows = list(
            read_only_client.query(
                "SELECT COUNT(*) n FROM `nano.v_purchase_items_normalized`"
            ).result()
        )
        assert rows[0].n >= 0

    def test_it_cannot_write(self, read_only_client):
        """IAM refuses this regardless of what our validation decides."""
        with pytest.raises(Exception) as caught:
            read_only_client.query("DELETE FROM `nano.purchases` WHERE FALSE").result()
        assert "denied" in str(caught.value).lower()

    def test_it_cannot_reach_the_test_dataset(self, read_only_client):
        with pytest.raises(Exception) as caught:
            list(read_only_client.query("SELECT COUNT(*) FROM `nano_test.purchases`").result())
        assert (
            "denied" in str(caught.value).lower()
            or "not have permission" in str(caught.value).lower()
        )

"""The backlog against real Iceberg: dedupe, counting, and triage."""

from __future__ import annotations

import pytest
from google.cloud import bigquery
from nano import backlog

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


def _cleanup(bq, dataset, request_id):
    bq.query(
        f"DELETE FROM `{dataset}.feature_requests` WHERE request_id = @rid",
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("rid", "STRING", request_id)]
        ),
    ).result()


class TestDemandNotRepetition:
    def test_the_same_request_asked_three_times_is_one_row_counted_three(
        self, wired, bq, dataset, run_id
    ):
        """A backlog ordered by demand is a plan; a list of utterances is not."""
        settings = wired
        text = f"compare prices for {run_id} between shops"
        request_id = backlog.record(text, "member_primary", backlog.UNANSWERED, settings)
        try:
            backlog.record(text.upper(), "member_secondary", backlog.UNANSWERED, settings)
            backlog.record(f"Compare prices for {run_id} between shops?", settings=settings)

            rows = list(
                bq.query(
                    f"SELECT COUNT(*) n, ANY_VALUE(request_count) c "
                    f"FROM `{dataset}.feature_requests` WHERE request_id = @rid",
                    job_config=bigquery.QueryJobConfig(
                        query_parameters=[
                            bigquery.ScalarQueryParameter("rid", "STRING", request_id)
                        ]
                    ),
                ).result()
            )
            assert rows[0].n == 1, "three phrasings should be one row"
            assert rows[0].c == 3, "and should be counted three times"
        finally:
            _cleanup(bq, dataset, request_id)

    def test_an_explicit_ask_promotes_an_inferred_one(self, wired, bq, dataset, run_id):
        """Someone's own words describe the request better than our inference."""
        settings = wired
        text = f"barcode scanning for {run_id}"
        request_id = backlog.record(text, source=backlog.UNANSWERED, settings=settings)
        try:
            backlog.record(text, source=backlog.EXPLICIT, settings=settings)
            rows = list(
                bq.query(
                    f"SELECT source FROM `{dataset}.feature_requests` WHERE request_id = @rid",
                    job_config=bigquery.QueryJobConfig(
                        query_parameters=[
                            bigquery.ScalarQueryParameter("rid", "STRING", request_id)
                        ]
                    ),
                ).result()
            )
            assert rows[0].source == "explicit"
        finally:
            _cleanup(bq, dataset, request_id)


class TestTriage:
    def test_a_request_appears_in_the_backlog_view_then_leaves_it(self, wired, bq, dataset, run_id):
        settings = wired
        request_id = backlog.record(f"something about {run_id}", settings=settings)
        try:
            open_ids = {e["request_id"] for e in backlog.open_requests(200, settings)}
            assert request_id in open_ids

            backlog.set_status(request_id, backlog.DONE, settings=settings)
            open_ids = {e["request_id"] for e in backlog.open_requests(200, settings)}
            assert request_id not in open_ids, "a done request should leave the backlog"
        finally:
            _cleanup(bq, dataset, request_id)

    def test_a_github_url_is_stored(self, wired, bq, dataset, run_id):
        settings = wired
        request_id = backlog.record(f"issue export for {run_id}", settings=settings)
        try:
            backlog.set_status(
                request_id,
                backlog.PLANNED,
                github_issue_url="https://github.com/example/repo/issues/1",
                settings=settings,
            )
            rows = list(
                bq.query(
                    f"SELECT github_issue_url u FROM `{dataset}.feature_requests` "
                    "WHERE request_id = @rid",
                    job_config=bigquery.QueryJobConfig(
                        query_parameters=[
                            bigquery.ScalarQueryParameter("rid", "STRING", request_id)
                        ]
                    ),
                ).result()
            )
            assert rows[0].u.endswith("/issues/1")
        finally:
            _cleanup(bq, dataset, request_id)

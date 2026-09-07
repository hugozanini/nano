"""Telemetry against real Iceberg, including the view that drives the backlog."""

from __future__ import annotations

import pytest
from google.cloud import bigquery
from nano import question_log

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


def _cleanup(bq, dataset, marker):
    bq.query(
        f"DELETE FROM `{dataset}.question_log` WHERE question LIKE @m",
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("m", "STRING", f"%{marker}%")]
        ),
    ).result()


class TestQuestionLogRoundTrip:
    def test_a_question_is_written_and_readable(self, wired, bq, dataset, run_id):
        settings = wired
        try:
            entry = question_log.QuestionRecord(
                question=f"how much did we spend on {run_id}?",
                asked_by="member_primary",
                resolution=question_log.TEMPLATE,
                template_name="total_spend",
                answered=True,
                latency_ms=42,
            )
            assert question_log.record(entry, settings) is True

            rows = list(
                bq.query(
                    f"SELECT resolution, template_name, answered, canonical "
                    f"FROM `{dataset}.question_log` WHERE question_id = @qid",
                    job_config=bigquery.QueryJobConfig(
                        query_parameters=[
                            bigquery.ScalarQueryParameter("qid", "STRING", entry.question_id)
                        ]
                    ),
                ).result()
            )
            assert len(rows) == 1
            assert rows[0].resolution == "template"
            assert rows[0].answered is True
            # The canonical form is normalised, not a substring of the original:
            # it lowercases, splits on punctuation and sorts, so compare against
            # the canonicaliser rather than looking for the raw text.
            assert rows[0].canonical == question_log.canonicalise_question(entry.question)
        finally:
            _cleanup(bq, dataset, run_id)


class TestPatternView:
    def test_a_repeated_unserved_question_becomes_a_template_candidate(
        self, wired, bq, dataset, run_id
    ):
        """The whole point of the telemetry: showing what to build next."""
        settings = wired
        question = f"which shop is cheapest for {run_id}"
        try:
            for _ in range(3):
                question_log.record(
                    question_log.QuestionRecord(
                        question=question,
                        resolution=question_log.UNANSWERABLE,
                        answered=False,
                    ),
                    settings,
                )

            canonical = question_log.canonicalise_question(question)
            rows = list(
                bq.query(
                    f"""
                    SELECT times_asked, unanswered, answered, template_candidate
                    FROM `{dataset}.v_question_patterns` WHERE canonical = @c
                    """,
                    job_config=bigquery.QueryJobConfig(
                        query_parameters=[bigquery.ScalarQueryParameter("c", "STRING", canonical)]
                    ),
                ).result()
            )
            assert len(rows) == 1, "three askings should collapse into one canonical row"
            assert rows[0].times_asked == 3
            assert rows[0].unanswered == 3
            assert rows[0].answered == 0
            assert rows[0].template_candidate is True
        finally:
            _cleanup(bq, dataset, run_id)

    def test_a_question_a_template_already_serves_is_not_a_candidate(
        self, wired, bq, dataset, run_id
    ):
        settings = wired
        question = f"how much did we spend on {run_id}"
        try:
            for _ in range(2):
                question_log.record(
                    question_log.QuestionRecord(
                        question=question,
                        resolution=question_log.TEMPLATE,
                        template_name="total_spend",
                        answered=True,
                    ),
                    settings,
                )

            rows = list(
                bq.query(
                    f"SELECT template_candidate FROM `{dataset}.v_question_patterns` "
                    "WHERE canonical = @c",
                    job_config=bigquery.QueryJobConfig(
                        query_parameters=[
                            bigquery.ScalarQueryParameter(
                                "c", "STRING", question_log.canonicalise_question(question)
                            )
                        ]
                    ),
                ).result()
            )
            assert rows[0].template_candidate is False
        finally:
            _cleanup(bq, dataset, run_id)

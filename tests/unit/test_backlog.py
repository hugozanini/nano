"""A backlog built from demand, not from utterances.

Two sources feed it: an explicit "can you add X", and — more valuable — a
question that could not be answered, captured at the moment it happened. That
second one is the only time anyone reliably reports what is missing, because
nobody files a ticket for a grocery bot.

Deduplication is the point. A backlog ordered by how often something was wanted
is a plan; a list of every utterance is not.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from nano import backlog
from nano.config import load_settings

from tests.fakes.bigquery import FakeBigQueryClient
from tests.fakes.whatsapp import signed, text_message


@pytest.fixture
def settings():
    return load_settings()


@pytest.fixture
def wired(monkeypatch, fake_bq: FakeBigQueryClient):
    monkeypatch.setattr(backlog, "bigquery_client", lambda: fake_bq)
    return fake_bq


@pytest.fixture
def client(nano_app) -> TestClient:
    return TestClient(nano_app)


def post(client: TestClient, payload: dict, app_secret: str):
    body, headers = signed(payload, app_secret)
    return client.post("/webhook", content=body, headers=headers)


class TestDeduplication:
    def test_the_same_request_maps_to_one_id(self):
        first = backlog.request_id_for("cheapest milk shop")
        second = backlog.request_id_for("cheapest milk shop")
        assert first == second

    def test_rephrasings_collapse_to_one_row(self, wired, settings):
        """Demand, not repetition — the reason for storing it at all."""
        a = backlog.record("which shop is cheapest for milk?", "alex", settings=settings)
        b = backlog.record("Which shop is the cheapest for milk", "sam", settings=settings)
        assert a == b

    def test_different_requests_stay_apart(self, wired, settings):
        a = backlog.record("compare prices between shops", settings=settings)
        b = backlog.record("scan barcodes to add items", settings=settings)
        assert a != b

    def test_a_repeat_increments_rather_than_inserts(self, wired, settings):
        backlog.record("cheapest milk", settings=settings)
        sql = " ".join(wired.statements_matching("MERGE")[-1].sql.split())
        assert "request_count = IFNULL(T.request_count, 0) + 1" in sql


class TestRecording:
    def test_an_unanswered_question_is_recorded(self, wired, settings):
        request_id = backlog.record("which shop is cheapest", "alex", backlog.UNANSWERED, settings)
        assert request_id
        merge = wired.statements_matching("MERGE")[-1]
        assert merge.param("source") == "unanswered"
        assert merge.param("requested_by") == "alex"

    def test_an_explicit_request_is_recorded(self, wired, settings):
        backlog.record("barcode scanning", "alex", backlog.EXPLICIT, settings)
        assert wired.statements_matching("MERGE")[-1].param("source") == "explicit"

    def test_an_explicit_ask_outranks_an_inferred_one(self, wired, settings):
        """Someone's own words describe the request better than our inference."""
        backlog.record("barcode scanning", settings=settings)
        sql = " ".join(wired.statements_matching("MERGE")[-1].sql.split())
        assert "IF(@source = 'explicit'" in sql

    def test_empty_text_records_nothing(self, wired, settings):
        assert backlog.record("", settings=settings) == ""
        assert wired.queries == []

    def test_meaningless_text_records_nothing(self, wired, settings):
        """Punctuation and stop words are not product signal."""
        assert backlog.record("?!  the  ", settings=settings) == ""
        assert wired.queries == []

    def test_an_unknown_source_is_refused(self, wired, settings):
        assert backlog.record("something", source="invented", settings=settings) == ""

    def test_a_bigquery_failure_never_raises(self, wired, settings):
        wired.fail("MERGE", RuntimeError("bq down"))
        assert backlog.record("cheapest milk", settings=settings) == ""


class TestReading:
    def test_open_requests_are_returned_most_wanted_first(self, wired, settings):
        wired.when(
            "FROM `nano_test.v_backlog`",
            [
                {
                    "request_id": "a",
                    "text": "compare prices between shops",
                    "status": "new",
                    "source": "unanswered",
                    "request_count": 4,
                    "first_requested_at": "2026-09-01",
                    "last_requested_at": "2026-09-07",
                    "days_open": 6,
                    "github_issue_url": "",
                }
            ],
        )
        entries = backlog.open_requests(settings=settings)
        assert entries[0]["request_count"] == 4
        sql = " ".join(wired.statements_matching("FROM `nano_test.v_backlog`")[-1].sql.split())
        assert "ORDER BY request_count DESC" in sql

    def test_a_read_failure_returns_nothing_rather_than_raising(self, wired, settings):
        wired.fail("FROM `nano_test.v_backlog`", RuntimeError("bq down"))
        assert backlog.open_requests(settings=settings) == []


class TestStatus:
    def test_a_request_can_be_triaged(self, wired, settings):
        assert backlog.set_status("abc", backlog.PLANNED, settings=settings) is True
        update = wired.statements_matching("UPDATE")[-1]
        assert update.param("status") == "planned"

    def test_an_unknown_status_is_refused(self, wired, settings):
        assert backlog.set_status("abc", "invented", settings=settings) is False
        assert wired.queries == []

    def test_a_github_url_can_be_attached(self, wired, settings):
        backlog.set_status(
            "abc",
            backlog.PLANNED,
            github_issue_url="https://github.com/x/y/issues/1",
            settings=settings,
        )
        assert "issues/1" in wired.statements_matching("UPDATE")[-1].param("url")


class TestChatFormatting:
    def test_an_empty_backlog_reads_naturally(self):
        assert "Nothing in the backlog" in backlog.format_for_chat([])

    def test_counts_are_shown_only_when_repeated(self):
        text = backlog.format_for_chat(
            [
                {"text": "compare prices", "request_count": 3, "status": "new"},
                {"text": "barcode scanning", "request_count": 1, "status": "new"},
            ]
        )
        assert "compare prices (asked 3 times)" in text
        assert "barcode scanning" in text
        assert "barcode scanning (asked" not in text

    def test_a_non_new_status_is_shown(self):
        text = backlog.format_for_chat(
            [{"text": "compare prices", "request_count": 1, "status": "planned"}]
        )
        assert "[planned]" in text


class TestEndToEnd:
    def test_an_unanswerable_question_reaches_the_backlog(
        self, client, app_secret, fake_gemini, fake_bq
    ):
        fake_gemini.queue_function_call(
            "cannot_answer", request="set a reminder to shop on Fridays"
        )
        post(client, text_message("remind me to shop Fridays", message_id="wamid.B1"), app_secret)

        merges = fake_bq.statements_matching("MERGE `nano_test.feature_requests`")
        assert len(merges) == 1
        assert merges[0].param("source") == "unanswered"

    def test_an_explicit_request_reaches_the_backlog(
        self, client, app_secret, fake_gemini, fake_bq
    ):
        fake_gemini.queue_function_call("request_feature", request="barcode scanning")
        post(client, text_message("can you add barcodes?", message_id="wamid.B2"), app_secret)

        merges = fake_bq.statements_matching("MERGE `nano_test.feature_requests`")
        assert merges[0].param("source") == "explicit"

    def test_an_unclear_message_records_nothing(self, client, app_secret, fake_gemini, fake_bq):
        """A typo is not a feature request; logging it would be noise."""
        fake_gemini.queue_function_call("unclear")
        post(client, text_message("asdkjh", message_id="wamid.B3"), app_secret)
        assert fake_bq.statements_matching("MERGE `nano_test.feature_requests`") == []

    def test_the_backlog_can_be_read_back_in_chat(
        self, client, app_secret, fake_gemini, fake_bq, fake_whatsapp
    ):
        fake_bq.when(
            "FROM `nano_test.v_backlog`",
            [
                {
                    "request_id": "a",
                    "text": "compare prices between shops",
                    "status": "new",
                    "source": "unanswered",
                    "request_count": 2,
                    "first_requested_at": "2026-09-01",
                    "last_requested_at": "2026-09-07",
                    "days_open": 6,
                    "github_issue_url": "",
                }
            ],
        )
        fake_gemini.queue_function_call("list_requests")
        post(client, text_message("what have we asked for?", message_id="wamid.B4"), app_secret)

        reply = fake_whatsapp.texts[-1]
        assert "compare prices between shops" in reply
        assert "asked 2 times" in reply

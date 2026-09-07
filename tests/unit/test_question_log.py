"""Telemetry that says what to build next, and never costs an answer.

Nano answers common questions from hand-written templates and everything else
from generated SQL. Knowing which open questions recur is what tells us when one
has earned a template, and knowing which were never answered is the most direct
product signal available.
"""

from __future__ import annotations

import pytest
from nano import question_log
from nano.config import load_settings

from tests.fakes.bigquery import FakeBigQueryClient


@pytest.fixture
def settings():
    return load_settings()


@pytest.fixture
def wired(monkeypatch, fake_bq: FakeBigQueryClient):
    monkeypatch.setattr(question_log, "bigquery_client", lambda: fake_bq)
    return fake_bq


class TestCanonicalisation:
    """Grouping decides the telemetry's usefulness, so it is deterministic."""

    def test_rephrasings_group_together(self):
        a = question_log.canonicalise_question("How much did we spend on milk?")
        b = question_log.canonicalise_question("how much do we spend on milk")
        assert a == b

    def test_punctuation_and_case_are_irrelevant(self):
        assert question_log.canonicalise_question(
            "What's the MAIN place we do groceries?!"
        ) == question_log.canonicalise_question("what is the main place we do groceries")

    def test_genuinely_different_questions_stay_apart(self):
        milk = question_log.canonicalise_question("how much did we spend on milk")
        bread = question_log.canonicalise_question("how much did we spend on bread")
        assert milk != bread

    def test_word_order_does_not_split_a_question(self):
        assert question_log.canonicalise_question(
            "milk price trend"
        ) == question_log.canonicalise_question("trend price milk")

    def test_empty_input_is_safe(self):
        assert question_log.canonicalise_question("") == ""
        assert question_log.canonicalise_question("?!") == ""

    def test_it_is_deterministic(self):
        """A grouping that shifts between runs makes the telemetry worthless."""
        text = "How much did we spend on strawberries last month?"
        assert len({question_log.canonicalise_question(text) for _ in range(10)}) == 1


class TestRecording:
    def test_a_template_answer_is_recorded(self, wired, settings):
        entry = question_log.QuestionRecord(
            question="how much did we spend?",
            asked_by="alex",
            resolution=question_log.TEMPLATE,
            template_name="total_spend",
            answered=True,
        )
        assert question_log.record(entry, settings) is True

        insert = wired.statements_matching("INSERT INTO `nano_test.question_log`")[-1]
        assert insert.param("resolution") == "template"
        assert insert.param("template_name") == "total_spend"
        assert insert.param("answered") is True
        assert insert.param("canonical") == question_log.canonicalise_question(
            "how much did we spend?"
        )

    def test_an_unanswerable_question_is_recorded(self, wired, settings):
        entry = question_log.QuestionRecord(
            question="which shop is cheapest?",
            resolution=question_log.UNANSWERABLE,
            answered=False,
        )
        question_log.record(entry, settings)
        insert = wired.statements_matching("INSERT INTO `nano_test.question_log`")[-1]
        assert insert.param("resolution") == "unanswerable"
        assert insert.param("answered") is False

    def test_an_unknown_resolution_is_refused(self, wired, settings):
        entry = question_log.QuestionRecord(question="x", resolution="invented")
        assert question_log.record(entry, settings) is False
        assert wired.queries == []

    def test_long_text_is_truncated_rather_than_rejected(self, wired, settings):
        entry = question_log.QuestionRecord(
            question="x" * 5000,
            resolution=question_log.TEMPLATE,
            generated_sql="y" * 20000,
        )
        question_log.record(entry, settings)
        insert = wired.statements_matching("INSERT INTO `nano_test.question_log`")[-1]
        assert len(insert.param("question")) == 1000
        assert len(insert.param("generated_sql")) == 8000

    def test_a_bigquery_failure_never_raises(self, wired, settings):
        """Telemetry must not cost the user their answer."""
        wired.fail("INSERT INTO `nano_test.question_log`", RuntimeError("bq down"))
        entry = question_log.QuestionRecord(
            question="x", resolution=question_log.TEMPLATE, answered=True
        )
        assert question_log.record(entry, settings) is False


class TestTracking:
    def test_the_context_manager_records_and_times(self, wired, settings):
        with question_log.track("how much?", "alex", settings) as entry:
            entry.resolution = question_log.TEMPLATE
            entry.template_name = "total_spend"
            entry.answered = True

        insert = wired.statements_matching("INSERT INTO `nano_test.question_log`")[-1]
        assert insert.param("template_name") == "total_spend"
        assert insert.param("latency_ms") >= 0

    def test_an_exception_is_recorded_as_a_failure_and_re_raised(self, wired, settings):
        """Telemetry should reflect what happened, not only the happy path."""
        with (
            pytest.raises(RuntimeError, match="boom"),
            question_log.track("how much?", "alex", settings) as entry,
        ):
            entry.resolution = question_log.TEMPLATE
            raise RuntimeError("boom")

        insert = wired.statements_matching("INSERT INTO `nano_test.question_log`")[-1]
        assert insert.param("resolution") == "failed"
        assert insert.param("answered") is False
        assert "boom" in insert.param("failure_reason")

    def test_a_telemetry_failure_does_not_mask_the_real_error(self, wired, settings):
        wired.fail("INSERT INTO `nano_test.question_log`", RuntimeError("bq down"))
        with (
            pytest.raises(ValueError, match="the real problem"),
            question_log.track("q", "alex", settings),
        ):
            raise ValueError("the real problem")


class TestTemplateCandidates:
    def test_recurring_unserved_questions_are_returned(self, wired, settings):
        wired.when(
            "WHERE template_candidate",
            [
                {
                    "canonical": "groceries main place",
                    "example_question": "What is the main place we do groceries?",
                    "times_asked": 4,
                    "unanswered": 4,
                    "served_by_generated_sql": 0,
                    "last_asked": "2026-09-07",
                }
            ],
        )
        candidates = question_log.template_candidates(settings=settings)
        assert candidates[0]["times_asked"] == 4
        assert "main place" in candidates[0]["example_question"]

    def test_a_query_failure_returns_nothing_rather_than_raising(self, wired, settings):
        wired.fail("WHERE template_candidate", RuntimeError("bq down"))
        assert question_log.template_candidates(settings=settings) == []

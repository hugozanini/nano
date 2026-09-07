"""Giving a language model SQL access, safely.

Every test in `TestValidation` is a containment layer. They matter more than the
feature does: the worst outcome here is not a failed answer, it is a confidently
wrong one about money, or a generated statement that changes data.

The strongest layer is not tested here at all, because it is not ours — the
queries run as a read-only identity, and IAM refuses a write regardless of what
this module decides. That is verified in the integration tier.
"""

from __future__ import annotations

import pytest
from nano import analytics
from nano.config import load_settings

from tests.fakes.bigquery import FakeBigQueryClient
from tests.fakes.gemini import FakeGeminiModel


@pytest.fixture
def settings():
    return load_settings()


@pytest.fixture
def wired(monkeypatch, fake_bq: FakeBigQueryClient):
    monkeypatch.setattr(analytics, "analytics_client", lambda: fake_bq)
    return fake_bq


class TestStripFences:
    def test_markdown_fences_are_removed(self):
        assert analytics.strip_fences("```sql\nSELECT 1\n```") == "SELECT 1"

    def test_a_trailing_semicolon_is_removed(self):
        """A semicolon invites a second statement; there is never a need for one."""
        assert analytics.strip_fences("SELECT 1;") == "SELECT 1"

    def test_plain_sql_is_untouched(self):
        assert analytics.strip_fences("SELECT 1") == "SELECT 1"

    def test_empty_input_is_safe(self):
        assert analytics.strip_fences("") == ""


class TestValidation:
    """Each of these is a way the generated SQL could go wrong."""

    def test_a_plain_select_is_allowed(self, wired, settings):
        ok, reason, _ = analytics.validate("SELECT 1 FROM `nano_test.v_stores`", settings)
        assert ok, reason

    def test_a_delete_is_refused(self, wired, settings):
        """Refused by BigQuery's own parser, not by scanning the text.

        Scanning for the word DELETE is trivially evaded; `statement_type` is
        what the query actually is.
        """
        wired.dry_run_reports(statement_type="DELETE")
        ok, reason, _ = analytics.validate("DELETE FROM `nano_test.purchases`", settings)
        assert not ok
        assert "only SELECT" in reason

    @pytest.mark.parametrize(
        "statement_type", ["INSERT", "UPDATE", "MERGE", "CREATE_TABLE", "DROP_TABLE", "SCRIPT"]
    )
    def test_every_non_select_statement_is_refused(self, wired, settings, statement_type):
        wired.dry_run_reports(statement_type=statement_type)
        ok, _, _ = analytics.validate("-- whatever", settings)
        assert not ok

    def test_a_query_reaching_another_dataset_is_refused(self, wired, settings):
        wired.dry_run_reports(tables=[("some_other_dataset", "secrets")])
        ok, reason, _ = analytics.validate("SELECT 1", settings)
        assert not ok
        assert "outside nano_test" in reason

    def test_a_query_touching_one_allowed_and_one_forbidden_table_is_refused(self, wired, settings):
        wired.dry_run_reports(
            tables=[("nano_test", "purchases"), ("other", "elsewhere")],
        )
        ok, _, _ = analytics.validate("SELECT 1", settings)
        assert not ok

    def test_an_oversized_scan_is_refused(self, wired, settings):
        wired.dry_run_reports(total_bytes=settings.analytics_max_bytes + 1)
        ok, reason, _ = analytics.validate("SELECT 1", settings)
        assert not ok
        assert "over the" in reason

    def test_invalid_sql_is_refused_with_the_reason(self, wired, settings):
        wired.dry_run_reports(error=RuntimeError("Unrecognized name: no_such_column"))
        ok, reason, _ = analytics.validate("SELECT no_such_column FROM x", settings)
        assert not ok
        assert "no_such_column" in reason

    def test_empty_sql_is_refused(self, wired, settings):
        ok, reason, _ = analytics.validate("   ", settings)
        assert not ok
        assert "no SQL" in reason

    def test_validation_never_executes_anything(self, wired, settings):
        """A dry run compiles the query; it must not run it."""
        analytics.validate("SELECT 1 FROM `nano_test.v_stores`", settings)
        assert wired.dry_runs, "the validation should have dry-run the query"
        executed = [q for q in wired.queries if q.sql not in wired.dry_runs]
        assert executed == [], "nothing should have executed during validation"


class TestLimits:
    def test_a_missing_limit_is_added(self):
        sql = analytics._ensure_limit("SELECT store_brand FROM `nano.v_stores`")
        assert f"LIMIT {analytics.MAX_ROWS}" in sql

    def test_an_existing_limit_is_respected(self):
        sql = analytics._ensure_limit("SELECT 1 FROM t LIMIT 5")
        assert sql.count("LIMIT") == 1
        assert "LIMIT 5" in sql

    def test_execution_applies_a_cost_ceiling(self, wired, settings):
        """BigQuery enforces this, so a runaway query costs nothing."""
        analytics.execute("SELECT 1 FROM `nano_test.v_stores`", settings)
        assert wired.queries[-1].maximum_bytes_billed == settings.analytics_max_bytes

    def test_execution_is_not_a_dry_run(self, wired, settings):
        analytics.execute("SELECT 1 FROM `nano_test.v_stores`", settings)
        assert wired.queries[-1].dry_run is False

    def test_validation_is_a_dry_run(self, wired, settings):
        analytics.validate("SELECT 1 FROM `nano_test.v_stores`", settings)
        assert wired.queries[-1].dry_run is True


class TestAnswering:
    def _model(self, sql: str, summary: str) -> FakeGeminiModel:
        return FakeGeminiModel().queue_text(sql).queue_text(summary)

    def test_a_question_is_answered_from_generated_sql(self, wired, settings):
        wired.when("SELECT store_brand", [{"store_brand": "Shop A", "trips": 4}])
        model = self._model(
            "SELECT store_brand, COUNT(*) trips FROM `nano_test.v_stores` GROUP BY 1",
            "You shop most at Shop A — 4 trips.",
        )
        result = analytics.answer("where do we shop most?", settings=settings, model=model)

        assert result.succeeded
        assert "Shop A" in result.answer
        assert result.row_count == 1

    def test_the_sql_is_returned_for_the_telemetry(self, wired, settings):
        wired.when("SELECT", [{"n": 1}])
        model = self._model("SELECT 1 n FROM `nano_test.v_stores`", "One.")
        result = analytics.answer("how many?", settings=settings, model=model)
        assert "SELECT 1 n" in result.sql

    def test_rejected_sql_is_retried_once_with_the_error(self, wired, settings):
        """Most rejections are a wrong column name, which the model can fix."""
        model = FakeGeminiModel()
        model.queue_text("SELECT bad_column FROM `nano_test.v_stores`")
        model.queue_text("SELECT store_brand FROM `nano_test.v_stores`")
        model.queue_text("Shop A.")

        calls = {"n": 0}
        original = analytics.validate

        def flaky(sql, settings=None):
            calls["n"] += 1
            if calls["n"] == 1:
                return False, "Unrecognized name: bad_column", 0
            return original(sql, settings)

        analytics.validate = flaky
        try:
            wired.when("SELECT store_brand", [{"store_brand": "Shop A"}])
            result = analytics.answer("which shop?", settings=settings, model=model)
        finally:
            analytics.validate = original

        assert result.succeeded
        assert "bad_column" not in result.sql
        assert "bad_column" in model.calls[1].parts[0], "the error should be fed back"

    def test_two_failed_attempts_give_an_honest_answer(self, wired, settings):
        wired.dry_run_reports(statement_type="DELETE")
        model = FakeGeminiModel().queue_text("DELETE FROM x").queue_text("DELETE FROM x")
        result = analytics.answer("drop everything", settings=settings, model=model)

        assert not result.succeeded
        assert "couldn't work that one out" in result.answer
        assert "validation failed" in result.failure_reason

    def test_no_rows_still_produces_an_answer(self, wired, settings):
        wired.when("SELECT", [])
        model = self._model(
            "SELECT 1 FROM `nano_test.v_stores`", "I couldn't find anything matching that."
        )
        result = analytics.answer("spending on caviar?", settings=settings, model=model)
        assert result.succeeded
        assert result.row_count == 0


class TestFailureIsAlwaysSurvivable:
    def test_a_model_outage_gives_an_answer_not_an_exception(self, wired, settings):
        model = FakeGeminiModel().raises(RuntimeError("vertex down"))
        result = analytics.answer("anything", settings=settings, model=model)
        assert not result.succeeded
        assert result.answer

    def test_an_execution_failure_is_survivable(self, wired, settings):
        wired.fail("SELECT store_brand", RuntimeError("bq exploded"))
        model = FakeGeminiModel().queue_text("SELECT store_brand FROM `nano_test.v_stores`")
        result = analytics.answer("which shop?", settings=settings, model=model)
        assert not result.succeeded
        assert "execution failed" in result.failure_reason

    def test_an_empty_summary_is_reported_rather_than_sent(self, wired, settings):
        wired.when("SELECT", [{"n": 1}])
        model = FakeGeminiModel().queue_text("SELECT 1 n FROM `nano_test.v_stores`")
        model.queue_text("")
        result = analytics.answer("how many?", settings=settings, model=model)
        assert not result.succeeded
        assert result.answer


class TestPromptsEncodeTheRules:
    """The prompt is the specification; the model follows it literally."""

    def test_the_schema_only_mentions_views(self):
        for view in analytics.ANALYSIS_VIEWS:
            assert view in analytics.SCHEMA
        assert "purchase_items`" not in analytics.SCHEMA
        assert "`{dataset}.purchases`" not in analytics.SCHEMA

    def test_the_prompt_forbids_dml(self):
        assert "No DML" in analytics.SQL_PROMPT
        assert "ONE SELECT statement" in analytics.SQL_PROMPT

    def test_the_prompt_steers_grouping_to_the_canonical_name(self):
        """Grouping on the printed name would split milk across languages."""
        assert "item_canonical" in analytics.SQL_PROMPT

    def test_the_prompt_steers_money_comparisons_to_eur(self):
        assert "line_total_eur" in analytics.SQL_PROMPT

    def test_the_prompt_protects_the_inferred_city_distinction(self):
        assert "city_inferred" in analytics.SQL_PROMPT

    def test_the_answer_prompt_demands_honesty_about_thin_data(self):
        """The worst outcome is a confident trend inferred from one purchase."""
        assert "too thin" in analytics.ANSWER_PROMPT
        assert "guessing is not" in analytics.ANSWER_PROMPT


class TestMoneyIsReportedCorrectly:
    """A spending assistant that reports euros as dollars is broken.

    Observed against real data: asked what strawberries cost, the generated SQL
    returned an unnamed column holding `3.11`, so the summariser had no currency
    to go on and reported "$3.11" for a euro purchase. The fix is in both
    prompts — name the columns, and never invent a symbol.
    """

    def test_the_sql_prompt_requires_named_columns(self):
        assert "Alias EVERY selected expression" in analytics.SQL_PROMPT
        assert "f0_" in analytics.SQL_PROMPT, "the failure mode should be named"

    def test_the_sql_prompt_requires_the_unit_to_be_visible(self):
        assert "AS total_eur" in analytics.SQL_PROMPT
        assert "also select `currency`" in analytics.SQL_PROMPT

    def test_the_answer_prompt_forbids_inventing_a_currency(self):
        assert "_eur` is euros" in analytics.ANSWER_PROMPT
        assert "Never introduce a currency symbol" in analytics.ANSWER_PROMPT

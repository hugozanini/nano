"""Answering "what is this thing on my receipt?" from what is already stored.

From the live chat, immediately after Nano listed a purchase in English:

    You:  What is family pack breast? Chicken breast?
    Nano: I can't work that out yet — you're asking about what family pack
          breast is … I've noted it as a feature request.

Every fact needed was already in BigQuery: `PIT FAM.` printed, "family pack
breast" translated, `meat` categorised, EUR 7.49 charged. The question was
asked *because* Nano's own translation was unclear, so answering "I can't"
withheld data it had — and filed a capability gap that does not exist, which
is worse than silence, since the backlog is meant to record what is genuinely
missing.

What it must still not do is answer the question that was actually asked.
"Chicken breast?" cannot be confirmed from `PIT FAM.`, and saying yes would be
the same class of error as reporting euros as dollars.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from nano import history
from nano.config import load_settings

from tests.fakes.bigquery import FakeBigQueryClient


@pytest.fixture
def settings():
    return load_settings()


@pytest.fixture
def wired_history(monkeypatch, fake_bq: FakeBigQueryClient):
    monkeypatch.setattr(history, "bigquery_client", lambda: fake_bq)
    return fake_bq


def row(**overrides) -> dict:
    base = {
        "item_name": "PIT FAM.",
        "item_name_en": "family pack breast",
        "category": "meat",
        "line_total": Decimal("7.49"),
        "purchased_at": datetime.datetime(2026, 8, 29),
        "currency": "EUR",
        "store_brand": "Shop A",
        "match_count": 1,
    }
    base.update(overrides)
    return base


def explain(wired, item="breast", display="family pack breast", settings=None, rows=None):
    wired.when("pi.normalized_name", list(rows if rows is not None else [row()]))
    return history.answer_history_query(
        "explain_item", item_name=item, settings=settings, display_name=display
    )


class TestItAnswersFromWhatIsStored:
    def test_it_names_the_printed_text_and_the_translation(self, wired_history, settings):
        answer = explain(wired_history, settings=settings)
        assert "PIT FAM." in answer
        assert "family pack breast" in answer

    def test_it_gives_the_category_readably(self, wired_history, settings):
        answer = explain(wired_history, settings=settings)
        assert "meat" in answer

    def test_an_underscored_category_is_not_shown_raw(self, wired_history, settings):
        answer = explain(wired_history, rows=[row(category="personal_care")], settings=settings)
        assert "personal care" in answer
        assert "personal_care" not in answer

    def test_it_gives_the_price_and_where_it_came_from(self, wired_history, settings):
        answer = explain(wired_history, settings=settings)
        assert "EUR 7.49" in answer
        assert "Shop A" in answer
        assert "29 Aug" in answer

    def test_it_is_not_a_feature_request(self, wired_history, settings):
        """The exact failure: a lookup answered as a missing capability."""
        answer = explain(wired_history, settings=settings)
        assert "feature request" not in answer
        assert "can't work that out" not in answer


class TestItDoesNotGuessBeyondTheReceipt:
    def test_it_says_where_the_receipt_stops(self, wired_history, settings):
        """ "Chicken breast?" is unanswerable from `PIT FAM.` — so say so."""
        answer = explain(wired_history, settings=settings)
        assert "can't confirm more" in answer

    def test_it_never_invents_the_species(self, wired_history, settings):
        answer = explain(wired_history, settings=settings)
        assert "chicken" not in answer.lower(), (
            "the receipt says PIT FAM. and nothing about the animal"
        )

    def test_an_untranslated_item_gets_no_caveat(self, wired_history, settings):
        """Nothing was interpreted, so there is nothing to qualify."""
        answer = explain(
            wired_history,
            rows=[row(item_name="BANANA", item_name_en="banana", category="produce")],
            item="banana",
            display="banana",
            settings=settings,
        )
        assert "can't confirm more" not in answer
        assert "BANANA" in answer

    def test_a_missing_translation_is_survivable(self, wired_history, settings):
        answer = explain(wired_history, rows=[row(item_name_en=None)], settings=settings)
        assert "PIT FAM." in answer


class TestWhenItIsNotThere:
    def test_a_miss_is_reported_as_a_miss(self, wired_history, settings):
        answer = explain(wired_history, rows=[], display="caviar", settings=settings)
        assert "caviar" in answer
        assert "don't have anything matching" in answer

    def test_a_miss_is_not_filed_as_a_missing_feature(self, wired_history, settings):
        """A lookup that finds nothing is not a capability gap."""
        answer = explain(wired_history, rows=[], display="caviar", settings=settings)
        assert "feature request" not in answer

    def test_no_item_name_asks_which_one(self, wired_history, settings):
        answer = history.answer_history_query("explain_item", settings=settings)
        assert "Which item" in answer


class TestRepeatPurchases:
    def test_buying_it_repeatedly_is_mentioned(self, wired_history, settings):
        answer = explain(wired_history, rows=[row(match_count=4)], settings=settings)
        assert "4 times" in answer

    def test_a_single_purchase_is_not_counted_out_loud(self, wired_history, settings):
        answer = explain(wired_history, settings=settings)
        assert "1 times" not in answer


class TestTheIntentIsReachable:
    def test_explain_item_is_an_offered_query_type(self):
        from nano import gemini

        spec = next(s for s in gemini.INTENT_SPECS if s["name"] == "query_history")
        assert "explain_item" in spec["parameters"]["properties"]["query_type"]["enum"]

    def test_the_description_rules_out_cannot_answer(self):
        from nano import gemini

        spec = next(s for s in gemini.INTENT_SPECS if s["name"] == "query_history")
        described = spec["parameters"]["properties"]["query_type"]["description"]
        assert "never a cannot_answer" in described

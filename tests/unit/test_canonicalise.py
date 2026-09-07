"""Cross-language item matching.

The shape of the problem, as it appears on a Catalan supermarket receipt:

    item_name              normalized_name          actually
    POMA GOLDEN 1KG        poma golden 1kg          apples
    ARROS RODO 1KG         arros rodo 1kg           rice
    IOGURT NATURAL P-4     iogurt natural p-4       yogurt
    OUS M-12               ous m-12                 eggs
    PILES AAA P-4          piles aaa p-4            batteries, not food

`spend_by_item` matches `normalized_name` with LIKE, so "how much did we spend
on apples?" could never match `poma golden 1kg` — the words share no letters.
"""

from __future__ import annotations

import pytest
from nano import canonicalise

from tests.fakes.gemini import FakeGeminiModel


class TestFallbackNormalise:
    """The deterministic path, used whenever the model is unavailable.

    It cannot solve cross-language matching — nothing deterministic can — but it
    is stable, free, and strictly better than the `lower()` it replaces.
    """

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("ARROS RODO 1KG", "arros rodo"),
            ("IOGURT NATURAL P-4", "iogurt natural"),
            ("LLET SENCERA 1L", "llet sencera"),
            ("OUS M-12", "ous m"),
            ("  POMA GOLDEN 1KG  ", "poma golden"),
            ("PILES AAA P-4", "piles aaa"),
        ],
    )
    def test_sizes_and_punctuation_are_stripped(self, raw, expected):
        assert canonicalise.fallback_normalise(raw) == expected

    def test_accents_are_folded(self):
        """`dolç` and `dolc` must not be different products."""
        assert canonicalise.fallback_normalise("PA DOLÇ") == canonicalise.fallback_normalise(
            "PA DOLC"
        )

    def test_empty_input_is_safe(self):
        assert canonicalise.fallback_normalise("") == ""


class TestCanonicaliseItems:
    def test_maps_catalan_lines_to_english_terms(self):
        """The whole point: a question in English reaching a receipt in Catalan."""
        model = FakeGeminiModel().queue_json(
            [
                {"canonical": "apple", "category": "produce"},
                {"canonical": "rice", "category": "produce"},
                {"canonical": "yogurt", "category": "dairy"},
                {"canonical": "batteries", "category": "personal_care"},
            ]
        )
        result = canonicalise.canonicalise_items(
            ["POMA GOLDEN 1KG", "ARROS RODO 1KG", "IOGURT NATURAL P-4", "PILES AAA P-4"],
            model=model,
        )
        assert [r["canonical"] for r in result] == [
            "apple",
            "rice",
            "yogurt",
            "batteries",
        ]

    def test_a_non_grocery_item_gets_a_non_food_category(self):
        """A batteries should not land in the food budget."""
        model = FakeGeminiModel().queue_json(
            [{"canonical": "batteries", "category": "personal_care"}]
        )
        result = canonicalise.canonicalise_items(["PILES AAA P-4"], model=model)
        assert result[0]["category"] == "personal_care"

    def test_one_call_for_the_whole_receipt(self):
        model = FakeGeminiModel().queue_json([{"canonical": "milk", "category": "dairy"}] * 12)
        canonicalise.canonicalise_items([f"ITEM {i}" for i in range(12)], model=model)
        assert model.call_count == 1, "canonicalisation must be batched, not per item"

    def test_unknown_categories_become_other(self):
        model = FakeGeminiModel().queue_json(
            [{"canonical": "milk", "category": "invented-category"}]
        )
        assert canonicalise.canonicalise_items(["LLET"], model=model)[0]["category"] == "other"

    def test_fenced_json_is_tolerated(self):
        model = FakeGeminiModel().queue_text(
            '```json\n[{"canonical": "milk", "category": "dairy"}]\n```'
        )
        assert canonicalise.canonicalise_items(["LLET"], model=model)[0]["canonical"] == "milk"


class TestCanonicalisationDegradesSafely:
    """A canonicalisation failure must never lose the receipt."""

    def test_a_model_error_falls_back_deterministically(self):
        model = FakeGeminiModel().raises(RuntimeError("vertex unavailable"))
        result = canonicalise.canonicalise_items(["ARROS RODO 1KG"], model=model)
        assert result == [{"canonical": "arros rodo", "category": "other"}]

    def test_a_wrong_length_response_falls_back(self):
        """Two items in, one out: the mapping cannot be trusted positionally."""
        model = FakeGeminiModel().queue_json([{"canonical": "milk", "category": "dairy"}])
        result = canonicalise.canonicalise_items(["LLET", "PA INTEGRAL"], model=model)
        assert [r["canonical"] for r in result] == ["llet", "pa integral"]

    def test_unparseable_output_falls_back(self):
        model = FakeGeminiModel().queue_text("I'm afraid I can't do that")
        assert (
            canonicalise.canonicalise_items(["POMA GOLDEN 1KG"], model=model)[0]["canonical"]
            == "poma golden"
        )

    def test_a_blank_canonical_falls_back_for_that_item_only(self):
        model = FakeGeminiModel().queue_json(
            [
                {"canonical": "", "category": "produce"},
                {"canonical": "rice", "category": "produce"},
            ]
        )
        result = canonicalise.canonicalise_items(["POMA GOLDEN 1KG", "ARROS RODO 1KG"], model=model)
        assert result[0]["canonical"] == "poma golden"
        assert result[1]["canonical"] == "rice"

    def test_empty_input_returns_empty(self):
        assert canonicalise.canonicalise_items([]) == []

    def test_output_always_lines_up_with_input(self):
        model = FakeGeminiModel().queue_text("garbage")
        names = ["A", "B", "C", "D"]
        assert len(canonicalise.canonicalise_items(names, model=model)) == len(names)


class TestCanonicaliseQuery:
    def test_a_question_term_is_mapped_the_same_way_as_stored_rows(self):
        """Both sides must go through the same mapping or nothing matches."""
        model = FakeGeminiModel().queue_json([{"canonical": "milk", "category": "dairy"}])
        assert canonicalise.canonicalise_query("leite", model=model) == "milk"

    def test_english_terms_pass_through(self):
        model = FakeGeminiModel().queue_json([{"canonical": "milk", "category": "dairy"}])
        assert canonicalise.canonicalise_query("milk", model=model) == "milk"

    def test_empty_query_is_safe(self):
        assert canonicalise.canonicalise_query("") == ""

    def test_a_failure_still_yields_something_matchable(self):
        model = FakeGeminiModel().raises(RuntimeError("down"))
        assert canonicalise.canonicalise_query("POMA GOLDEN 1KG", model=model) == "poma golden"

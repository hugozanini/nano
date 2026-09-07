"""Does the real model actually map these words?

The unit tests prove the plumbing: batching, fallbacks, output alignment. They
cannot prove the premise, because the fake returns whatever the test scripts.
The premise is that Gemini genuinely knows `POMA GOLDEN 1KG` is an apple and
`PILES AAA P-4` is not food — and that is the entire justification for
choosing model-based canonicalisation over a hand-maintained synonym table.

Real model output drifts between versions, so these assertions are deliberately
loose: they check the mapping is *usable*, not that it produced one exact word.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

# Catalan line items in the abbreviated form receipts actually print, with the
# English words a person would ask about.
# Distinct on purpose: the lookup below is positional, and a repeated item
# would silently compare against the wrong result. An earlier vocabulary swap
# collapsed two entries onto the same name and did exactly that.
CATALAN_ITEMS = [
    ("POMA GOLDEN 1KG", {"apple", "apples"}),
    ("ARROS RODO 1KG", {"rice"}),
    ("LLET SENCERA 1L", {"milk", "whole milk"}),
    ("OUS M-12", {"egg", "eggs"}),
    ("PA INTEGRAL", {"bread", "wholemeal", "whole wheat", "wholewheat"}),
]

FOOD_CATEGORIES = {"produce", "dairy", "meat", "seafood", "bakery", "pantry", "frozen", "snacks"}


@pytest.fixture(scope="module")
def real_model(project_id):
    import vertexai
    from vertexai.generative_models import GenerativeModel

    vertexai.init(project=project_id, location="us-central1")
    return GenerativeModel("gemini-2.5-flash")


@pytest.fixture(scope="module")
def mapped(real_model):
    """One call for the whole batch, shared by every assertion below."""
    from nano import canonicalise

    names = [name for name, _ in CATALAN_ITEMS] + ["PILES AAA P-4"]
    return names, canonicalise.canonicalise_items(names, model=real_model)


class TestRealCanonicalisation:
    def test_every_input_gets_an_output(self, mapped):
        names, result = mapped
        assert len(result) == len(names)

    @pytest.mark.parametrize(
        ("index", "item", "acceptable"),
        [(i, name, words) for i, (name, words) in enumerate(CATALAN_ITEMS)],
    )
    def test_catalan_items_map_to_recognisable_english(self, mapped, index, item, acceptable):
        # Indexed rather than looked up by name: canonicalise_items guarantees
        # output aligns positionally with input, and a name lookup would read
        # the wrong row if two inputs ever shared a name.
        _names, result = mapped
        canonical = result[index]["canonical"]
        assert any(word in canonical for word in acceptable), (
            f"{item!r} became {canonical!r}, which does not contain any of {acceptable}"
        )

    def test_a_batteries_is_not_categorised_as_food(self, mapped):
        """The reason `category` exists: a receipt is not all groceries."""
        names, result = mapped
        entry = result[names.index("PILES AAA P-4")]  # unique, so safe
        assert entry["category"] not in FOOD_CATEGORIES, (
            f"batteries was categorised as {entry['category']!r}"
        )

    def test_categories_come_from_the_allowed_set(self, mapped):
        from nano.canonicalise import CATEGORIES

        _, result = mapped
        assert all(entry["category"] in CATEGORIES for entry in result)

    def test_a_question_and_a_receipt_line_meet_in_the_middle(self, real_model):
        """The end-to-end premise: English question, Catalan receipt, same term."""
        from nano import canonicalise

        stored = canonicalise.canonicalise_items(["LLET SENCERA 1L"], model=real_model)[0]
        asked = canonicalise.canonicalise_query("milk", model=real_model)
        assert "milk" in stored["canonical"] and "milk" in asked, (
            f"receipt line canonicalised to {stored['canonical']!r} but the question "
            f"canonicalised to {asked!r}; they must match for spend_by_item to work"
        )


def test_fixture_item_names_are_unique():
    """Positional comparison only works if no input name repeats."""
    names = [name for name, _ in CATALAN_ITEMS]
    assert len(names) == len(set(names)), f"duplicate fixture names: {names}"

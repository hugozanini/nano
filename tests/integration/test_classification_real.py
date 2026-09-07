"""Routing, against the model that actually decides it.

The unit tier cannot test this. It asserts that the context carries Nano's
replies and that the rules are stated in the prompt — both true — while the
fake returns whatever the test queued, so it would pass just as happily if the
model ignored every word of it.

That gap is not hypothetical here. The conversation-context fix was written,
unit-tested and looked right, and measured **zero** improvement against the
real model: `get_list`'s description said "asks what is on the list", and that
surface match outweighed a paragraph of context. Only this tier showed it.

Model output drifts, so these assert the routing, never the wording.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

PURCHASE_REPLY = (
    "Your last trip was 29 Aug at Mercadona, EUR 43.06 for 12 items: "
    "- 24 fresh eggs (24 OUS FRESC) - strawberry (MADUIXA)"
)
AFTER_PURCHASE = [
    {"text": "What have I purchase last time?", "intent": "query_history", "reply": PURCHASE_REPLY}
]


@pytest.fixture
def wired(monkeypatch, project_id):
    from nano import clients, config

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", project_id)
    monkeypatch.setenv("VERIFY_TOKEN", "integration-token")
    monkeypatch.setenv("PHONE_NUMBER_ID", "000000000000000")
    monkeypatch.setenv("HOUSEHOLD_MEMBERS", "15550100001:member_primary")
    monkeypatch.setenv("TASK_BACKEND", "inline")

    config.reset_settings_cache()
    clients.reset_clients()
    yield config.get_settings()
    config.reset_settings_cache()
    clients.reset_clients()


def classify(text, turns=None, items=None, topic=""):
    from nano import gemini

    return gemini.classify_message(
        text, turns=turns or [], current_items=items or [], last_topic=topic
    )


class TestBareReferencesFollowTheConversation:
    """The screenshot, replayed."""

    def test_the_list_means_the_purchase_after_a_purchase_answer(self, wired):
        intent, args = classify(
            "Give me the list in English", AFTER_PURCHASE, ["coffee"], "purchases"
        )
        assert intent == "query_history"
        assert args.get("query_type") == "items_in_last_purchase"

    def test_that_again_follows_the_same_subject(self, wired):
        intent, _ = classify("show me that again", AFTER_PURCHASE, ["coffee"], "purchases")
        assert intent == "query_history"

    def test_the_shopping_list_stays_reachable_from_the_same_state(self, wired):
        """The regression risk runs this way: steering so hard it disappears."""
        intent, _ = classify(
            "what's on the shopping list?", AFTER_PURCHASE, ["coffee"], "purchases"
        )
        assert intent == "get_list"

    def test_a_bare_list_with_no_history_is_still_the_shopping_list(self, wired):
        intent, _ = classify("what's on the list?", [], ["coffee"])
        assert intent == "get_list"

    def test_an_action_is_unaffected_by_the_subject(self, wired):
        intent, _ = classify("add coffee", AFTER_PURCHASE, ["coffee"], "purchases")
        assert intent == "add_items"


LISTED_A_PURCHASE = [
    {
        "text": "What have I purchase last time?",
        "intent": "query_history",
        "reply": (
            "Your last trip was 29 Aug at Mercadona, EUR 43.06 for 12 items: "
            "- 24 fresh eggs (24 OUS FRESC) - family pack breast (PIT FAM.) "
            "- strawberry (MADUIXA)"
        ),
    }
]


class TestAskingWhatSomethingIsIsALookup:
    """The failure: Nano listed `family pack breast (PIT FAM.)`, was asked what
    that was, and answered "I can't work that out yet — I've noted it as a
    feature request." Every fact was already stored, and the backlog gained an
    entry for a capability that is not missing.
    """

    @pytest.mark.parametrize(
        "text",
        [
            "What is family pack breast? Chicken breast?",
            "what is PIT FAM?",
            "what's a maduixa?",
        ],
    )
    def test_asking_what_an_item_is_reaches_the_receipt_data(self, wired, text):
        intent, args = classify(text, LISTED_A_PURCHASE, ["coffee"], "purchases")
        assert intent == "query_history", f"{text!r} became {intent}, not a lookup"
        assert args.get("query_type") == "explain_item"

    def test_it_is_never_filed_as_a_missing_feature(self, wired):
        intent, _ = classify(
            "What is family pack breast? Chicken breast?",
            LISTED_A_PURCHASE,
            ["coffee"],
            "purchases",
        )
        assert intent not in {"cannot_answer", "request_feature"}, (
            "the backlog is for what is genuinely missing"
        )

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("what did we buy?", "items_in_last_purchase"),
            ("how much did we spend on strawberries?", "spend_by_item"),
            ("when did we last buy strawberries?", "last_purchase"),
        ],
    )
    def test_the_other_history_questions_still_route(self, wired, text, expected):
        """A new enum value must not swallow the ones already there."""
        intent, args = classify(text, [], [])
        assert intent == "query_history"
        assert args.get("query_type") == expected


class TestItCanAnswerForItself:
    """Someone asked which AI model Nano uses; it filed a feature request.

    Same shape as the item lookup: the model name is in configuration, so this
    was never a capability gap. The routing matters more than usual here,
    because the answers are hand-written — a question that misses `about_nano`
    reaches a path that would improvise, and improvising about your own
    architecture produces plausible fiction.
    """

    @pytest.mark.parametrize(
        ("text", "topic"),
        [
            ("which AI model do you use?", "which_model"),
            ("what model are you running on?", "which_model"),
            ("how do you work?", "how_it_works"),
            ("do you keep my messages?", "data_privacy"),
            ("are you training on our chats?", "data_privacy"),
            ("who built you?", "who_built_it"),
        ],
    )
    def test_a_question_about_nano_reaches_nano(self, wired, text, topic):
        intent, args = classify(text)
        assert intent == "about_nano", f"{text!r} became {intent}"
        assert args.get("topic") == topic

    def test_it_is_never_filed_as_a_missing_feature(self, wired):
        intent, _ = classify("which AI model do you use?")
        assert intent not in {"cannot_answer", "request_feature"}

    @pytest.mark.parametrize("text", ["what can you do?", "help"])
    def test_asking_what_it_can_do_is_still_help(self, wired, text):
        """`help` is about groceries; `about_nano` is about the assistant."""
        assert classify(text)[0] == "help"


class TestItDoesNotAskAboutClearMessages:
    """The guard that matters most.

    Over-asking would make clarification worse than the bug it addresses: a
    wrong answer to a clear question is occasional, a question back on every
    clear message is constant.
    """

    @pytest.mark.parametrize(
        ("text", "turns", "topic", "items"),
        [
            ("add coffee", [], "", ["coffee"]),
            ("remove milk", [], "", ["milk", "coffee"]),
            ("what's on the shopping list?", [], "", ["coffee"]),
            ("what's on the list?", [], "", ["coffee"]),
            ("how much did we spend this month?", [], "", []),
            ("Give me the list in English", AFTER_PURCHASE, "purchases", ["coffee"]),
            ("show me that again", AFTER_PURCHASE, "purchases", ["coffee"]),
            ("clear the list", [], "", ["coffee"]),
            ("undo", [], "", ["coffee"]),
            ("when did we last buy strawberries?", [], "", []),
            ("help", [], "", []),
            ("we need eggs and bread", [], "", []),
        ],
    )
    def test_a_clear_message_is_answered_not_questioned(self, wired, text, turns, topic, items):
        intent, _ = classify(text, turns, items, topic)
        assert intent != "clarify", f"asked about an unambiguous message: {text!r}"


class TestItAsksWhenNothingSettlesIt:
    def test_a_bare_quantity_question_is_ambiguous(self, wired):
        """ "how much?" with no subject could be the list or the spending.

        Asserted loosely: answering one reading and naming the assumption is
        also a correct outcome. What must not happen is a confident answer to
        a question with no subject.
        """
        intent, args = classify("how much?", [], ["coffee"])
        if intent == "clarify":
            assert len(args.get("options") or []) == 2
        else:
            assert args.get("assumed"), "commit to a reading or ask, but say which"

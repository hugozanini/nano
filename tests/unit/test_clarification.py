"""Asking which of two things was meant — and, mostly, not asking.

The risk that would make this feature worse than the bug it addresses is
over-asking. A wrong answer to a clear question is bad; a question back on
every clear message is worse, because it happens constantly rather than rarely.
So most of these tests assert that Nano does *not* ask, and the resolution path
is deterministic rather than another model call.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from nano import gemini, handlers, question_log, store
from nano.config import load_settings

from tests.fakes.whatsapp import MEMBER_PRIMARY, signed, text_message


@pytest.fixture
def settings():
    return load_settings()


@pytest.fixture
def wired_store(monkeypatch, fake_firestore):
    monkeypatch.setattr(store, "firestore_client", lambda: fake_firestore)
    return fake_firestore


@pytest.fixture
def client(nano_app) -> TestClient:
    return TestClient(nano_app)


def post(client: TestClient, payload: dict, app_secret: str):
    body, headers = signed(payload, app_secret)
    return client.post("/webhook", content=body, headers=headers)


class TestTheQuestionIsWrittenInCode:
    """The model picks the readings; the grammar is not its problem."""

    def test_the_question_names_both_readings(self):
        question = handlers._clarify_question(["shopping_list", "last_purchase"])
        assert "your shopping list" in question
        assert "what you bought last time" in question
        assert question.endswith('Reply "shopping list" or "last purchase".')

    def test_every_reading_produces_a_grammatical_question(self):
        """Any pair the model can return, not just the one that prompted this."""
        readings = sorted(gemini.READINGS)
        for first in readings:
            for second in readings:
                if first == second:
                    continue
                question = handlers._clarify_question([first, second])
                assert question.startswith("Do you mean ")
                assert ", or " in question

    def test_one_option_is_not_a_question(self):
        assert handlers._clarify_options(["shopping_list"]) == []

    def test_a_repeated_option_is_not_two_options(self):
        assert handlers._clarify_options(["shopping_list", "shopping_list"]) == []

    def test_a_third_option_is_dropped_rather_than_asked(self):
        """Two is a question; three is an interrogation."""
        options = handlers._clarify_options(["shopping_list", "last_purchase", "spending"])
        assert len(options) == 2

    def test_an_unknown_reading_is_ignored(self):
        assert handlers._clarify_options(["shopping_list", "the_weather"]) == []

    def test_no_options_at_all_is_survivable(self):
        assert handlers._clarify_options(None) == []


class TestAnsweringTheQuestion:
    @pytest.mark.parametrize(
        ("answer", "expected"),
        [
            ("1", "shopping_list"),
            ("first", "shopping_list"),
            ("the first", "shopping_list"),
            ("2", "last_purchase"),
            ("second", "last_purchase"),
            ("shopping list", "shopping_list"),
            ("the shopping one", "shopping_list"),
            ("last purchase", "last_purchase"),
            ("what i bought", "last_purchase"),
            ("the receipt", "last_purchase"),
        ],
    )
    def test_an_answer_picks_the_right_reading(self, answer, expected):
        options = ["shopping_list", "last_purchase"]
        assert handlers._match_option(answer, options) == expected

    def test_a_longer_phrase_wins_over_a_shorter_one(self):
        """ "shopping list" contains "the list", which belongs to the other option."""
        assert (
            handlers._match_option("the shopping list", ["shopping_list", "last_purchase"])
            == "shopping_list"
        )

    def test_an_unrelated_message_matches_nothing(self):
        """It gets classified fresh instead — the subject changed."""
        assert handlers._match_option("add coffee", ["shopping_list", "last_purchase"]) == ""

    def test_an_ordinal_beyond_the_options_matches_nothing(self):
        assert handlers._match_option("2", ["shopping_list"]) == ""


class TestTheClarificationRoundTrip:
    def test_asking_stores_the_pending_question_and_answers_nothing(
        self, client, app_secret, fake_gemini, fake_firestore, settings
    ):
        fake_gemini.queue_function_call("clarify", options=["shopping_list", "last_purchase"])
        post(client, text_message("the list", message_id="wamid.C1"), app_secret)

        conversation = store.get_conversation(MEMBER_PRIMARY, settings)
        assert conversation["pending"]["kind"] == "clarify"
        assert conversation["pending"]["payload"]["options"] == [
            "shopping_list",
            "last_purchase",
        ]

    def test_the_answer_runs_the_chosen_intent(
        self, client, app_secret, fake_gemini, fake_bq, fake_firestore, settings
    ):
        fake_bq.when("ORDER BY p.purchased_at DESC", [])
        fake_gemini.queue_function_call("clarify", options=["shopping_list", "last_purchase"])
        post(client, text_message("the list", message_id="wamid.C2"), app_secret)

        before = len(fake_gemini.calls)
        post(client, text_message("last purchase", message_id="wamid.C3"), app_secret)

        assert len(fake_gemini.calls) == before, "resolving an answer should not cost a model call"

    def test_changing_the_subject_drops_the_clarification(
        self, client, app_secret, fake_gemini, fake_firestore, settings
    ):
        """Insisting on an answer to a question you have moved on from is broken."""
        fake_gemini.queue_function_call("clarify", options=["shopping_list", "last_purchase"])
        post(client, text_message("the list", message_id="wamid.C4"), app_secret)

        fake_gemini.queue_function_call("add_items", items=["coffee"])
        post(client, text_message("add coffee", message_id="wamid.C5"), app_secret)

        assert "coffee" in store.get_items(settings)
        conversation = store.get_conversation(MEMBER_PRIMARY, settings)
        assert not conversation.get("pending"), "the question should be gone either way"

    def test_the_choice_becomes_the_subject(
        self, client, app_secret, fake_gemini, fake_bq, fake_firestore, settings
    ):
        """So "and in English?" stays on it rather than asking again."""
        fake_bq.when("ORDER BY p.purchased_at DESC", [])
        fake_gemini.queue_function_call("clarify", options=["shopping_list", "last_purchase"])
        post(client, text_message("the list", message_id="wamid.C6"), app_secret)
        post(client, text_message("last purchase", message_id="wamid.C7"), app_secret)

        conversation = store.get_conversation(MEMBER_PRIMARY, settings)
        assert conversation["last_topic"] == "purchases"

    def test_a_clarification_with_no_usable_options_answers_instead(
        self, client, app_secret, fake_gemini, fake_firestore, settings
    ):
        """An empty question is worse than a possibly-wrong answer."""
        fake_gemini.queue_function_call("clarify", options=["shopping_list"])
        post(client, text_message("the list", message_id="wamid.C8"), app_secret)

        conversation = store.get_conversation(MEMBER_PRIMARY, settings)
        assert not conversation.get("pending")

    def test_a_stale_clarification_is_ignored(self, wired_store, settings):
        """Reusing the confirmation TTL: an answer twenty minutes later is not one."""
        import datetime

        store.set_pending(MEMBER_PRIMARY, "clarify", {"options": ["shopping_list"]}, settings)
        conversation = store.get_conversation(MEMBER_PRIMARY, settings)
        conversation["pending"]["at"] = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
            hours=2
        )
        store._save_conversation(MEMBER_PRIMARY, conversation, settings)
        assert store.take_pending(MEMBER_PRIMARY, settings) is None


class TestTheAssumptionNote:
    """When one reading wins, say which — do not ask."""

    def test_the_note_names_the_reading_and_offers_the_other(self):
        noted = handlers._assumption_note("last_purchase", "Your last trip was …")
        assert noted.startswith("Your last trip was …")
        assert "what you bought last time" in noted
        assert "shopping list" in noted

    def test_no_assumption_leaves_the_reply_untouched(self):
        """An unnecessary note reads as second-guessing."""
        assert handlers._assumption_note("", "Here's the list.") == "Here's the list."

    def test_an_unknown_assumption_leaves_the_reply_untouched(self):
        assert handlers._assumption_note("weather", "Here's the list.") == "Here's the list."

    def test_the_note_reaches_the_phone_with_the_answer(
        self, client, app_secret, fake_gemini, fake_firestore, fake_whatsapp, settings
    ):
        store.add_items(["coffee"], settings)
        fake_gemini.queue_function_call("get_list", assumed="shopping_list")
        post(client, text_message("the list", message_id="wamid.A1"), app_secret)

        reply = fake_whatsapp.sent[-1].text
        assert "coffee" in reply, "the answer comes first; the note is only appended"
        assert "say" in reply and "last purchase" in reply

    def test_a_clear_message_gets_no_note(
        self, client, app_secret, fake_gemini, fake_firestore, fake_whatsapp, settings
    ):
        store.add_items(["coffee"], settings)
        fake_gemini.queue_function_call("get_list")
        post(
            client, text_message("what's on the shopping list?", message_id="wamid.A2"), app_secret
        )

        assert "say" not in fake_whatsapp.sent[-1].text.lower()


class TestPendingStaysBackwardCompatible:
    def test_a_confirmation_parked_by_the_old_code_still_resolves(self, wired_store, settings):
        """A deploy can land while one of these is in Firestore.

        Dropping it would silently eat someone's "yes" on a list they meant to
        clear, which is the one pending action with data behind it.
        """
        conversation = store.get_conversation(MEMBER_PRIMARY, settings)
        conversation["pending"] = {
            "action": "close_list",
            "items": ["coffee", "milk"],
            "at": store._now(),
        }
        store._save_conversation(MEMBER_PRIMARY, conversation, settings)

        pending = store.take_pending(MEMBER_PRIMARY, settings)
        assert pending["kind"] == "close_list"
        assert pending["payload"]["items"] == ["coffee", "milk"]


class TestTheTelemetry:
    def test_clarified_is_a_known_resolution(self):
        assert question_log.CLARIFIED in question_log.RESOLUTIONS

    def test_a_clarification_is_recorded(self, client, app_secret, fake_gemini, fake_bq):
        fake_gemini.queue_function_call("clarify", options=["shopping_list", "last_purchase"])
        post(client, text_message("the list", message_id="wamid.Q1"), app_secret)

        logged = [q for q in fake_bq.queries if "question_log" in q.sql]
        assert logged, "an ambiguous phrasing is exactly what the log is for"


class TestTheDescriptionGuardsAgainstOverAsking:
    def test_clarify_requires_both_plausibility_and_a_different_answer(self):
        spec = next(s for s in gemini.INTENT_SPECS if s["name"] == "clarify")
        assert "ONLY when BOTH are true" in spec["description"]
        assert "materially different" in spec["description"]

    def test_clarify_defers_to_settled_context(self):
        spec = next(s for s in gemini.INTENT_SPECS if s["name"] == "clarify")
        assert "already settles it" in spec["description"]

    def test_clarify_names_the_messages_that_must_never_ask(self):
        spec = next(s for s in gemini.INTENT_SPECS if s["name"] == "clarify")
        assert "add coffee" in spec["description"]

    def test_assumed_is_offered_as_the_cheaper_alternative(self):
        spec = next(s for s in gemini.INTENT_SPECS if s["name"] == "clarify")
        assert "set `assumed` instead of asking" in spec["description"]

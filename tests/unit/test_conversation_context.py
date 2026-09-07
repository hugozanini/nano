"""Following a conversation, rather than reading each message in isolation.

The failure this guards against, from production:

    You:  What have I purchase last time?
    Nano: Your last trip was 29 Aug at Mercadona … 12 items      correct
    You:  Give me the list in English
    Nano: Here's the current list (1 items): coffee              wrong

"The list" meant the twelve items just printed. The cause was a dropped field,
not a missing feature: `record_turn` stored Nano's reply and `build_context`
never read it, so the model saw only that a `query_history` had happened and had
no idea a list of purchases was on screen.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from nano import gemini, store
from nano.config import load_settings

from tests.fakes.whatsapp import MEMBER_PRIMARY, MEMBER_SECONDARY, signed, text_message

PURCHASE_REPLY = (
    "Your last trip was 29 Aug at Mercadona, EUR 43.06 for 12 items:\n"
    "- 24 fresh eggs (24 OUS FRESC) — EUR 10.50\n- strawberry (MADUIXA) — EUR 3.11"
)


@pytest.fixture
def settings():
    return load_settings()


@pytest.fixture
def wired(monkeypatch, fake_firestore):
    monkeypatch.setattr(store, "firestore_client", lambda: fake_firestore)
    return fake_firestore


@pytest.fixture
def client(nano_app) -> TestClient:
    return TestClient(nano_app)


def post(client: TestClient, payload: dict, app_secret: str):
    body, headers = signed(payload, app_secret)
    return client.post("/webhook", content=body, headers=headers)


class TestTheReplyReachesThePrompt:
    """The dropped field, restored."""

    def test_what_nano_said_is_in_the_context(self):
        context = gemini.build_context(
            [
                {
                    "text": "What have I purchase last time?",
                    "intent": "query_history",
                    "reply": PURCHASE_REPLY,
                }
            ],
            ["coffee"],
        )
        assert "Nano replied" in context
        assert "24 fresh eggs" in context, "without the reply, 'the list' has nothing to attach to"

    def test_a_long_reply_is_excerpted_not_dropped(self):
        context = gemini.build_context(
            [{"text": "q", "intent": "query_history", "reply": "x" * 2000}], []
        )
        assert "Nano replied" in context
        assert len(context) < 1000, "six turns of full replies would be an expensive prompt"

    def test_a_turn_with_no_reply_still_renders(self):
        context = gemini.build_context([{"text": "add coffee", "intent": "add_items"}], [])
        assert "add coffee" in context

    def test_newlines_in_a_reply_do_not_break_the_layout(self):
        context = gemini.build_context(
            [{"text": "q", "intent": "query_history", "reply": "line one\nline two"}], []
        )
        assert "line one line two" in context


class TestTheLastSubject:
    def test_the_subject_is_stated_for_bare_references(self):
        context = gemini.build_context([], [], last_topic="purchases")
        assert "what was bought on a recent shopping trip" in context
        assert '"the list"' in context

    @pytest.mark.parametrize(
        ("intent", "query_type", "expected"),
        [
            ("query_history", "items_in_last_purchase", "purchases"),
            ("query_history", "last_purchase", "purchases"),
            ("query_history", "total_spend", "spending"),
            ("add_items", "", "shopping_list"),
            ("get_list", "", "shopping_list"),
            ("log_receipt", "", "purchases"),
            ("analytics_question", "", "spending"),
            ("help", "", ""),
        ],
    )
    def test_each_intent_leaves_the_right_subject(self, intent, query_type, expected):
        assert gemini.topic_for(intent, query_type) == expected

    def test_the_subject_is_recorded_after_a_purchase_question(
        self, client, app_secret, fake_gemini, fake_bq, fake_firestore
    ):
        fake_bq.when("ORDER BY p.purchased_at DESC", [])
        fake_gemini.queue_function_call("query_history", query_type="items_in_last_purchase")
        post(client, text_message("what did I buy?", message_id="wamid.T1"), app_secret)

        doc = next(v for k, v in fake_firestore.store.items() if "conversations" in k)
        assert doc["last_topic"] == "purchases"

    def test_the_screenshot_sequence_leaves_purchases_as_the_subject(
        self, client, app_secret, fake_gemini, fake_bq, fake_firestore
    ):
        """After this, "the list" should bind to purchases rather than the list."""
        fake_bq.when("ORDER BY p.purchased_at DESC", [])
        fake_gemini.queue_function_call("query_history", query_type="items_in_last_purchase")
        post(
            client,
            text_message("What have I purchase last time?", message_id="wamid.T2"),
            app_secret,
        )

        doc = next(v for k, v in fake_firestore.store.items() if "conversations" in k)
        context = gemini.build_context(
            doc["turns"], ["coffee"], last_topic=doc.get("last_topic", "")
        )
        assert "recent shopping trip" in context


class TestSharedMemoryForBroadcasts:
    """Both phones saw it, so both memories should carry it."""

    def test_a_change_reaches_both_members(self, client, app_secret, fake_gemini, fake_firestore):
        fake_gemini.queue_function_call("add_items", items=["coffee"])
        post(
            client,
            text_message("add coffee", sender=MEMBER_PRIMARY, message_id="wamid.S1"),
            app_secret,
        )

        conversations = [k for k in fake_firestore.store if "conversations" in k]
        assert len(conversations) == 2, "an add is broadcast, so both memories get it"

    def test_the_actor_is_recorded_so_the_prompt_can_name_them(
        self, client, app_secret, fake_gemini, fake_firestore
    ):
        fake_gemini.queue_function_call("add_items", items=["coffee"])
        post(
            client,
            text_message("add coffee", sender=MEMBER_PRIMARY, message_id="wamid.S2"),
            app_secret,
        )

        other = fake_firestore.store[f"nano_test_conversations/{MEMBER_SECONDARY}"]
        assert other["turns"][-1]["by"] == "member_primary"

    def test_an_answer_stays_with_whoever_asked(
        self, client, app_secret, fake_gemini, fake_bq, fake_firestore
    ):
        """The other person never saw it, so they must not refer back to it."""
        fake_bq.when("ORDER BY p.purchased_at DESC", [])
        fake_gemini.queue_function_call("query_history", query_type="items_in_last_purchase")
        post(
            client,
            text_message("what did I buy?", sender=MEMBER_PRIMARY, message_id="wamid.S3"),
            app_secret,
        )

        conversations = [k for k in fake_firestore.store if "conversations" in k]
        assert len(conversations) == 1
        assert MEMBER_PRIMARY in conversations[0]

    def test_the_other_person_is_named_rather_than_called_you(self):
        context = gemini.build_context(
            [{"text": "add coffee", "intent": "add_items", "by": "sam"}],
            ["coffee"],
            viewer="alex",
        )
        assert 'sam said: "add coffee"' in context

    def test_your_own_turns_still_read_as_yours(self):
        context = gemini.build_context(
            [{"text": "add coffee", "intent": "add_items", "by": "alex"}],
            ["coffee"],
            viewer="alex",
        )
        assert 'you said: "add coffee"' in context


class TestTheInstructionsResolveBareReferences:
    """Context alone did not fix the screenshot. An explicit instruction did.

    Measured against the real model, ten trials each, on "Give me the list in
    English" after a purchase answer:

        richer context, no instruction change     0/10
        get_list description only                10/10
        system prompt rule only                  10/10
        both, as shipped                         10/10

    So context loses to a standing instruction — `get_list` saying "asks what is
    on the list" outweighed a paragraph of conversation — and either instruction
    alone is enough. Both are kept: a function description and a general
    principle drift differently as models change, and carrying both costs two
    sentences.

    These assert the instructions are still stated. Whether they still *work* is
    `tests/integration/test_classification_real.py`, which is the only tier that
    can tell.
    """

    def test_the_system_prompt_binds_bare_references_to_the_subject(self):
        assert "A bare reference" in gemini.SYSTEM_PROMPT
        assert "NOT to whichever function the word usually matches" in gemini.SYSTEM_PROMPT

    def test_get_list_says_when_the_list_means_something_else(self):
        spec = next(s for s in gemini.INTENT_SPECS if s["name"] == "get_list")
        assert "Do NOT use this" in spec["description"]
        assert "items_in_last_purchase" in spec["description"], (
            "the description should name where to go instead"
        )

    def test_get_list_still_describes_its_own_job_first(self):
        """Steering away from it must not stop it being findable."""
        spec = next(s for s in gemini.INTENT_SPECS if s["name"] == "get_list")
        assert spec["description"].startswith("Show the shared shopping list")

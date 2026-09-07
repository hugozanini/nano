"""Memory, confirmations, undo and help.

Until now every message was classified in complete isolation, so a follow-up had
nothing to refer back to: "no, the decaf one" and "remove that" were
uninterpretable, and `close_list` destroyed the list on the strength of one
possibly-misheard sentence with no way back.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from nano import gemini, store
from nano.config import load_settings

from tests.fakes.whatsapp import MEMBER_PRIMARY, MEMBER_SECONDARY, signed, text_message


@pytest.fixture
def client(nano_app) -> TestClient:
    return TestClient(nano_app)


def post(client: TestClient, payload: dict, app_secret: str):
    body, headers = signed(payload, app_secret)
    return client.post("/webhook", content=body, headers=headers)


@pytest.fixture
def settings():
    return load_settings()


@pytest.fixture
def wired_store(monkeypatch, fake_firestore):
    monkeypatch.setattr(store, "firestore_client", lambda: fake_firestore)
    return fake_firestore


# --------------------------------------------------------------------------
# Memory
# --------------------------------------------------------------------------


class TestConversationMemory:
    def test_turns_are_recorded(self, wired_store, settings):
        store.record_turn(MEMBER_PRIMARY, "add coffee", "add_items", settings, "Added!")
        turns = store.get_conversation(MEMBER_PRIMARY, settings)["turns"]
        assert turns[0]["text"] == "add coffee"
        assert turns[0]["intent"] == "add_items"

    def test_only_the_most_recent_turns_are_kept(self, wired_store, settings):
        """The prompt has to stay cheap and focused on what is still relevant."""
        for i in range(20):
            store.record_turn(MEMBER_PRIMARY, f"message {i}", "add_items", settings)
        turns = store.get_conversation(MEMBER_PRIMARY, settings)["turns"]
        assert len(turns) == store.MEMORY_TURNS
        assert turns[-1]["text"] == "message 19"

    def test_each_person_has_their_own_thread(self, wired_store, settings):
        store.record_turn(MEMBER_PRIMARY, "add coffee", "add_items", settings)
        store.record_turn(MEMBER_SECONDARY, "add milk", "add_items", settings)
        assert store.get_conversation(MEMBER_PRIMARY, settings)["turns"][0]["text"] == "add coffee"
        assert store.get_conversation(MEMBER_SECONDARY, settings)["turns"][0]["text"] == "add milk"

    def test_no_history_is_not_an_error(self, wired_store, settings):
        assert store.get_conversation("nobody", settings) == {}


class TestClassificationContext:
    def test_context_includes_the_current_list(self):
        context = gemini.build_context([], ["coffee", "milk"])
        assert "coffee, milk" in context

    def test_an_empty_list_says_so(self):
        assert "(empty)" in gemini.build_context([], [])

    def test_context_includes_recent_turns(self):
        context = gemini.build_context([{"text": "add coffee", "intent": "add_items"}], ["coffee"])
        assert "add coffee" in context
        assert "add_items" in context

    def test_context_reaches_the_model(self, fake_gemini):
        """Without this, a follow-up has nothing to refer back to."""
        fake_gemini.queue_function_call("remove_items", items=["coffee"])
        gemini.classify_message(
            "no, the decaf one",
            model=fake_gemini,
            turns=[{"text": "add coffee", "intent": "add_items"}],
            current_items=["coffee"],
        )
        prompt = fake_gemini.calls[-1].parts[0]
        assert "add coffee" in prompt
        assert "coffee" in prompt


# --------------------------------------------------------------------------
# Destructive actions
# --------------------------------------------------------------------------


class TestCloseListConfirmation:
    def test_a_non_empty_list_is_not_cleared_immediately(
        self, client, app_secret, fake_gemini, fake_whatsapp
    ):
        fake_gemini.queue_function_call("add_items", items=["coffee", "milk"])
        post(client, text_message("add coffee and milk", message_id="wamid.1"), app_secret)

        fake_gemini.queue_function_call("close_list")
        post(client, text_message("clear the list", message_id="wamid.2"), app_secret)

        assert any("Reply 'yes' to confirm" in t for t in fake_whatsapp.texts)
        assert any("2 items" in t for t in fake_whatsapp.texts)

    def test_yes_confirms_and_clears(self, client, app_secret, fake_gemini, fake_whatsapp):
        fake_gemini.queue_function_call("add_items", items=["coffee"])
        post(client, text_message("add coffee", message_id="wamid.1"), app_secret)
        fake_gemini.queue_function_call("close_list")
        post(client, text_message("clear the list", message_id="wamid.2"), app_secret)

        post(client, text_message("yes", message_id="wamid.3"), app_secret)
        assert any("List cleared" in t for t in fake_whatsapp.texts)

    def test_no_leaves_the_list_alone(self, client, app_secret, fake_gemini, fake_whatsapp):
        fake_gemini.queue_function_call("add_items", items=["coffee"])
        post(client, text_message("add coffee", message_id="wamid.1"), app_secret)
        fake_gemini.queue_function_call("close_list")
        post(client, text_message("clear the list", message_id="wamid.2"), app_secret)

        post(client, text_message("no", message_id="wamid.3"), app_secret)
        assert any("Left the list as it is" in t for t in fake_whatsapp.texts)
        assert not any("List cleared" in t for t in fake_whatsapp.texts)

    def test_yes_without_a_pending_confirmation_is_classified_normally(
        self, client, app_secret, fake_gemini, fake_whatsapp
    ):
        """'yes' only means confirmation while something is actually pending."""
        fake_gemini.set_default_function_call("get_list")
        post(client, text_message("yes", message_id="wamid.1"), app_secret)
        assert not any("List cleared" in t for t in fake_whatsapp.texts)

    def test_an_already_empty_list_needs_no_confirmation(
        self, client, app_secret, fake_gemini, fake_whatsapp
    ):
        fake_gemini.queue_function_call("close_list")
        post(client, text_message("clear the list", message_id="wamid.1"), app_secret)
        assert any("already empty" in t for t in fake_whatsapp.texts)

    def test_a_stale_confirmation_is_ignored(self, wired_store, settings):
        """Answering 'yes' twenty minutes later almost certainly means something else."""
        import datetime

        store.set_pending(MEMBER_PRIMARY, "close_list", {"items": ["coffee"]}, settings)
        conversation = store.get_conversation(MEMBER_PRIMARY, settings)
        conversation["pending"]["at"] = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
            hours=2
        )
        store._save_conversation(MEMBER_PRIMARY, conversation, settings)

        assert store.take_pending(MEMBER_PRIMARY, settings) is None

    def test_a_pending_confirmation_is_consumed_once(self, wired_store, settings):
        store.set_pending(MEMBER_PRIMARY, "close_list", {"items": ["coffee"]}, settings)
        assert store.take_pending(MEMBER_PRIMARY, settings) is not None
        assert store.take_pending(MEMBER_PRIMARY, settings) is None


# --------------------------------------------------------------------------
# Undo
# --------------------------------------------------------------------------


class TestUndo:
    def test_undo_reverses_an_add(self, client, app_secret, fake_gemini, fake_whatsapp):
        fake_gemini.queue_function_call("add_items", items=["caviar"])
        post(client, text_message("add caviar", message_id="wamid.1"), app_secret)

        fake_gemini.queue_function_call("undo")
        post(client, text_message("undo", message_id="wamid.2"), app_secret)

        assert any("Undone" in t for t in fake_whatsapp.texts)
        assert any("empty" in t for t in fake_whatsapp.texts[-1:])

    def test_undo_restores_items_after_a_removal(
        self, client, app_secret, fake_gemini, fake_whatsapp
    ):
        fake_gemini.queue_function_call("add_items", items=["coffee", "milk"])
        post(client, text_message("add coffee and milk", message_id="wamid.1"), app_secret)
        fake_gemini.queue_function_call("remove_items", items=["coffee"])
        post(client, text_message("remove coffee", message_id="wamid.2"), app_secret)
        fake_gemini.queue_function_call("undo")
        post(client, text_message("undo", message_id="wamid.3"), app_secret)

        assert "coffee" in fake_whatsapp.texts[-1]
        assert "milk" in fake_whatsapp.texts[-1]

    def test_undo_with_nothing_to_undo_says_so(
        self, client, app_secret, fake_gemini, fake_whatsapp
    ):
        fake_gemini.queue_function_call("undo")
        post(client, text_message("undo", message_id="wamid.1"), app_secret)
        assert any("nothing for me to undo" in t for t in fake_whatsapp.texts)

    def test_undo_cannot_be_applied_twice(self, client, app_secret, fake_gemini, fake_whatsapp):
        """The action is consumed, so a second undo does not rewind further."""
        fake_gemini.queue_function_call("add_items", items=["coffee"])
        post(client, text_message("add coffee", message_id="wamid.1"), app_secret)
        fake_gemini.queue_function_call("undo")
        post(client, text_message("undo", message_id="wamid.2"), app_secret)
        fake_gemini.queue_function_call("undo")
        post(client, text_message("undo again", message_id="wamid.3"), app_secret)

        assert "nothing for me to undo" in fake_whatsapp.texts[-1]

    def test_undo_restores_the_whole_previous_list(self, wired_store, settings):
        """Restoring state beats inverting the operation.

        Adding an item that was already present is a no-op; inverting that add
        would wrongly remove it. Restoring the previous list is correct either way.
        """
        store.add_items(["coffee"], settings)
        store.record_action(MEMBER_PRIMARY, "add_items", ["coffee"], ["coffee"], settings)
        action = store.take_last_action(MEMBER_PRIMARY, settings)
        store.replace_items(list(action["previous"]), settings)
        assert store.get_items(settings) == ["coffee"]


# --------------------------------------------------------------------------
# Help
# --------------------------------------------------------------------------


class TestHelp:
    def test_help_explains_the_capabilities(self, client, app_secret, fake_gemini, fake_whatsapp):
        fake_gemini.queue_function_call("help")
        post(client, text_message("what can you do?", message_id="wamid.1"), app_secret)

        reply = fake_whatsapp.texts[-1]
        for capability in ("receipt", "undo", "spend", "list"):
            assert capability in reply.lower()

    def test_help_goes_only_to_the_asker(self, client, app_secret, fake_gemini, fake_whatsapp):
        fake_gemini.queue_function_call("help")
        post(client, text_message("help", message_id="wamid.1"), app_secret)
        assert fake_whatsapp.recipients == [MEMBER_PRIMARY]

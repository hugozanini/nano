"""Answering the question that was asked, or saying honestly that you can't.

The failure this guards against, observed in production: asked "What is the main
place we do groceries?" — a reasonable question, answerable from data already
held — Nano replied with its capability list. That is worse than "I don't know",
because it does not even acknowledge what was asked.

The cause was the `help` intent being documented as covering "a message that
makes no sense as a grocery request", which quietly made it the catch-all. The
fallbacks are now three distinct things: I don't understand you, I understand
and can't do that, and you asked what I can do.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from nano import gemini, handlers

from tests.fakes.whatsapp import MEMBER_PRIMARY, signed, text_message


@pytest.fixture
def client(nano_app) -> TestClient:
    return TestClient(nano_app)


def post(client: TestClient, payload: dict, app_secret: str):
    body, headers = signed(payload, app_secret)
    return client.post("/webhook", content=body, headers=headers)


class TestHelpIsNoLongerTheCatchAll:
    def test_the_help_intent_forbids_fallback_use(self):
        """The exact wording that caused the bug must not come back."""
        description = next(
            spec["description"] for spec in gemini.INTENT_SPECS if spec["name"] == "help"
        )

        assert "makes no sense" not in description, (
            "this phrasing made `help` the catch-all for every unrecognised message"
        )
        assert "ONLY" in description
        assert "Never use this as a fallback" in description

    def test_the_fallback_intents_all_exist(self):
        names = {spec["name"] for spec in gemini.INTENT_SPECS}
        assert {"help", "cannot_answer", "request_feature", "unclear"} <= names

    def test_the_system_prompt_forbids_answering_with_capabilities(self):
        assert "Never answer a real question by listing your capabilities" in (gemini.SYSTEM_PROMPT)

    def test_help_still_works_when_actually_asked(
        self, client: TestClient, app_secret: str, fake_gemini, fake_whatsapp
    ):
        fake_gemini.queue_function_call("help")
        post(client, text_message("what can you do?", message_id="wamid.H1"), app_secret)
        assert "Nano" in fake_whatsapp.texts[-1]


class TestCannotAnswerNamesTheRequest:
    """A brush-off and an answer differ by whether the request is named back."""

    def test_the_reply_quotes_what_was_asked(
        self, client: TestClient, app_secret: str, fake_gemini, fake_whatsapp
    ):
        fake_gemini.queue_function_call(
            "cannot_answer", request="know which shop is cheapest for milk"
        )
        post(
            client,
            text_message("where's the cheapest milk?", message_id="wamid.C1"),
            app_secret,
        )
        reply = fake_whatsapp.texts[-1]
        assert "which shop is cheapest for milk" in reply
        assert "feature request" in reply

    def test_the_reply_is_not_the_capability_list(
        self, client: TestClient, app_secret: str, fake_gemini, fake_whatsapp
    ):
        """The regression from the screenshot, stated directly."""
        fake_gemini.queue_function_call(
            "cannot_answer", request="know the main place we do groceries"
        )
        post(
            client,
            text_message("What is the main place we do groceries?", message_id="wamid.C2"),
            app_secret,
        )
        reply = fake_whatsapp.texts[-1]
        assert "main place we do groceries" in reply
        assert "The list" not in reply, "this is the capability brochure again"
        assert "Receipts" not in reply

    @pytest.mark.parametrize(
        "request_text",
        [
            "which shop is cheapest for milk",
            "whether milk prices are rising",
            "the main place we do groceries",
            "where to buy milk in bulk",
            "know which shop is cheapest",  # a verb-led phrase, if the model slips
        ],
    )
    def test_the_sentence_is_grammatical_for_any_phrasing(self, request_text):
        """An earlier template stripped the leading verb to fit "I can't {x} yet",
        which produced "I can't the main place we do groceries yet". The template
        now takes the phrase as given."""
        reply = handlers._cannot_answer_reply(request_text)
        assert f"you're asking about {request_text}" in reply
        assert "I can't work that out yet" in reply

    def test_trailing_punctuation_is_trimmed(self):
        assert handlers._describe("compare prices.") == "compare prices"
        assert handlers._describe("  which shop?  ") == "which shop"

    def test_an_empty_request_still_gives_a_usable_reply(self):
        reply = handlers._cannot_answer_reply("")
        assert "can't do that yet" in reply
        assert "feature request" in reply

    def test_the_reply_says_what_happens_next(self):
        reply = handlers._cannot_answer_reply("prices between shops")
        assert "noted it" in reply.lower()


class TestExplicitFeatureRequests:
    def test_an_explicit_request_is_acknowledged_by_name(
        self, client: TestClient, app_secret: str, fake_gemini, fake_whatsapp
    ):
        fake_gemini.queue_function_call("request_feature", request="scan barcodes to add items")
        post(
            client,
            text_message("can you add barcode scanning?", message_id="wamid.F1"),
            app_secret,
        )
        reply = fake_whatsapp.texts[-1]
        assert "scan barcodes to add items" in reply
        assert "backlog" in reply


class TestUnclearMessages:
    def test_an_unparseable_message_asks_rather_than_lectures(
        self, client: TestClient, app_secret: str, fake_gemini, fake_whatsapp
    ):
        fake_gemini.queue_function_call("unclear")
        post(client, text_message("asdkjhasd", message_id="wamid.U1"), app_secret)
        reply = fake_whatsapp.texts[-1]
        assert "didn't follow" in reply
        assert "The list" not in reply, "an unclear message is not a request for help"

    def test_unclear_is_distinct_from_cannot_answer(self):
        """One is 'I don't understand', the other 'I understand and can't'.

        Collapsing them is what produced the original bug, and it also decides
        what gets logged to the backlog: a real request is signal, a typo is not.
        """
        declarations = {spec["name"]: spec["description"] for spec in gemini.INTENT_SPECS}
        assert "no discernible intent" in declarations["unclear"]
        assert "cannot_answer" in declarations["unclear"], (
            "the unclear intent should point the model at cannot_answer for real requests"
        )


class TestRepliesGoToTheAskerOnly:
    def test_a_fallback_does_not_buzz_both_phones(
        self, client: TestClient, app_secret: str, fake_gemini, fake_whatsapp
    ):
        fake_gemini.queue_function_call("cannot_answer", request="compare prices")
        post(client, text_message("cheapest shop?", message_id="wamid.R1"), app_secret)
        assert fake_whatsapp.recipients == [MEMBER_PRIMARY]


class TestIntentSpecsAreUsable:
    """The specs are the product's conversational behaviour, so they get checked.

    They are data rather than SDK constructor calls precisely so that a review
    or a test can read them; the model follows them literally, and one careless
    clause already caused a visible product bug.
    """

    def test_every_spec_builds_a_real_declaration(self):
        assert len(gemini._function_declarations()) == len(gemini.INTENT_SPECS)

    def test_every_spec_has_a_name_and_a_description(self):
        for spec in gemini.INTENT_SPECS:
            assert spec["name"] and spec["description"]

    def test_names_are_unique(self):
        names = [spec["name"] for spec in gemini.INTENT_SPECS]
        assert len(names) == len(set(names))

    def test_intents_taking_a_request_require_it(self):
        """cannot_answer and request_feature are useless without the phrasing."""
        for name in ("cannot_answer", "request_feature"):
            spec = next(s for s in gemini.INTENT_SPECS if s["name"] == name)
            assert spec["parameters"]["required"] == ["request"]

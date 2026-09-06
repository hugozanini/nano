"""The webhook contract, including regression tests for the production incident.

The incident, restated so the tests below have a reason to exist: receipt
handling did ~40s of synchronous work on the event loop, Meta timed out and
retried, retries queued behind the blocked loop and got slower (82s at the
peak across 60 concurrent requests), and nothing was idempotent — so seven
deliveries of one receipt produced seven purchases and 84 line items.

Every test in `TestNeverTriggersRedelivery` and `TestIdempotency` exists to keep
one link of that chain broken.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from tests.fakes.whatsapp import (
    MEMBER_PRIMARY,
    MEMBER_SECONDARY,
    OUTSIDER,
    image_message,
    sign,
    signed,
    status_callback,
    text_message,
    unsupported_message,
)


@pytest.fixture
def client(nano_app) -> TestClient:
    return TestClient(nano_app)


def post(client: TestClient, payload: dict, app_secret: str, headers: dict | None = None):
    body, hdrs = signed(payload, app_secret)
    hdrs.update(headers or {})
    return client.post("/webhook", content=body, headers=hdrs)


class TestHealth:
    def test_healthz_makes_no_external_calls(self, client: TestClient):
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestVerificationHandshake:
    def test_correct_token_returns_the_challenge_as_plain_text(self, client: TestClient):
        response = client.get(
            "/webhook",
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "test-verify-token-not-real",
                "hub.challenge": "1987382041",
            },
        )
        assert response.status_code == 200
        assert response.text == "1987382041"

    def test_wrong_token_is_rejected(self, client: TestClient):
        response = client.get(
            "/webhook",
            params={"hub.verify_token": "wrong", "hub.challenge": "123"},
        )
        assert response.status_code == 403

    def test_missing_challenge_does_not_crash(self, client: TestClient):
        """The original called int(None) here and raised a TypeError."""
        response = client.get("/webhook", params={"hub.verify_token": "test-verify-token-not-real"})
        assert response.status_code == 400


class TestSignatureVerification:
    def test_valid_signature_is_accepted(self, client: TestClient, app_secret: str):
        assert post(client, text_message(), app_secret).status_code == 200

    def test_forged_signature_is_rejected(self, client: TestClient):
        response = post(client, text_message(), "the-wrong-secret")
        assert response.status_code == 403
        assert response.json()["error"] == "invalid signature"

    def test_missing_signature_is_rejected(self, client: TestClient):
        body = json.dumps(text_message()).encode()
        response = client.post(
            "/webhook", content=body, headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 403

    def test_signature_is_checked_against_the_raw_bytes(self, client: TestClient, app_secret: str):
        """Signing a re-serialised body must fail; key ordering changes the bytes."""
        payload = text_message()
        raw = json.dumps(payload).encode()
        reserialised = json.dumps(payload, sort_keys=True, indent=2).encode()
        assert raw != reserialised

        response = client.post(
            "/webhook",
            content=raw,
            headers={
                "X-Hub-Signature-256": sign(reserialised, app_secret),
                "Content-Type": "application/json",
            },
        )
        assert response.status_code == 403

    def test_tampered_body_is_rejected(self, client: TestClient, app_secret: str):
        body, headers = signed(text_message("add coffee"), app_secret)
        tampered = body.replace(b"add coffee", b"add caviar")
        response = client.post("/webhook", content=tampered, headers=headers)
        assert response.status_code == 403


class TestSenderAllowlist:
    def test_household_member_is_processed(
        self, client: TestClient, app_secret: str, fake_whatsapp
    ):
        post(client, text_message(sender=MEMBER_PRIMARY), app_secret)
        assert fake_whatsapp.sent, "expected a reply to a household member"

    def test_stranger_is_ignored_silently(self, client: TestClient, app_secret: str, fake_whatsapp):
        """No reply at all: answering costs money and confirms the number is live."""
        response = post(client, text_message(sender=OUTSIDER), app_secret)
        assert response.status_code == 200
        assert response.json()["status"] == "ignored"
        assert fake_whatsapp.sent == []

    def test_number_with_stray_quote_still_matches(
        self, client: TestClient, app_secret, fake_whatsapp
    ):
        """Production shipped HOUSEHOLD_NUMBERS as '"15550100001' for weeks."""
        post(client, text_message(sender=f'"{MEMBER_PRIMARY}'), app_secret)
        assert fake_whatsapp.sent


class TestPayloadShapes:
    def test_status_callback_is_ignored(self, client: TestClient, app_secret: str, fake_whatsapp):
        """These are the majority of inbound traffic and must be cheap."""
        response = post(client, status_callback(), app_secret)
        assert response.status_code == 200
        assert response.json()["status"] == "ignored"
        assert fake_whatsapp.sent == []

    def test_malformed_payload_is_ignored_not_crashed(self, client: TestClient, app_secret: str):
        assert post(client, {"object": "whatsapp_business_account"}, app_secret).status_code == 200

    def test_invalid_json_is_ignored(self, client: TestClient, app_secret: str):
        response = client.post(
            "/webhook",
            content=b"not json at all",
            headers={
                "X-Hub-Signature-256": sign(b"not json at all", app_secret),
                "Content-Type": "application/json",
            },
        )
        assert response.status_code == 200

    def test_unsupported_type_gets_a_polite_reply(
        self, client: TestClient, app_secret: str, fake_whatsapp
    ):
        post(client, unsupported_message(), app_secret)
        assert any("text and receipt photos" in t for t in fake_whatsapp.texts)


class TestNeverTriggersRedelivery:
    """A non-2xx to Meta is a redelivery instruction. Almost nothing earns one."""

    def test_a_failing_send_still_returns_200(
        self, client: TestClient, app_secret: str, fake_whatsapp
    ):
        """The exact shape of the original bug.

        `notify_household` ended in `raise_for_status()`, so one undeliverable
        recipient raised, the handler 500'd, and Meta redelivered the whole
        message. Now a blocked number is a log line.
        """
        fake_whatsapp.block(MEMBER_SECONDARY, error_code=130497)

        response = post(client, text_message("add coffee"), app_secret)

        assert response.status_code == 200
        assert fake_whatsapp.count_to(MEMBER_PRIMARY) == 1, "the reachable member still got it"
        assert fake_whatsapp.count_to(MEMBER_SECONDARY) == 1, "the blocked one was attempted"
        assert fake_whatsapp.sent[1].ok is False

    def test_every_recipient_failing_still_returns_200(
        self, client: TestClient, app_secret: str, fake_whatsapp
    ):
        fake_whatsapp.block(MEMBER_PRIMARY).block(MEMBER_SECONDARY)
        assert post(client, text_message(), app_secret).status_code == 200

    def test_a_gemini_outage_degrades_instead_of_raising(
        self, client: TestClient, app_secret: str, fake_gemini, fake_whatsapp
    ):
        """Classification failure must fall back, not propagate."""
        fake_gemini.raises(RuntimeError("vertex unavailable"))
        response = post(client, text_message(), app_secret)
        assert response.status_code == 200
        assert fake_whatsapp.sent, "expected the get_list fallback reply"

    def test_a_safety_blocked_response_degrades_instead_of_raising(
        self, client: TestClient, app_secret: str, fake_gemini, fake_whatsapp
    ):
        """A safety block returns zero candidates; the original code IndexErrored."""
        fake_gemini.queue_empty()
        response = post(client, text_message(), app_secret)
        assert response.status_code == 200
        assert fake_whatsapp.sent

    def test_a_failing_enqueue_still_returns_200(
        self, client: TestClient, app_secret: str, monkeypatch
    ):
        from nano import tasks

        def explode(*args, **kwargs):
            raise RuntimeError("cloud tasks unavailable")

        monkeypatch.setattr(tasks, "enqueue", explode)
        response = post(client, text_message(), app_secret)
        assert response.status_code == 200
        assert response.json()["status"] == "error"


class TestIdempotency:
    def test_the_same_message_is_processed_once(
        self, client: TestClient, app_secret: str, fake_whatsapp
    ):
        """Seven deliveries of one receipt produced seven purchases in production."""
        payload = text_message("add coffee", message_id="wamid.REPEATED")

        for _ in range(7):
            assert post(client, payload, app_secret).status_code == 200

        assert fake_whatsapp.count_to(MEMBER_PRIMARY) == 1

    def test_a_receipt_is_logged_once_across_redeliveries(
        self, client: TestClient, app_secret: str, fake_bq, fake_whatsapp, receipt, fake_gemini
    ):
        fake_gemini.queue_json(receipt.as_dict())
        payload = image_message(message_id="wamid.RECEIPT1")

        for _ in range(7):
            post(client, payload, app_secret)

        inserts = fake_bq.count_matching("INSERT INTO `nano_test.purchases`")
        assert inserts == 1, f"expected exactly one purchase insert, got {inserts}"

    def test_distinct_messages_are_each_processed(
        self, client: TestClient, app_secret: str, fake_whatsapp
    ):
        post(client, text_message("add coffee", message_id="wamid.A"), app_secret)
        post(client, text_message("add milk", message_id="wamid.B"), app_secret)
        assert fake_whatsapp.count_to(MEMBER_PRIMARY) == 2


class TestReplyRouting:
    def test_state_changes_go_to_the_whole_household(
        self, client: TestClient, app_secret: str, fake_whatsapp
    ):
        post(client, text_message("add coffee"), app_secret)
        assert set(fake_whatsapp.recipients) == {MEMBER_PRIMARY, MEMBER_SECONDARY}

    def test_questions_are_answered_only_to_the_asker(
        self, client: TestClient, app_secret: str, fake_whatsapp, fake_gemini
    ):
        """'what's on the list?' used to buzz both phones."""
        fake_gemini.queue_function_call("get_list")
        post(client, text_message("what's on the list?"), app_secret)
        assert fake_whatsapp.recipients == [MEMBER_PRIMARY]

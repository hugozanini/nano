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
    def test_health_makes_no_external_calls(self, client: TestClient):
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_health_is_not_served_from_the_reserved_healthz_path(self, client: TestClient):
        """Google's frontend intercepts /healthz on *.run.app.

        A deploy's post-deploy check failed against the live service because
        Google answered /healthz with its own HTML 404 before the request
        reached the container, even though FastAPI had the route registered and
        advertised it in /openapi.json. Every other path reached the app.
        """
        from nano.routes import router

        paths = {route.path for route in router.routes}
        assert "/health" in paths
        assert "/healthz" not in paths, (
            "/healthz is intercepted by Google's frontend on Cloud Run and never "
            "reaches the container; use /health"
        )


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


class TestImageCaptions:
    """Regression: a caption on a receipt photo was silently dropped.

    WhatsApp puts it at `message["image"]["caption"]`, not in `text`, so
    sending a receipt with "what did we buy?" attached logged the receipt and
    ignored the question entirely — the person had to ask again.
    """

    def test_a_caption_is_parsed_off_an_image(self):
        from nano.whatsapp import parse_inbound

        payload = image_message()
        payload["entry"][0]["changes"][0]["value"]["messages"][0]["image"]["caption"] = (
            "What did we buy?"
        )
        parsed = parse_inbound(payload)
        assert parsed.is_image
        assert parsed.text == "What did we buy?"

    def test_an_image_without_a_caption_has_empty_text(self):
        from nano.whatsapp import parse_inbound

        assert parse_inbound(image_message()).text == ""

    def test_a_captioned_receipt_is_logged_and_the_caption_answered(
        self, client: TestClient, app_secret: str, fake_gemini, fake_whatsapp, fake_bq, receipt
    ):
        fake_gemini.queue_json(receipt.as_dict())  # extraction
        fake_gemini.queue_json(  # canonicalisation
            [{"canonical": "milk", "category": "dairy"} for _ in receipt.items]
        )
        fake_gemini.queue_function_call("query_history", query_type="items_in_last_purchase")

        payload = image_message(message_id="wamid.CAPTIONED")
        payload["entry"][0]["changes"][0]["value"]["messages"][0]["image"]["caption"] = (
            "What did we buy?"
        )
        response = post(client, payload, app_secret)

        assert response.status_code == 200
        texts = " ".join(fake_whatsapp.texts)
        assert "Logged your receipt" in texts, "the receipt should still be logged"
        assert len(fake_whatsapp.texts) > 1, "the caption should also get an answer"


class TestDuplicateReceiptDetection:
    """Re-photographing the same receipt is a different message, so message-id
    idempotency cannot catch it — and the recorded spend silently doubles.
    One EUR 43.06 shop became EUR 86.12 in production this way."""

    def _script(self, fake_gemini, receipt):
        fake_gemini.queue_json(receipt.as_dict())
        fake_gemini.queue_json([{"canonical": "milk", "category": "dairy"} for _ in receipt.items])

    def test_a_matching_purchase_blocks_a_second_write(
        self, client: TestClient, app_secret: str, fake_gemini, fake_whatsapp, fake_bq, receipt
    ):
        self._script(fake_gemini, receipt)
        fake_bq.when("SELECT purchase_id FROM `nano_test.purchases`", [{"purchase_id": "older"}])

        post(client, image_message(message_id="wamid.RESENT"), app_secret)

        assert fake_bq.count_matching("INSERT INTO `nano_test.purchases`") == 0
        assert any("already logged a receipt" in t for t in fake_whatsapp.texts)
        assert any("doubled your total" in t for t in fake_whatsapp.texts)

    def test_a_new_receipt_is_logged_normally(
        self, client: TestClient, app_secret: str, fake_gemini, fake_whatsapp, fake_bq, receipt
    ):
        self._script(fake_gemini, receipt)
        fake_bq.when("SELECT purchase_id FROM `nano_test.purchases`", [])

        post(client, image_message(message_id="wamid.NEW"), app_secret)

        assert fake_bq.count_matching("INSERT INTO `nano_test.purchases`") == 1

    def test_an_explicit_caption_overrides_the_check(
        self, client: TestClient, app_secret: str, fake_gemini, fake_whatsapp, fake_bq, receipt
    ):
        """Two identical shops on one day is unusual but possible."""
        self._script(fake_gemini, receipt)
        fake_bq.when("SELECT purchase_id FROM `nano_test.purchases`", [{"purchase_id": "older"}])

        payload = image_message(message_id="wamid.FORCED")
        payload["entry"][0]["changes"][0]["value"]["messages"][0]["image"]["caption"] = (
            "log it anyway"
        )
        post(client, payload, app_secret)

        assert fake_bq.count_matching("INSERT INTO `nano_test.purchases`") == 1


class TestReceiptOriginIsRecorded:
    """Provenance must reach the row, not just the log.

    A receipt's country is inferred from its tax table where possible, and the
    evidence and confidence are stored alongside so analysis can tell a
    fingerprint from a guess.
    """

    def _catalan_receipt(self, receipt):
        payload = receipt.as_dict()
        payload.update(
            {
                "receipt_language": "ca",
                "tax_rates": [4, 10, 21],
                "tax_id": "A12345674",
                "store_city": "",
                "store_address": "Carrer Example 1",
                "country_guess": "AD",  # deliberately wrong; VAT should win
            }
        )
        return payload

    def _script(self, fake_gemini, payload, item_count):
        fake_gemini.queue_json(payload)
        fake_gemini.queue_json([{"canonical": "milk", "category": "dairy"}] * item_count)

    def test_country_comes_from_the_tax_table_not_the_model_guess(
        self, client: TestClient, app_secret: str, fake_gemini, fake_bq, receipt
    ):
        self._script(fake_gemini, self._catalan_receipt(receipt), len(receipt.items))
        fake_bq.when("SELECT purchase_id FROM `nano_test.purchases`", [])

        post(client, image_message(message_id="wamid.CA1"), app_secret)

        insert = fake_bq.statements_matching("INSERT INTO `nano_test.purchases`")[-1]
        assert insert.param("country_code") == "ES", "VAT 4/10/21 should outrank a model guess"
        assert insert.param("location_confidence") == "high"
        assert "VAT" in insert.param("location_evidence")

    def test_region_is_recorded_but_city_is_not_invented(
        self, client: TestClient, app_secret: str, fake_gemini, fake_bq, receipt
    ):
        """The design's central rule: no city unless it was printed."""
        self._script(fake_gemini, self._catalan_receipt(receipt), len(receipt.items))
        fake_bq.when("SELECT purchase_id FROM `nano_test.purchases`", [])

        post(client, image_message(message_id="wamid.CA2"), app_secret)

        insert = fake_bq.statements_matching("INSERT INTO `nano_test.purchases`")[-1]
        assert insert.param("region") == "Catalonia"
        assert insert.param("city_printed") == ""
        assert insert.param("city_inferred") == ""

    def test_a_printed_city_is_stored_as_printed(
        self, client: TestClient, app_secret: str, fake_gemini, fake_bq, receipt
    ):
        payload = self._catalan_receipt(receipt)
        payload["store_city"] = "Girona"
        self._script(fake_gemini, payload, len(receipt.items))
        fake_bq.when("SELECT purchase_id FROM `nano_test.purchases`", [])

        post(client, image_message(message_id="wamid.CA3"), app_secret)

        insert = fake_bq.statements_matching("INSERT INTO `nano_test.purchases`")[-1]
        assert insert.param("city_printed") == "Girona"

    def test_tax_rates_and_language_are_stored(
        self, client: TestClient, app_secret: str, fake_gemini, fake_bq, receipt
    ):
        self._script(fake_gemini, self._catalan_receipt(receipt), len(receipt.items))
        fake_bq.when("SELECT purchase_id FROM `nano_test.purchases`", [])

        post(client, image_message(message_id="wamid.CA4"), app_secret)

        insert = fake_bq.statements_matching("INSERT INTO `nano_test.purchases`")[-1]
        assert insert.param("receipt_language") == "ca"
        # The SDK coerces NUMERIC parameters to Decimal, as it does with DATE.
        assert [float(r) for r in insert.param("tax_rates")] == [4.0, 10.0, 21.0]
        assert insert.param("tax_id") == "A12345674"

    def test_a_receipt_with_no_locale_signals_still_logs(
        self, client: TestClient, app_secret: str, fake_gemini, fake_bq, receipt
    ):
        """Missing provenance must never cost us the purchase."""
        self._script(fake_gemini, receipt.as_dict(), len(receipt.items))
        fake_bq.when("SELECT purchase_id FROM `nano_test.purchases`", [])

        post(client, image_message(message_id="wamid.BARE"), app_secret)

        assert fake_bq.count_matching("INSERT INTO `nano_test.purchases`") == 1
        insert = fake_bq.statements_matching("INSERT INTO `nano_test.purchases`")[-1]
        assert insert.param("country_code") == ""
        assert insert.param("location_confidence") == "none"

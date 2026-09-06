"""Tests for the test harness itself.

Every later PR asserts behaviour through these fakes, so the fakes have to
match the real client semantics that Nano depends on. The critical one is
`FakeDocumentReference.create()` raising AlreadyExists — the whole webhook
idempotency design rests on that being an atomic, failing-on-conflict write.
"""

from __future__ import annotations

import json

import pytest
from google.api_core import exceptions as gcp_exceptions

from tests.fakes.bigquery import FakeBigQueryClient
from tests.fakes.firestore import FakeFirestoreClient
from tests.fakes.gemini import FakeGeminiModel
from tests.fakes.whatsapp import (
    MEMBER_PRIMARY,
    MEMBER_SECONDARY,
    FakeWhatsAppClient,
    image_message,
    signed,
    status_callback,
    text_message,
    unsupported_message,
)


class TestFakeFirestore:
    def test_get_missing_document_reports_not_exists(self, fake_firestore: FakeFirestoreClient):
        snap = fake_firestore.collection("lists").document("shared_list").get()
        assert snap.exists is False
        assert snap.to_dict() is None

    def test_set_then_get_roundtrips(self, fake_firestore: FakeFirestoreClient):
        ref = fake_firestore.collection("lists").document("shared_list")
        ref.set({"items": ["coffee", "milk"]})
        assert ref.get().to_dict() == {"items": ["coffee", "milk"]}

    def test_set_replaces_whole_document(self, fake_firestore: FakeFirestoreClient):
        """Matches the real client: set() without merge is a full replace.

        This is the semantics behind the lost-update bug in the current app.
        """
        ref = fake_firestore.collection("lists").document("shared_list")
        ref.set({"items": ["coffee"], "updated_at": "t1"})
        ref.set({"items": ["milk"]})
        assert ref.get().to_dict() == {"items": ["milk"]}

    def test_set_with_merge_preserves_other_fields(self, fake_firestore: FakeFirestoreClient):
        ref = fake_firestore.collection("lists").document("shared_list")
        ref.set({"items": ["coffee"], "updated_at": "t1"})
        ref.set({"items": ["milk"]}, merge=True)
        assert ref.get().to_dict() == {"items": ["milk"], "updated_at": "t1"}

    def test_create_succeeds_once(self, fake_firestore: FakeFirestoreClient):
        ref = fake_firestore.collection("processed_messages").document("wamid.ABC")
        ref.create({"seen": True})
        assert ref.get().exists is True

    def test_create_twice_raises_already_exists(self, fake_firestore: FakeFirestoreClient):
        """The atomic claim primitive the dedupe guard is built on."""
        ref = fake_firestore.collection("processed_messages").document("wamid.ABC")
        ref.create({"seen": True})
        with pytest.raises(gcp_exceptions.AlreadyExists):
            ref.create({"seen": True})

    def test_update_on_missing_document_raises_not_found(self, fake_firestore: FakeFirestoreClient):
        ref = fake_firestore.collection("lists").document("nope")
        with pytest.raises(gcp_exceptions.NotFound):
            ref.update({"items": []})

    def test_stored_data_is_isolated_from_caller_mutation(
        self, fake_firestore: FakeFirestoreClient
    ):
        payload = {"items": ["coffee"]}
        ref = fake_firestore.collection("lists").document("shared_list")
        ref.set(payload)
        payload["items"].append("mutated after write")
        assert ref.get().to_dict() == {"items": ["coffee"]}

    def test_writes_are_recorded_for_assertions(self, fake_firestore: FakeFirestoreClient):
        ref = fake_firestore.collection("lists").document("shared_list")
        ref.set({"items": []})
        ref.set({"items": ["milk"]})
        assert fake_firestore.write_count("lists/shared_list") == 2

    def test_transaction_runs_read_modify_write_inline(self, fake_firestore: FakeFirestoreClient):
        ref = fake_firestore.collection("lists").document("shared_list")
        ref.set({"items": ["coffee"]})

        def add_milk(tx, doc_ref):
            current = tx.get(doc_ref).to_dict()["items"]
            tx.set(doc_ref, {"items": [*current, "milk"]})
            return current

        fake_firestore.transaction()(add_milk, ref)
        assert ref.get().to_dict()["items"] == ["coffee", "milk"]


class TestFakeBigQuery:
    def test_unscripted_query_returns_no_rows(self, fake_bq: FakeBigQueryClient):
        assert fake_bq.query("SELECT 1").result() == []

    def test_scripted_rows_support_attribute_access(self, fake_bq: FakeBigQueryClient):
        fake_bq.when("FROM `nano_test.purchases`", [{"total": 43.06, "trips": 1}])
        row = fake_bq.query(
            "SELECT SUM(x) total, COUNT(*) trips FROM `nano_test.purchases`"
        ).result()[0]
        assert row.total == 43.06
        assert row.trips == 1

    def test_matching_ignores_sql_whitespace(self, fake_bq: FakeBigQueryClient):
        fake_bq.when("SELECT a FROM t", [{"a": 1}])
        rows = fake_bq.query("""
            SELECT a
            FROM t
        """).result()
        assert rows[0].a == 1

    def test_statements_are_counted(self, fake_bq: FakeBigQueryClient):
        """Backs the N+1 regression test: one INSERT per receipt, not one per item."""
        for _ in range(3):
            fake_bq.query("INSERT INTO `nano_test.purchase_items` VALUES (1)")
        assert fake_bq.count_matching("INSERT INTO `nano_test.purchase_items`") == 3

    def test_scripted_error_is_raised_on_result(self, fake_bq: FakeBigQueryClient):
        fake_bq.fail("SELECT boom", RuntimeError("bq exploded"))
        with pytest.raises(RuntimeError, match="bq exploded"):
            fake_bq.query("SELECT boom").result()

    def test_query_parameters_are_captured(self, fake_bq: FakeBigQueryClient):
        """Note the coercion: the SDK turns DATE strings into datetime.date.

        The fake preserves that, so date-range tests assert on real date objects
        rather than the strings that were passed in.
        """
        import datetime

        from google.cloud import bigquery

        fake_bq.query(
            "SELECT * FROM t WHERE d BETWEEN @start AND @end",
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("start", "DATE", "2026-09-01"),
                    bigquery.ScalarQueryParameter("end", "DATE", "2026-09-06"),
                ]
            ),
        )
        assert fake_bq.queries[-1].param("start") == datetime.date(2026, 9, 1)
        assert fake_bq.queries[-1].param("end") == datetime.date(2026, 9, 6)

    def test_array_parameters_are_flattened(self, fake_bq: FakeBigQueryClient):
        """The batched INSERT ... UNNEST(@rows) path binds an array parameter."""
        from google.cloud import bigquery

        fake_bq.query(
            "INSERT INTO t SELECT * FROM UNNEST(@rows)",
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ArrayQueryParameter("rows", "STRING", ["a", "b", "c"]),
                ]
            ),
        )
        assert fake_bq.queries[-1].param("rows") == ["a", "b", "c"]


class TestFakeGemini:
    def test_function_call_response(self, fake_gemini: FakeGeminiModel):
        fake_gemini.queue_function_call("add_items", items=["coffee"])
        part = fake_gemini.generate_content("add coffee").candidates[0].content.parts[0]
        assert part.function_call.name == "add_items"
        assert part.function_call.args == {"items": ["coffee"]}

    def test_json_response_parses(self, fake_gemini: FakeGeminiModel):
        fake_gemini.queue_json({"store_name": "SUPERMERCAT EXEMPLE, S.L."})
        assert json.loads(fake_gemini.generate_content("read this").text)["store_name"] == (
            "SUPERMERCAT EXEMPLE, S.L."
        )

    def test_responses_are_returned_in_order(self, fake_gemini: FakeGeminiModel):
        fake_gemini.queue_text("first").queue_text("second")
        assert fake_gemini.generate_content("a").text == "first"
        assert fake_gemini.generate_content("b").text == "second"

    def test_last_response_repeats_once_queue_is_drained(self, fake_gemini: FakeGeminiModel):
        fake_gemini.queue_text("only")
        assert fake_gemini.generate_content("a").text == "only"
        assert fake_gemini.generate_content("b").text == "only"

    def test_empty_candidates_response_is_scriptable(self, fake_gemini: FakeGeminiModel):
        """Safety blocks return no candidates; today that IndexErrors in main.py."""
        fake_gemini.queue_empty()
        assert fake_gemini.generate_content("blocked").candidates == []

    def test_unscripted_call_fails_loudly(self, fake_gemini: FakeGeminiModel):
        with pytest.raises(AssertionError, match="unscripted call"):
            fake_gemini.generate_content("nothing queued")

    def test_prompts_are_recorded(self, fake_gemini: FakeGeminiModel):
        fake_gemini.queue_text("ok")
        fake_gemini.generate_content("classify: add coffee")
        assert "add coffee" in fake_gemini.last_prompt
        assert fake_gemini.call_count == 1

    def test_chat_records_history(self, fake_gemini: FakeGeminiModel):
        fake_gemini.queue_text("ok")
        chat = fake_gemini.start_chat()
        chat.send_message("hello")
        assert chat.history == ["hello"]


class TestFakeWhatsApp:
    def test_send_records_recipient_and_text(self, fake_whatsapp: FakeWhatsAppClient):
        fake_whatsapp.send_text(MEMBER_PRIMARY, "Added! coffee")
        assert fake_whatsapp.recipients == [MEMBER_PRIMARY]
        assert fake_whatsapp.texts == ["Added! coffee"]

    def test_blocked_number_reports_failure_without_raising(
        self, fake_whatsapp: FakeWhatsAppClient
    ):
        """Models Meta error 130497 — the failure must be a return value, not an exception."""
        fake_whatsapp.block(MEMBER_SECONDARY)
        assert fake_whatsapp.send_text(MEMBER_PRIMARY, "hi") is True
        assert fake_whatsapp.send_text(MEMBER_SECONDARY, "hi") is False
        assert fake_whatsapp.count_to(MEMBER_PRIMARY) == 1

    def test_media_download_roundtrips(self, fake_whatsapp: FakeWhatsAppClient):
        fake_whatsapp.stage_media("MEDIA_1", b"\xff\xd8jpegbytes", "image/jpeg")
        content, mime = fake_whatsapp.download_media("MEDIA_1")
        assert content == b"\xff\xd8jpegbytes"
        assert mime == "image/jpeg"


class TestInboundPayloads:
    def test_text_payload_shape(self):
        msg = text_message("add coffee")["entry"][0]["changes"][0]["value"]["messages"][0]
        assert msg["type"] == "text"
        assert msg["text"]["body"] == "add coffee"
        assert msg["from"] == MEMBER_PRIMARY
        assert msg["id"] == "wamid.TEST0001"

    def test_image_payload_shape(self):
        msg = image_message()["entry"][0]["changes"][0]["value"]["messages"][0]
        assert msg["type"] == "image"
        assert msg["image"]["id"] == "MEDIA_1"

    def test_unsupported_payload_shape(self):
        msg = unsupported_message()["entry"][0]["changes"][0]["value"]["messages"][0]
        assert msg["type"] == "audio"

    def test_status_callback_has_no_messages_key(self):
        """Nano receives several of these per real message; they must be ignored fast."""
        value = status_callback()["entry"][0]["changes"][0]["value"]
        assert "messages" not in value
        assert value["statuses"][0]["status"] == "delivered"

    def test_signature_is_over_the_exact_bytes_sent(self, app_secret: str):
        body, headers = signed(text_message(), app_secret)
        assert headers["X-Hub-Signature-256"].startswith("sha256=")

        import hashlib
        import hmac

        expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
        assert headers["X-Hub-Signature-256"] == f"sha256={expected}"

    def test_signature_differs_for_different_bodies(self, app_secret: str):
        _, h1 = signed(text_message("add coffee"), app_secret)
        _, h2 = signed(text_message("add milk"), app_secret)
        assert h1["X-Hub-Signature-256"] != h2["X-Hub-Signature-256"]


class TestReceiptFixture:
    def test_line_totals_sum_to_the_stated_total(self, receipt):
        """Guards the fixture itself: 8 synthetic items summing to EUR 21.70."""
        assert len(receipt.items) == 8
        assert round(sum(i["line_total"] for i in receipt.items), 2) == 21.70
        assert receipt.total_amount == 21.70

    def test_purchase_date_is_the_month_before_the_query_date(self, receipt):
        """The 0.00 bug shape: bought in one month, asked about the next one."""
        from datetime import date

        from tests.fakes.whatsapp import QUERY_DATE

        bought = date.fromisoformat(receipt.purchase_date)
        asked = date.fromisoformat(QUERY_DATE)
        assert (bought.year, bought.month) < (asked.year, asked.month)

    def test_contains_non_english_and_non_grocery_items(self, receipt):
        """The normalization case: Catalan names, plus batteries filed as groceries."""
        names = [i["item_name"] for i in receipt.items]
        assert "LLET SENCERA 1L" in names  # milk, in Catalan
        assert "POMA GOLDEN 1KG" in names  # apples
        assert "PILES AAA P-4" in names  # batteries, not food


class TestGuardrails:
    def test_unit_tests_cannot_make_real_http_calls(self):
        import requests

        with pytest.raises(AssertionError, match="real HTTP request"):
            requests.post("https://graph.facebook.com/v20.0/x/messages")

    def test_environment_points_at_test_resources_not_production(self):
        import os

        assert os.environ["BQ_DATASET"] == "nano_test"
        assert os.environ["FIRESTORE_PREFIX"] == "nano_test_"

    def test_environment_contains_no_real_project_identifiers(self):
        import os

        assert os.environ["GOOGLE_CLOUD_PROJECT"] == "test-project"
        assert os.environ["RECEIPTS_BUCKET"] == "test-receipts-bucket"

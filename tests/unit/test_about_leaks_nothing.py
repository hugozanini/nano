"""What Nano says about itself must never carry infrastructure with it.

The existing scanner checks the repository. This checks the other direction:
text Nano *sends*, to a chat that gets screenshotted. "How do you work?" is the
single most likely exchange to appear in an article, and the object that would
answer it — `Settings` — also holds the WhatsApp token, the app secret, the
verify token, the project id, the receipts bucket and two service-account
emails.

So the answers are an allowlist written by hand, and these tests assert that no
route from configuration to the chat exists. Armed with real values via
`NANO_SECRET_SCAN_VALUES`, exactly like the repository scanner, so CI checks the
actual identifiers without any of them living in this file.
"""

from __future__ import annotations

import os
import re

import pytest
from nano import about
from nano.config import load_settings

SENSITIVE_FIELDS = [
    "whatsapp_token",
    "whatsapp_app_secret",
    "verify_token",
    "phone_number_id",
    "project_id",
    "receipts_bucket",
    "tasks_invoker_sa",
    "tasks_target_url",
    "analytics_service_account",
    "firestore_prefix",
]


@pytest.fixture
def settings(monkeypatch):
    """Config filled with values distinctive enough to spot in any output."""
    monkeypatch.setenv("VERIFY_TOKEN", "canary-verify-token")
    monkeypatch.setenv("WHATSAPP_TOKEN", "canary-whatsapp-token")
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "canary-app-secret")
    monkeypatch.setenv("PHONE_NUMBER_ID", "000000000000000")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "canary-project-id")
    monkeypatch.setenv("RECEIPTS_BUCKET", "canary-bucket")
    monkeypatch.setenv("TASKS_INVOKER_SA", "canary-sa@canary-project-id.iam.gserviceaccount.com")
    monkeypatch.setenv(
        "ANALYTICS_SERVICE_ACCOUNT", "canary-analytics@canary.iam.gserviceaccount.com"
    )
    monkeypatch.setenv("HOUSEHOLD_MEMBERS", "15550100001:member_primary")
    return load_settings()


def every_answer(settings) -> dict[str, str]:
    return {topic: about.answer(topic, settings) for topic in sorted(about.TOPICS)}


class TestNoConfigurationReachesTheChat:
    def test_no_sensitive_field_value_appears_in_any_answer(self, settings):
        for topic, text in every_answer(settings).items():
            for field in SENSITIVE_FIELDS:
                value = str(getattr(settings, field, "") or "")
                if len(value) < 6:
                    continue
                assert value not in text, f"{topic} leaked {field}"

    def test_no_household_number_or_name_appears(self, settings):
        for topic, text in every_answer(settings).items():
            for number, name in settings.household.items():
                assert number not in text, f"{topic} leaked a phone number"
                assert name not in text.lower(), f"{topic} leaked a household name"

    def test_no_answer_contains_a_long_digit_run(self, settings):
        """Catches an identifier this test did not think to name."""
        for topic, text in every_answer(settings).items():
            assert not re.search(r"\d{7,}", text), f"{topic} contains a long digit run"

    def test_no_answer_contains_a_service_account_address(self, settings):
        for topic, text in every_answer(settings).items():
            assert "gserviceaccount.com" not in text, f"{topic} names a service account"

    def test_no_answer_contains_a_url(self, settings):
        for topic, text in every_answer(settings).items():
            assert "http://" not in text and "https://" not in text, f"{topic} contains a URL"

    @pytest.mark.skipif(
        not os.environ.get("NANO_SECRET_SCAN_VALUES"),
        reason="set NANO_SECRET_SCAN_VALUES to check against the real identifiers",
    )
    def test_no_real_identifier_appears(self):
        """Armed in CI and before a PR; reports the topic, never the value."""
        real = [v.strip() for v in os.environ["NANO_SECRET_SCAN_VALUES"].split(",") if v.strip()]
        for topic, text in every_answer(load_settings()).items():
            for index, value in enumerate(real):
                assert value.lower() not in text.lower(), (
                    f"{topic} contains secret-scan value #{index}"
                )


class TestTheModelNameIsTheOnlyRuntimeValue:
    def test_the_configured_model_is_reported(self, monkeypatch, settings):
        """Hardcoding it would go stale at the next model upgrade."""
        monkeypatch.setenv("GEMINI_MODEL", "claude-of-the-future-1")
        refreshed = load_settings()
        assert "claude-of-the-future-1" in about.answer(about.WHICH_MODEL, refreshed)

    def test_the_model_name_appears_nowhere_else_as_a_literal(self):
        source = about.__file__ or ""
        with open(source, encoding="utf-8") as handle:
            body = handle.read()
        assert "gemini-2.5" not in body, "read the model from config, don't write it down"


class TestItAnswersTheQuestionThatWasAsked:
    def test_every_topic_produces_text(self, settings):
        for topic, text in every_answer(settings).items():
            assert len(text) > 40, f"{topic} is not an answer"

    def test_an_unknown_topic_falls_back_rather_than_failing(self, settings):
        """The model picked a word outside the enum; not the user's problem."""
        assert about.answer("what_is_your_favourite_colour", settings)

    def test_privacy_is_answered_honestly(self, settings):
        """ "Do you keep my messages?" deserves a straight yes."""
        text = about.answer(about.DATA_PRIVACY, settings).lower()
        assert "yes" in text
        assert "receipt" in text
        assert "train" in text, "whether it trains on the chat is the actual worry"

    def test_privacy_says_the_household_shares_everything(self, settings):
        text = about.answer(about.DATA_PRIVACY, settings).lower()
        assert "both" in text

    def test_which_model_names_the_provider_not_just_the_model(self, settings):
        assert "Vertex AI" in about.answer(about.WHICH_MODEL, settings)

    def test_it_is_never_a_feature_request(self, settings):
        for topic, text in every_answer(settings).items():
            assert "feature request" not in text, f"{topic} filed itself as missing"


class TestTheIntentIsReachable:
    def test_about_nano_is_declared(self):
        from nano import gemini

        assert any(s["name"] == "about_nano" for s in gemini.INTENT_SPECS)

    def test_every_declared_topic_has_an_answer(self):
        """An enum value with no branch would silently fall back."""
        from nano import gemini

        spec = next(s for s in gemini.INTENT_SPECS if s["name"] == "about_nano")
        declared = set(spec["parameters"]["properties"]["topic"]["enum"])
        assert declared == about.TOPICS

    def test_the_description_rules_out_cannot_answer(self):
        from nano import gemini

        spec = next(s for s in gemini.INTENT_SPECS if s["name"] == "about_nano")
        assert "never cannot_answer" in spec["description"]

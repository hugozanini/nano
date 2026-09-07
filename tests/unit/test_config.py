"""Configuration parsing, especially household numbers.

`parse_household` carries a real production scar. `deploy-cloudrun`'s `env_vars`
input is comma-delimited, so a comma-separated secret is split before anything
else sees it. In production this turned one variable into three:

    HOUSEHOLD_NUMBERS = '"15550100001'   <- truncated, with a literal quote
    <second number>"                     <- became its own env var
    <third number>"                      <- and so did this

which meant the service messaged one number, with a stray `"` on the front, and
never contacted the second household member at all. These tests pin down the
parsing so the app survives that class of mangling rather than silently
misbehaving.
"""

from __future__ import annotations

import os
from typing import ClassVar

import pytest
from nano import config as config_module
from nano.config import ConfigError, load_settings, parse_household

from tests.fakes.whatsapp import MEMBER_PRIMARY, MEMBER_SECONDARY


class TestParseHousehold:
    @pytest.mark.parametrize("sep", [";", ",", "|"])
    def test_members_form_with_names(self, sep: str):
        """Semicolon is what the deploy pipeline uses, because a comma is the
        one character the CI action splits values on. All three must parse."""
        result = parse_household(f"{MEMBER_PRIMARY}:alex{sep}{MEMBER_SECONDARY}:sam", "")
        assert result == {MEMBER_PRIMARY: "alex", MEMBER_SECONDARY: "sam"}

    @pytest.mark.parametrize("sep", [";", ",", "|"])
    def test_legacy_numbers_form_accepts_any_separator(self, sep: str):
        result = parse_household("", f"{MEMBER_PRIMARY}{sep}{MEMBER_SECONDARY}")
        assert set(result) == {MEMBER_PRIMARY, MEMBER_SECONDARY}

    def test_legacy_numbers_form_gets_generated_names(self):
        result = parse_household("", f"{MEMBER_PRIMARY},{MEMBER_SECONDARY}")
        assert list(result) == [MEMBER_PRIMARY, MEMBER_SECONDARY]
        assert all(name.startswith("member_") for name in result.values())

    def test_members_form_takes_precedence(self):
        result = parse_household(f"{MEMBER_PRIMARY}:alex", MEMBER_SECONDARY)
        assert result == {MEMBER_PRIMARY: "alex"}

    @pytest.mark.parametrize(
        "raw",
        [
            f'"{MEMBER_PRIMARY}',  # the exact production shape
            f'{MEMBER_PRIMARY}"',
            f'"{MEMBER_PRIMARY}"',
            f"  {MEMBER_PRIMARY}  ",
            f"'{MEMBER_PRIMARY}'",
            f"+{MEMBER_PRIMARY}",
            f"+{MEMBER_PRIMARY[:2]} {MEMBER_PRIMARY[2:5]} {MEMBER_PRIMARY[5:]}",
        ],
    )
    def test_wrappers_and_formatting_are_stripped(self, raw: str):
        """All of these must resolve to the same recipient."""
        assert parse_household("", raw) == {MEMBER_PRIMARY: "member_1"}

    def test_quoted_multi_number_secret_survives(self):
        result = parse_household("", f'"{MEMBER_PRIMARY},{MEMBER_SECONDARY}"')
        assert set(result) == {MEMBER_PRIMARY, MEMBER_SECONDARY}

    def test_empty_and_junk_entries_are_dropped(self):
        result = parse_household("", f"{MEMBER_PRIMARY},,   ,not-a-number-at-all,")
        assert set(result) == {MEMBER_PRIMARY}

    def test_no_input_yields_nothing(self):
        assert parse_household("", "") == {}


class TestSettingsMatching:
    def test_is_household_ignores_wrappers(self, monkeypatch):
        monkeypatch.setenv("HOUSEHOLD_MEMBERS", f"{MEMBER_PRIMARY}:alex")
        settings = load_settings()
        assert settings.is_household(MEMBER_PRIMARY)
        assert settings.is_household(f'"{MEMBER_PRIMARY}')
        assert settings.is_household(f"+{MEMBER_PRIMARY}")
        assert not settings.is_household("15550109999")

    def test_table_and_collection_are_namespaced(self, monkeypatch):
        monkeypatch.setenv("BQ_DATASET", "nano_test")
        monkeypatch.setenv("FIRESTORE_PREFIX", "nano_test_")
        settings = load_settings()
        assert settings.table("purchases") == "nano_test.purchases"
        assert settings.collection("lists") == "nano_test_lists"


class TestValidation:
    def test_missing_verify_token_is_fatal(self, monkeypatch):
        monkeypatch.delenv("VERIFY_TOKEN", raising=False)
        with pytest.raises(ConfigError, match="VERIFY_TOKEN"):
            load_settings()

    def test_no_household_is_fatal(self, monkeypatch):
        monkeypatch.delenv("HOUSEHOLD_MEMBERS", raising=False)
        monkeypatch.delenv("HOUSEHOLD_NUMBERS", raising=False)
        with pytest.raises(ConfigError, match="household"):
            load_settings()

    def test_bad_task_backend_is_fatal(self, monkeypatch):
        monkeypatch.setenv("TASK_BACKEND", "carrier_pigeon")
        with pytest.raises(ConfigError, match="TASK_BACKEND"):
            load_settings()

    def test_cloud_tasks_requires_its_settings(self, monkeypatch):
        monkeypatch.setenv("TASK_BACKEND", "cloud_tasks")
        monkeypatch.delenv("TASKS_TARGET_URL", raising=False)
        with pytest.raises(ConfigError, match="TASKS_TARGET_URL"):
            load_settings()


class TestSignatureEnforcement:
    def test_enforced_by_default_when_the_secret_exists(self, monkeypatch):
        monkeypatch.setenv("WHATSAPP_APP_SECRET", "a-secret")
        assert load_settings().require_signature is True

    def test_disabled_when_no_secret_is_configured(self, monkeypatch):
        """A deliberate migration path: introducing verification must not
        black-hole live traffic before the secret exists. It logs CRITICAL."""
        monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)
        assert load_settings().require_signature is False

    def test_explicitly_requiring_it_without_a_secret_is_fatal(self, monkeypatch):
        monkeypatch.delenv("WHATSAPP_APP_SECRET", raising=False)
        monkeypatch.setenv("REQUIRE_SIGNATURE", "true")
        with pytest.raises(ConfigError, match="WHATSAPP_APP_SECRET"):
            load_settings()


class TestCloudRunEnvironment:
    """Model the environment Cloud Run actually provides, not an assumed one.

    A deploy failed at startup with `GOOGLE_CLOUD_PROJECT is required when
    TASK_BACKEND=cloud_tasks`, because Cloud Run injects only PORT and the
    K_* variables — never GOOGLE_CLOUD_PROJECT. An earlier smoke check passed
    because it set that variable itself, which tested the assumption rather
    than the platform.
    """

    CLOUD_RUN_INJECTED: ClassVar[dict[str, str]] = {
        "PORT": "8080",
        "K_SERVICE": "nano-webhook",
        "K_REVISION": "nano-webhook-00010-abc",
        "K_CONFIGURATION": "nano-webhook",
    }

    @pytest.fixture
    def cloud_run_env(self, monkeypatch):
        """Only what Cloud Run injects, plus what the deploy sets explicitly."""
        for key in list(os.environ):
            if key.startswith(("GOOGLE_", "K_", "VERTEX_", "TASK", "HOUSEHOLD_")):
                monkeypatch.delenv(key, raising=False)
        for key, value in self.CLOUD_RUN_INJECTED.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("VERIFY_TOKEN", "token")
        monkeypatch.setenv("PHONE_NUMBER_ID", "000000000000000")
        monkeypatch.setenv("HOUSEHOLD_MEMBERS", f"{MEMBER_PRIMARY}:alex")
        monkeypatch.setenv("TASK_BACKEND", "cloud_tasks")
        monkeypatch.setenv("TASKS_TARGET_URL", "https://example.invalid/tasks/process")
        monkeypatch.setenv("TASKS_INVOKER_SA", "invoker@example.invalid")
        return monkeypatch

    def test_project_is_detected_when_the_platform_does_not_set_it(
        self, cloud_run_env, monkeypatch
    ):
        """Cloud Run never sets GOOGLE_CLOUD_PROJECT; ADC knows it anyway."""
        monkeypatch.setattr(config_module, "detect_project_id", lambda: "detected-project")
        settings = load_settings()
        assert settings.project_id == "detected-project"

    def test_an_explicit_project_wins_over_detection(self, cloud_run_env, monkeypatch):
        monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "explicit-project")
        monkeypatch.setattr(config_module, "detect_project_id", lambda: "detected-project")
        assert load_settings().project_id == "explicit-project"

    def test_startup_still_fails_loudly_if_the_project_cannot_be_resolved(
        self, cloud_run_env, monkeypatch
    ):
        """Detection failing must be a clear error, not a mystery at first use."""
        monkeypatch.setattr(config_module, "detect_project_id", lambda: "")
        with pytest.raises(ConfigError, match="GOOGLE_CLOUD_PROJECT"):
            load_settings()

    def test_detection_never_raises(self, monkeypatch):
        """A metadata-server hiccup must not crash startup."""

        def explode():
            raise RuntimeError("metadata server unreachable")

        import google.auth

        monkeypatch.setattr(google.auth, "default", explode)
        assert config_module.detect_project_id() == ""

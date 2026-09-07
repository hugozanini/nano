"""Shared fixtures, production-data guardrails, and secret hygiene.

Two separate protections live here, and they guard different things.

1. **Guardrails** stop a test from reaching live household data. Integration
   tests run against the real project with real credentials, so a careless
   fixture could overwrite the shared shopping list or write junk into the
   production dataset that spend queries then report.

2. **Secret hygiene** stops real identifiers from ever being committed. Nothing
   in the test suite hardcodes a real phone number, business ID, project ID or
   token. Unit tests use obviously-synthetic placeholders; the integration tier
   reads real values from the environment, supplied locally by the developer's
   shell or in CI by GitHub secrets. See `tests/unit/test_no_secrets_leaked.py`,
   which enforces this automatically.
"""

from __future__ import annotations

import os

import pytest

from tests.fakes.bigquery import FakeBigQueryClient
from tests.fakes.firestore import FakeFirestoreClient
from tests.fakes.gemini import FakeGeminiModel
from tests.fakes.storage import FakeStorageClient
from tests.fakes.whatsapp import (
    MEMBER_PRIMARY,
    MEMBER_SECONDARY,
    PHONE_NUMBER_ID,
    FakeWhatsAppClient,
    ReceiptFixture,
)

# Live resources that tests must never touch, by name.
PROD_BQ_DATASET = "nano"
PROD_LIST_DOC = "lists/shared_list"

TEST_BQ_DATASET = "nano_test"
TEST_FIRESTORE_PREFIX = "nano_test_"

# Synthetic credentials. These are not real and are not expected to work.
APP_SECRET = "test-app-secret-not-real"
VERIFY_TOKEN = "test-verify-token-not-real"
ACCESS_TOKEN = "test-access-token-not-real"

# Placeholder infrastructure names for unit tests.
TEST_PROJECT = "test-project"
TEST_RECEIPTS_BUCKET = "test-receipts-bucket"


# --------------------------------------------------------------------------
# Environment
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def test_env(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic, entirely synthetic config for unit tests.

    Integration tests are skipped here — they get their configuration from the
    real environment (see `tests/integration/conftest.py`) because they need
    credentials and real resource names that must not live in the repo.
    """
    if "integration" in request.keywords:
        return

    env = {
        "VERIFY_TOKEN": VERIFY_TOKEN,
        "WHATSAPP_ACCESS_TOKEN": ACCESS_TOKEN,
        "WHATSAPP_APP_SECRET": APP_SECRET,
        "PHONE_NUMBER_ID": PHONE_NUMBER_ID,
        # Household members addressed by role, using fiction-reserved numbers.
        "HOUSEHOLD_MEMBERS": f"{MEMBER_PRIMARY}:member_primary,{MEMBER_SECONDARY}:member_secondary",
        "GOOGLE_CLOUD_PROJECT": TEST_PROJECT,
        "VERTEX_LOCATION": "us-central1",
        "BQ_DATASET": TEST_BQ_DATASET,
        "FIRESTORE_PREFIX": TEST_FIRESTORE_PREFIX,
        "HOUSEHOLD_TIMEZONE": "Europe/Madrid",
        "DEFAULT_CURRENCY": "EUR",
        "RECEIPTS_BUCKET": TEST_RECEIPTS_BUCKET,
        "TASK_BACKEND": "inline",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)


# --------------------------------------------------------------------------
# Guardrails
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def block_production_targets(request: pytest.FixtureRequest) -> None:
    """Fail loudly if a test is configured to touch live household data."""
    if "integration" not in request.keywords:
        return

    dataset = os.environ.get("BQ_DATASET")
    prefix = os.environ.get("FIRESTORE_PREFIX", "")

    if dataset == PROD_BQ_DATASET:
        pytest.fail(
            f"Integration test would target the PRODUCTION dataset `{PROD_BQ_DATASET}`. "
            f"BQ_DATASET must be `{TEST_BQ_DATASET}`.",
            pytrace=False,
        )
    if not prefix or not prefix.startswith(TEST_FIRESTORE_PREFIX):
        pytest.fail(
            f"Integration test would write to unprefixed Firestore collections "
            f"(prefix={prefix!r}); it must start with `{TEST_FIRESTORE_PREFIX}` so it "
            f"cannot reach `{PROD_LIST_DOC}`.",
            pytrace=False,
        )


@pytest.fixture(autouse=True)
def no_accidental_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Unit tests must not make real HTTP calls; a hang in CI is worse than a failure."""
    if "integration" in request.keywords:
        return

    import requests

    def _blocked(*args, **kwargs):
        raise AssertionError(
            "A unit test attempted a real HTTP request. Use the FakeWhatsAppClient "
            "fixture, or mark the test with @pytest.mark.integration."
        )

    for verb in ("get", "post", "put", "delete", "patch", "request"):
        monkeypatch.setattr(requests, verb, _blocked)
    monkeypatch.setattr(requests.Session, "request", _blocked)


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


@pytest.fixture
def fake_firestore() -> FakeFirestoreClient:
    return FakeFirestoreClient()


@pytest.fixture
def fake_bq() -> FakeBigQueryClient:
    return FakeBigQueryClient()


@pytest.fixture
def fake_gemini() -> FakeGeminiModel:
    return FakeGeminiModel()


@pytest.fixture
def fake_whatsapp() -> FakeWhatsAppClient:
    return FakeWhatsAppClient()


@pytest.fixture
def fake_storage() -> FakeStorageClient:
    return FakeStorageClient()


@pytest.fixture
def app_secret() -> str:
    return APP_SECRET


@pytest.fixture
def receipt() -> ReceiptFixture:
    """A synthetic Catalan-language receipt: 8 items, EUR 21.70, one non-grocery."""
    return ReceiptFixture()


# --------------------------------------------------------------------------
# The application, wired to fakes
# --------------------------------------------------------------------------


@pytest.fixture
def nano_app(
    monkeypatch: pytest.MonkeyPatch,
    fake_firestore: FakeFirestoreClient,
    fake_bq: FakeBigQueryClient,
    fake_gemini: FakeGeminiModel,
    fake_whatsapp: FakeWhatsAppClient,
    fake_storage: FakeStorageClient,
):
    """A real FastAPI app with every outbound dependency replaced.

    Routing, signature checking, dedupe, threadpool behaviour and error handling
    are all the production code paths; only the four external services are fake.
    TASK_BACKEND is `inline`, so background work runs through FastAPI's
    BackgroundTasks and completes before TestClient returns.
    """
    from nano import clients, config, gemini
    from nano import whatsapp as wa_module

    config.reset_settings_cache()
    clients.reset_clients()
    gemini.reset_models()

    monkeypatch.setattr(clients, "firestore_client", lambda: fake_firestore)
    monkeypatch.setattr(clients, "bigquery_client", lambda: fake_bq)
    monkeypatch.setattr(clients, "vertex_initialised", lambda: True)

    # store/purchases/history import the getters by name, so patch them there too.
    from nano import events, fx, history, purchases, question_log, receipts, store, stores

    monkeypatch.setattr(store, "firestore_client", lambda: fake_firestore)
    monkeypatch.setattr(purchases, "bigquery_client", lambda: fake_bq)
    monkeypatch.setattr(history, "bigquery_client", lambda: fake_bq)
    monkeypatch.setattr(events, "bigquery_client", lambda: fake_bq)
    monkeypatch.setattr(stores, "bigquery_client", lambda: fake_bq)
    monkeypatch.setattr(question_log, "bigquery_client", lambda: fake_bq)
    monkeypatch.setattr(fx, "firestore_client", lambda: fake_firestore)
    # No unit test may reach the ECB; rates come from the fake cache only.
    monkeypatch.setattr(fx, "fetch_ecb_rates", dict)
    monkeypatch.setattr(receipts, "storage_client", lambda: fake_storage)

    monkeypatch.setattr(gemini, "classifier_model", lambda: fake_gemini)
    monkeypatch.setattr(gemini, "extraction_model", lambda: fake_gemini)

    monkeypatch.setattr(
        wa_module, "send_text", lambda to, text, s=None: fake_whatsapp.send_text(to, text)
    )
    monkeypatch.setattr(
        wa_module, "mark_as_read", lambda mid, s=None: fake_whatsapp.mark_as_read(mid)
    )
    monkeypatch.setattr(
        wa_module, "download_media", lambda mid, s=None: fake_whatsapp.download_media(mid)
    )
    monkeypatch.setattr(
        wa_module,
        "notify",
        lambda numbers, text, s=None: {n: fake_whatsapp.send_text(n, text) for n in numbers},
    )

    # Fallback classification, used only when a test queues nothing of its own,
    # so tests about transport need not script an intent while tests about
    # intent still win.
    fake_gemini.set_default_function_call("add_items", items=["coffee"])
    fake_whatsapp.stage_media("MEDIA_1", b"\xff\xd8fake-jpeg-bytes", "image/jpeg")

    from nano.app import create_app

    app = create_app()
    yield app

    config.reset_settings_cache()
    clients.reset_clients()
    gemini.reset_models()

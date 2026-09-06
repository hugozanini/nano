"""Configuration for the integration tier.

These tests use real credentials against real services, so unlike the unit tier
they read their configuration from the *environment* rather than from synthetic
constants. That is deliberate: the real project ID, bucket and dataset must not
live in the repository, so they arrive from the developer's shell locally and
from GitHub secrets in CI.

Every test here is required to write only to the test dataset and to
`nano_test_`-prefixed Firestore collections. The parent `conftest.py` enforces
that before any client call is made.
"""

from __future__ import annotations

import os
import uuid

import pytest

REQUIRED_ENV = ["GOOGLE_CLOUD_PROJECT", "BQ_DATASET", "FIRESTORE_PREFIX"]


def pytest_collection_modifyitems(config, items):
    """Mark everything under tests/integration/ as integration, so the guardrails apply."""
    for item in items:
        if "tests/integration/" in str(item.fspath).replace("\\", "/"):
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="session", autouse=True)
def require_credentials() -> None:
    """Skip the whole tier with a useful message rather than failing obscurely."""
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        pytest.skip(
            f"integration tier needs {', '.join(missing)} in the environment; "
            "run scripts/setup_test_dataset.sh and export them",
            allow_module_level=True,
        )

    try:
        import google.auth

        google.auth.default()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"no usable ADC credentials: {exc}", allow_module_level=True)


@pytest.fixture(scope="session")
def project_id() -> str:
    return os.environ["GOOGLE_CLOUD_PROJECT"]


@pytest.fixture(scope="session")
def dataset() -> str:
    return os.environ["BQ_DATASET"]


@pytest.fixture(scope="session")
def firestore_prefix() -> str:
    return os.environ["FIRESTORE_PREFIX"]


@pytest.fixture(scope="session")
def bq():
    from google.cloud import bigquery

    return bigquery.Client()


@pytest.fixture(scope="session")
def firestore_client():
    from google.cloud import firestore

    return firestore.Client()


@pytest.fixture
def run_id() -> str:
    """Unique per test, so parallel or repeated runs never collide."""
    return f"itest-{uuid.uuid4()}"

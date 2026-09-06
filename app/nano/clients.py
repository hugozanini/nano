"""Lazily-constructed Google Cloud clients.

The original module built Firestore, BigQuery and Vertex clients at import time.
That has two costs: a credential or network problem kills the container during
startup with a bare traceback instead of a served health check, and no test can
import the application without reaching out to Google.

Every client here is created on first use and memoised. Tests override these
getters rather than patching library internals.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from nano.config import get_settings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def firestore_client() -> Any:
    from google.cloud import firestore

    logger.info("initialising Firestore client")
    return firestore.Client()


@lru_cache(maxsize=1)
def bigquery_client() -> Any:
    from google.cloud import bigquery

    logger.info("initialising BigQuery client")
    return bigquery.Client()


@lru_cache(maxsize=1)
def storage_client() -> Any:
    from google.cloud import storage

    logger.info("initialising Cloud Storage client")
    return storage.Client()


@lru_cache(maxsize=1)
def tasks_client() -> Any:
    from google.cloud import tasks_v2

    logger.info("initialising Cloud Tasks client")
    return tasks_v2.CloudTasksClient()


@lru_cache(maxsize=1)
def vertex_initialised() -> bool:
    import vertexai

    settings = get_settings()
    vertexai.init(project=settings.project_id, location=settings.location)
    logger.info("initialised Vertex AI")
    return True


def reset_clients() -> None:
    """Test hook: drop every memoised client.

    Tolerates a getter having been monkeypatched, in which case there is no
    cache to clear and the patch itself is what gets undone.
    """
    for fn in (
        firestore_client,
        bigquery_client,
        storage_client,
        tasks_client,
        vertex_initialised,
    ):
        clear = getattr(fn, "cache_clear", None)
        if clear is not None:
            clear()

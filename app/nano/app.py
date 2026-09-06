"""Application factory."""

from __future__ import annotations

import logging

from fastapi import FastAPI

from nano.config import ConfigError, get_settings
from nano.logging_setup import configure_logging
from nano.routes import router

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    """Build the app, failing fast and legibly on bad configuration."""
    try:
        settings = get_settings()
    except ConfigError:
        # Configure logging before re-raising so the reason is readable in
        # Cloud Run's logs rather than a bare traceback on stderr.
        configure_logging()
        logger.exception("startup aborted: invalid configuration")
        raise

    configure_logging(project_id=settings.project_id)
    logger.info(
        "nano starting: backend=%s dataset=%s signature=%s household=%d",
        settings.task_backend,
        settings.bq_dataset,
        "enforced" if settings.require_signature else "DISABLED",
        len(settings.household),
    )

    app = FastAPI(title="Nano", docs_url=None, redoc_url=None)
    app.include_router(router)
    return app

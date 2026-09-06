"""Configuration, parsed once from the environment and validated at startup.

Deliberately dependency-free (no pydantic-settings): the parsing is simple, and
keeping it explicit means the household-number handling below can be read and
tested directly, which matters because that is where a live production bug was
hiding.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from functools import lru_cache

logger = logging.getLogger(__name__)

# Characters a deploy pipeline can wrap around a value. `deploy-cloudrun` splits
# its `env_vars` input on commas, so a comma-separated secret arrives truncated
# and quoted: HOUSEHOLD_NUMBERS='"15550100001'. Stripping these makes the app
# resilient to that class of mangling instead of silently messaging a bad number.
_WRAPPERS = "\"' \t\r\n"

# Accepted between household entries. Semicolon is preferred and is what the
# deploy pipeline uses, precisely because a comma is the one character the CI
# action treats as a delimiter. Comma stays supported so an existing secret,
# or a hand-set local value, keeps working.
_ENTRY_SEPARATORS = ",;|"


class ConfigError(RuntimeError):
    """Raised at startup when configuration is missing or unusable."""


def _clean(value: str) -> str:
    return value.strip().strip(_WRAPPERS).strip()


def _digits_only(value: str) -> str:
    return "".join(ch for ch in value if ch.isdigit())


def _split_entries(raw: str) -> list[str]:
    """Split on any accepted separator, so the value survives either convention."""
    normalised = _clean(raw)
    for separator in _ENTRY_SEPARATORS[1:]:
        normalised = normalised.replace(separator, ",")
    return normalised.split(",")


def parse_household(members_raw: str, numbers_raw: str) -> dict[str, str]:
    """Build {phone_number: display_name} from either supported env var.

    Preferred form (HOUSEHOLD_MEMBERS):  "<number>:alex;<number>:sam"
    Legacy form    (HOUSEHOLD_NUMBERS):  "<number>;<number>"

    Entries may be separated by ";", "|" or ",". Semicolon is what the deploy
    pipeline uses, because a comma is the character the CI action splits on.

    Every value is stripped of stray quotes and reduced to digits, because a
    number that arrives as '"15550100001' must not be treated as a distinct
    recipient from '15550100001'. Entries that contain no digits are dropped.
    """
    household: dict[str, str] = {}

    for entry in _split_entries(members_raw):
        entry = _clean(entry)
        if not entry:
            continue
        number, _, name = entry.partition(":")
        number = _digits_only(number)
        if not number:
            continue
        household[number] = _clean(name) or f"member_{len(household) + 1}"

    if not household:
        for entry in _split_entries(numbers_raw):
            number = _digits_only(entry)
            if number:
                household[number] = f"member_{len(household) + 1}"

    return household


@dataclass(frozen=True)
class Settings:
    # WhatsApp / Meta
    verify_token: str
    whatsapp_token: str
    whatsapp_app_secret: str
    phone_number_id: str
    graph_api_version: str

    # Household
    household: dict[str, str] = field(default_factory=dict)
    household_timezone: str = "Europe/Madrid"
    default_currency: str = "EUR"

    # Google Cloud
    project_id: str = ""
    location: str = "us-central1"
    bq_dataset: str = "nano"
    firestore_prefix: str = ""
    receipts_bucket: str = ""
    gemini_model: str = "gemini-2.5-flash"

    # Async processing
    task_backend: str = "inline"  # cloud_tasks | inline
    tasks_queue: str = "nano-webhook-work"
    tasks_location: str = "us-central1"
    tasks_target_url: str = ""
    tasks_invoker_sa: str = ""

    require_signature: bool = True

    @property
    def household_numbers(self) -> list[str]:
        return list(self.household)

    def name_for(self, number: str) -> str:
        return self.household.get(_digits_only(number), "unknown")

    def is_household(self, number: str) -> bool:
        return _digits_only(number) in self.household

    def table(self, name: str) -> str:
        """Fully-qualified table reference, so tests can point at nano_test."""
        return f"{self.bq_dataset}.{name}"

    def collection(self, name: str) -> str:
        """Firestore collection name, namespaced in tests via FIRESTORE_PREFIX."""
        return f"{self.firestore_prefix}{name}"

    @property
    def uses_cloud_tasks(self) -> bool:
        return self.task_backend == "cloud_tasks"


def _env(key: str, default: str = "") -> str:
    return _clean(os.environ.get(key, default))


def _env_bool(key: str, default: bool) -> bool:
    raw = _env(key).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def load_settings() -> Settings:
    """Read and validate configuration. Raises ConfigError on anything unusable."""
    app_secret = _env("WHATSAPP_APP_SECRET")
    household = parse_household(_env("HOUSEHOLD_MEMBERS"), _env("HOUSEHOLD_NUMBERS"))

    settings = Settings(
        verify_token=_env("VERIFY_TOKEN"),
        whatsapp_token=_env("WHATSAPP_ACCESS_TOKEN"),
        whatsapp_app_secret=app_secret,
        phone_number_id=_env("PHONE_NUMBER_ID"),
        graph_api_version=_env("GRAPH_API_VERSION", "v20.0"),
        household=household,
        household_timezone=_env("HOUSEHOLD_TIMEZONE", "Europe/Madrid"),
        default_currency=_env("DEFAULT_CURRENCY", "EUR"),
        project_id=_env("GOOGLE_CLOUD_PROJECT"),
        location=_env("VERTEX_LOCATION", "us-central1"),
        bq_dataset=_env("BQ_DATASET", "nano"),
        firestore_prefix=_env("FIRESTORE_PREFIX"),
        receipts_bucket=_env("RECEIPTS_BUCKET"),
        gemini_model=_env("GEMINI_MODEL", "gemini-2.5-flash"),
        task_backend=_env("TASK_BACKEND", "inline"),
        tasks_queue=_env("TASKS_QUEUE", "nano-webhook-work"),
        tasks_location=_env("TASKS_LOCATION", _env("VERTEX_LOCATION", "us-central1")),
        tasks_target_url=_env("TASKS_TARGET_URL"),
        tasks_invoker_sa=_env("TASKS_INVOKER_SA"),
        # Signature checking is only enforceable once the app secret exists.
        # Defaults to on when the secret is present, off (loudly) when it is not,
        # so introducing verification cannot black-hole live traffic.
        require_signature=_env_bool("REQUIRE_SIGNATURE", bool(app_secret)),
    )

    problems: list[str] = []
    if not settings.verify_token:
        problems.append("VERIFY_TOKEN is required (Meta's webhook handshake)")
    if not settings.phone_number_id:
        problems.append("PHONE_NUMBER_ID is required")
    if not settings.household:
        problems.append("no household members: set HOUSEHOLD_MEMBERS or HOUSEHOLD_NUMBERS")
    if settings.require_signature and not settings.whatsapp_app_secret:
        problems.append("REQUIRE_SIGNATURE is on but WHATSAPP_APP_SECRET is not set")
    if settings.task_backend not in {"inline", "cloud_tasks"}:
        problems.append(
            f"TASK_BACKEND must be 'inline' or 'cloud_tasks', got {settings.task_backend!r}"
        )
    if settings.uses_cloud_tasks:
        if not settings.tasks_target_url:
            problems.append("TASKS_TARGET_URL is required when TASK_BACKEND=cloud_tasks")
        if not settings.tasks_invoker_sa:
            problems.append("TASKS_INVOKER_SA is required when TASK_BACKEND=cloud_tasks")
        if not settings.project_id:
            problems.append("GOOGLE_CLOUD_PROJECT is required when TASK_BACKEND=cloud_tasks")

    if problems:
        raise ConfigError("invalid configuration:\n  - " + "\n  - ".join(problems))

    if not settings.require_signature:
        logger.critical(
            "webhook signature verification is DISABLED; the endpoint is "
            "unauthenticated and will accept forged messages. Set "
            "WHATSAPP_APP_SECRET to enable it."
        )

    return settings


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()


def reset_settings_cache() -> None:
    """Test hook: forget the memoised settings so new env vars take effect."""
    get_settings.cache_clear()

"""Converting purchases to one comparable currency.

A household shopping in Spain and Brazil cannot sum EUR and BRL, so "how much
did we spend this year" is unanswerable until every amount lands in one unit.

Rates come from the European Central Bank's daily reference feed: free, no key,
EUR-based, and it publishes BRL. Two decisions matter:

**The rate that applied on the purchase date, not today's.** A 2024 shop keeps
its 2024 rate, so a total computed last year still reconciles this year. The
rate and its date are stored beside the converted amount, which makes the number
auditable rather than merely present.

**Failure never blocks a receipt.** The ECB publishes on business days only, and
the feed can simply be unreachable. The chain is: cached rate for that date, then
the most recent cached rate before it, then the live feed, then nothing — and
"nothing" means a NULL converted amount and a logged warning, never a lost
purchase. Same rule as receipt archival: a derived value is not worth the thing
it was derived from.
"""

from __future__ import annotations

import datetime
import logging
import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation

import requests

from nano.clients import firestore_client
from nano.config import Settings, get_settings
from nano.logging_setup import log_with

logger = logging.getLogger(__name__)

BASE_CURRENCY = "EUR"

# 90 days of daily rates: enough to cover a receipt photographed weeks late,
# and small enough to parse quickly.
ECB_FEED = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml"
ECB_NAMESPACE = {"ecb": "http://www.ecb.int/vocabulary/2002-08-01/eurofxref"}

FEED_TIMEOUT = 10

# How far back to accept a stale rate. The ECB skips weekends and holidays, so a
# few days is routine; beyond a fortnight the number stops being meaningful.
MAX_RATE_AGE = datetime.timedelta(days=14)


def _rates_collection(settings: Settings):
    return firestore_client().collection(settings.collection("fx_rates"))


def _to_decimal(value) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None


def fetch_ecb_rates() -> dict[datetime.date, dict[str, Decimal]]:
    """Pull the ECB's 90-day reference feed. Returns {} on any failure."""
    try:
        response = requests.get(ECB_FEED, timeout=FEED_TIMEOUT)
        response.raise_for_status()
        root = ET.fromstring(response.content)
    except (requests.RequestException, ET.ParseError) as exc:
        log_with(logger, logging.WARNING, "could not fetch ECB rates", error=str(exc))
        return {}

    rates: dict[datetime.date, dict[str, Decimal]] = {}
    for day in root.iter():
        if not day.get("time"):
            continue
        try:
            on = datetime.date.fromisoformat(day.get("time", ""))
        except ValueError:
            continue
        daily: dict[str, Decimal] = {}
        for entry in day:
            currency = entry.get("currency")
            rate = _to_decimal(entry.get("rate"))
            if currency and rate is not None:
                daily[currency.upper()] = rate
        if daily:
            rates[on] = daily

    return rates


def _cache_key(on: datetime.date) -> str:
    return on.isoformat()


def _read_cached(on: datetime.date, settings: Settings) -> dict[str, Decimal]:
    try:
        snapshot = _rates_collection(settings).document(_cache_key(on)).get()
    except Exception as exc:
        log_with(logger, logging.WARNING, "fx cache read failed", error=str(exc))
        return {}
    if not snapshot.exists:
        return {}
    stored = (snapshot.to_dict() or {}).get("rates") or {}
    return {k: d for k, v in stored.items() if (d := _to_decimal(v)) is not None}


def _write_cached(rates: dict[datetime.date, dict[str, Decimal]], settings: Settings) -> None:
    collection = _rates_collection(settings)
    for on, daily in rates.items():
        try:
            collection.document(_cache_key(on)).set(
                {"rates": {k: str(v) for k, v in daily.items()}, "source": "ecb"}
            )
        except Exception as exc:  # pragma: no cover - defensive
            log_with(logger, logging.WARNING, "fx cache write failed", error=str(exc))
            return


def rate_for(
    currency: str, on: datetime.date, settings: Settings | None = None
) -> tuple[Decimal | None, datetime.date | None]:
    """The EUR rate for `currency` on or before `on`.

    Returns ``(rate, rate_date)``, or ``(None, None)`` when no usable rate can
    be found. A rate of 1 is returned for EUR itself without any lookup.
    """
    settings = settings or get_settings()
    currency = str(currency or "").strip().upper()

    if not currency or currency == BASE_CURRENCY:
        return Decimal("1"), on

    # Walk back from the purchase date: the ECB skips weekends and holidays.
    for offset in range(MAX_RATE_AGE.days + 1):
        day = on - datetime.timedelta(days=offset)
        cached = _read_cached(day, settings)
        if currency in cached:
            return cached[currency], day

    # Nothing cached nearby — refresh from the feed and try once more.
    fetched = fetch_ecb_rates()
    if not fetched:
        return None, None
    _write_cached(fetched, settings)

    for offset in range(MAX_RATE_AGE.days + 1):
        day = on - datetime.timedelta(days=offset)
        if currency in fetched.get(day, {}):
            return fetched[day][currency], day

    log_with(
        logger,
        logging.WARNING,
        "no usable exchange rate found",
        currency=currency,
        wanted=on.isoformat(),
    )
    return None, None


def to_base(
    amount,
    currency: str,
    on: datetime.date,
    settings: Settings | None = None,
) -> tuple[Decimal | None, Decimal | None, datetime.date | None]:
    """Convert to EUR. Returns ``(amount_eur, rate, rate_date)``.

    Any part may be None: a missing rate yields a NULL converted amount rather
    than a wrong one, and never raises.
    """
    settings = settings or get_settings()

    value = _to_decimal(amount)
    if value is None:
        return None, None, None

    try:
        rate, rate_date = rate_for(currency, on, settings)
    except Exception as exc:  # pragma: no cover - defensive
        log_with(logger, logging.ERROR, "currency conversion failed", error=str(exc))
        return None, None, None

    if rate is None or rate == 0:
        return None, None, None

    # ECB quotes units of foreign currency per euro, so divide.
    converted = (value / rate).quantize(Decimal("0.01"))
    return converted, rate, rate_date

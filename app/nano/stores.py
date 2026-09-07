"""Resolving a shop once, and reusing the answer.

Inferring a store's location on every receipt produces inconsistent answers for
the same shop — "Catalonia" from one photo, nothing from the next when the
address was cut off or the tax table was cropped. Inconsistency in a dimension
is worse than missing data, because it splits one shop into several rows in
every `GROUP BY`.

So a store is resolved once and cached. The identity is deterministic, derived
from the normalised brand and tax id, which means:

- the same shop always maps to the same `store_id`, however its name was
  printed or how legible the photo was;
- the enrichment model is not called again for a shop already seen;
- a location that turns out wrong is corrected in one row, not in every receipt
  that references it.

Later sightings *upgrade* a record rather than overwrite it: a receipt that
finally shows an address fills in a city that earlier ones lacked, but a
receipt with a cropped tax table never erases a country already established.
"""

from __future__ import annotations

import datetime
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any

from nano.clients import bigquery_client
from nano.config import Settings, get_settings
from nano.locale import LocationInference
from nano.logging_setup import log_with

logger = logging.getLogger(__name__)

# Stable namespace: the same shop always yields the same id.
STORE_NAMESPACE = uuid.UUID("8c2f4b1e-5d3a-4c7b-9e6f-1a2b3c4d5e6f")

# Legal-form suffixes that vary between receipts for the same chain.
_LEGAL_SUFFIXES = re.compile(
    r"[\s,.]*\b(s\.?a\.?u?|s\.?l\.?u?|lda|ltda|ltd|gmbh|bv|nv|inc|plc|spa|srl|"
    r"me|epp|eireli)\b\.?\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Store:
    store_id: str
    store_name_original: str
    store_brand: str
    tax_id: str = ""
    country_code: str = ""
    region: str = ""
    city_printed: str = ""
    city_inferred: str = ""
    address: str = ""
    location_confidence: str = ""
    location_evidence: str = ""


def normalise_brand(store_name: str) -> str:
    """`MERCADONA, S.A.` -> `Mercadona`.

    Deterministic on purpose: brand normalisation decides identity, so it must
    not vary between runs or depend on a model being reachable.
    """
    name = str(store_name or "").strip()
    if not name:
        return ""

    # Strip legal suffixes repeatedly — "EXEMPLE, S.L. LDA" does occur.
    previous = None
    while previous != name:
        previous = name
        name = _LEGAL_SUFFIXES.sub("", name).strip(" ,.-")

    name = re.sub(r"\s+", " ", name).strip()
    if not name:
        return ""

    # Title-case only when the original was all caps, which receipts usually
    # are; a shop that prints mixed case already knows its own styling.
    return name.title() if name.isupper() else name


def store_id_for(brand: str, tax_id: str, country_code: str = "") -> str:
    """Deterministic identity, preferring the tax id when one was printed."""
    key = (brand or "").strip().lower()
    tax = re.sub(r"[^0-9A-Za-z]", "", str(tax_id or "")).lower()
    seed = f"{key}|{tax}" if tax else f"{key}|{(country_code or '').lower()}"
    return str(uuid.uuid5(STORE_NAMESPACE, seed))


def _columns() -> list[str]:
    return [
        "store_id",
        "store_name_original",
        "store_brand",
        "tax_id",
        "country_code",
        "region",
        "city_printed",
        "city_inferred",
        "address",
        "location_confidence",
        "location_evidence",
    ]


def lookup(store_id: str, settings: Settings | None = None) -> Store | None:
    """Read a store from the dimension, or None if it has not been seen."""
    from google.cloud import bigquery

    settings = settings or get_settings()
    try:
        rows = list(
            bigquery_client()
            .query(
                f"SELECT {', '.join(_columns())} FROM `{settings.table('stores')}` "
                "WHERE store_id = @sid LIMIT 1",
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[bigquery.ScalarQueryParameter("sid", "STRING", store_id)]
                ),
            )
            .result()
        )
    except Exception as exc:
        log_with(logger, logging.WARNING, "store lookup failed", error=str(exc))
        return None

    if not rows:
        return None
    row = rows[0]
    return Store(**{name: (getattr(row, name) or "") for name in _columns()})


def _merge(store: Store, settings: Settings) -> None:
    """Insert or update the dimension row.

    A later sighting fills gaps but never erases what an earlier, better
    receipt established — a cropped tax table must not blank a known country.
    """
    from google.cloud import bigquery

    now = datetime.datetime.now(datetime.UTC)
    params = [
        bigquery.ScalarQueryParameter(name, "STRING", getattr(store, name)) for name in _columns()
    ]
    params.append(bigquery.ScalarQueryParameter("now", "TIMESTAMP", now))

    bigquery_client().query(
        f"""
        MERGE `{settings.table("stores")}` T
        USING (SELECT @store_id AS store_id) S
        ON T.store_id = S.store_id
        WHEN MATCHED THEN UPDATE SET
          last_seen_at        = @now,
          receipt_count       = IFNULL(T.receipt_count, 0) + 1,
          store_name_original = IFNULL(NULLIF(T.store_name_original, ''), @store_name_original),
          store_brand         = IFNULL(NULLIF(T.store_brand, ''), @store_brand),
          tax_id              = IFNULL(NULLIF(T.tax_id, ''), @tax_id),
          country_code        = IFNULL(NULLIF(T.country_code, ''), @country_code),
          region              = IFNULL(NULLIF(T.region, ''), @region),
          city_printed        = IFNULL(NULLIF(T.city_printed, ''), @city_printed),
          city_inferred       = IFNULL(NULLIF(T.city_inferred, ''), @city_inferred),
          address             = IFNULL(NULLIF(T.address, ''), @address),
          location_confidence = IFNULL(NULLIF(T.location_confidence, ''), @location_confidence),
          location_evidence   = IFNULL(NULLIF(T.location_evidence, ''), @location_evidence)
        WHEN NOT MATCHED THEN INSERT (
          store_id, store_name_original, store_brand, tax_id, country_code, region,
          city_printed, city_inferred, address, location_confidence, location_evidence,
          first_seen_at, last_seen_at, receipt_count
        ) VALUES (
          @store_id, @store_name_original, @store_brand, @tax_id, @country_code, @region,
          @city_printed, @city_inferred, @address, @location_confidence, @location_evidence,
          @now, @now, 1
        )
        """,
        job_config=bigquery.QueryJobConfig(query_parameters=params),
    ).result()


def resolve(
    store_name: str,
    location: LocationInference,
    address: str = "",
    tax_id: str = "",
    settings: Settings | None = None,
) -> str:
    """Return the `store_id` for this shop, creating the record if it is new.

    Never raises: a dimension failure must not cost us the purchase, so it
    degrades to an empty id and the purchase is still written.
    """
    settings = settings or get_settings()

    brand = normalise_brand(store_name)
    if not brand:
        return ""

    store_id = store_id_for(brand, tax_id, location.country_code)

    existing = lookup(store_id, settings)
    if existing is not None:
        log_with(
            logger,
            logging.INFO,
            "store already known; reusing its location",
            store_id=store_id,
            brand=existing.store_brand,
        )

    store = Store(
        store_id=store_id,
        store_name_original=str(store_name or ""),
        store_brand=brand,
        tax_id=str(tax_id or ""),
        country_code=location.country_code,
        region=location.region,
        city_printed=location.city_printed,
        city_inferred=location.city_inferred,
        address=str(address or ""),
        location_confidence=location.confidence,
        location_evidence=location.evidence_text,
    )

    try:
        _merge(store, settings)
    except Exception as exc:
        log_with(
            logger,
            logging.ERROR,
            "could not upsert the store dimension",
            store_id=store_id,
            error=str(exc),
        )
        return store_id

    log_with(
        logger,
        logging.INFO,
        "resolved store",
        store_id=store_id,
        brand=brand,
        country=location.country_code,
        new=existing is None,
    )
    return store_id


def location_for(store_id: str, settings: Settings | None = None) -> dict[str, Any]:
    """The cached location for a store, for reuse when a receipt lacks signals."""
    store = lookup(store_id, settings) if store_id else None
    if store is None:
        return {}
    return {
        "country_code": store.country_code,
        "region": store.region,
        "city_printed": store.city_printed,
        "city_inferred": store.city_inferred,
    }

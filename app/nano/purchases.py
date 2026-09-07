"""Writing receipts into the Iceberg lakehouse.

Two changes from the original, both aimed at the duplicate-purchase incident.

**Deterministic purchase ids.** The id is derived from the WhatsApp message id
rather than a fresh uuid4, so re-processing the same message can only ever
address the same row. Combined with the existence check below, a retry that
somehow slips past both dedupe layers still cannot create a second purchase.

**One statement instead of N+1.** Line items are written with a single
``INSERT ... SELECT FROM UNNEST(@rows)``. The original issued one DML statement
per item — thirteen sequential BigQuery jobs for a twelve-item receipt, roughly
forty seconds — which is what pushed the handler past Meta's webhook timeout in
the first place. Verified against a real Iceberg table in the integration tier.
"""

from __future__ import annotations

import datetime
import logging
import uuid
from typing import Any

from nano.clients import bigquery_client
from nano.config import Settings, get_settings
from nano.logging_setup import log_with

logger = logging.getLogger(__name__)

# Stable namespace so a given WhatsApp message always maps to the same purchase.
PURCHASE_NAMESPACE = uuid.UUID("6f1d6b6a-0f2e-4a1b-9d2a-3c4e5f60718a")


def purchase_id_for(message_id: str) -> str:
    """Deterministic id: the same message can never become two purchases."""
    return str(uuid.uuid5(PURCHASE_NAMESPACE, message_id))


def find_matching_purchase(
    store_name: str,
    purchase_date: str,
    total_amount: Any,
    settings: Settings | None = None,
) -> str | None:
    """Find an existing purchase for the same shop, day and total.

    Message-id idempotency stops a *redelivery* creating a duplicate, but it
    cannot stop a person photographing the same receipt twice — different
    message, same trip, and the recorded spend silently doubles. Which is
    exactly what happened: one EUR 43.06 shop became EUR 86.12.
    """
    from google.cloud import bigquery

    settings = settings or get_settings()
    rows = list(
        bigquery_client()
        .query(
            f"""
            SELECT purchase_id FROM `{settings.table("purchases")}`
            WHERE store_name = @store
              AND DATE(purchased_at) = @purchased_on
              AND total_amount = @total
            LIMIT 1
            """,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("store", "STRING", store_name),
                    bigquery.ScalarQueryParameter("purchased_on", "DATE", purchase_date),
                    bigquery.ScalarQueryParameter("total", "NUMERIC", str(total_amount)),
                ]
            ),
        )
        .result()
    )
    return rows[0].purchase_id if rows else None


def purchase_exists(purchase_id: str, settings: Settings | None = None) -> bool:
    from google.cloud import bigquery

    settings = settings or get_settings()
    rows = list(
        bigquery_client()
        .query(
            f"SELECT 1 FROM `{settings.table('purchases')}` WHERE purchase_id = @pid LIMIT 1",
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("pid", "STRING", purchase_id)]
            ),
        )
        .result()
    )
    return bool(rows)


def _item_rows_parameter(purchase_id: str, items: list[dict[str, Any]]):
    """Build the ARRAY<STRUCT> parameter for the batched insert."""
    from google.cloud import bigquery

    struct_type = bigquery.StructQueryParameterType(
        bigquery.ScalarQueryParameterType("STRING", name="purchase_id"),
        bigquery.ScalarQueryParameterType("STRING", name="item_name"),
        bigquery.ScalarQueryParameterType("STRING", name="normalized_name"),
        bigquery.ScalarQueryParameterType("STRING", name="category"),
        bigquery.ScalarQueryParameterType("NUMERIC", name="quantity"),
        bigquery.ScalarQueryParameterType("NUMERIC", name="unit_price"),
        bigquery.ScalarQueryParameterType("NUMERIC", name="line_total"),
    )

    values = []
    for item in items:
        name = str(item.get("item_name", "")).strip()
        # `normalized_name` now holds the canonical English term, so a question
        # asked in English can match a line printed in Catalan.
        values.append(
            bigquery.StructQueryParameter(
                None,
                bigquery.ScalarQueryParameter("purchase_id", "STRING", purchase_id),
                bigquery.ScalarQueryParameter("item_name", "STRING", name),
                bigquery.ScalarQueryParameter(
                    "normalized_name", "STRING", str(item.get("normalized_name") or name.lower())
                ),
                bigquery.ScalarQueryParameter(
                    "category", "STRING", str(item.get("category") or "other")
                ),
                bigquery.ScalarQueryParameter(
                    "quantity", "NUMERIC", str(item.get("quantity", 1) or 1)
                ),
                bigquery.ScalarQueryParameter(
                    "unit_price", "NUMERIC", str(item.get("unit_price", 0) or 0)
                ),
                bigquery.ScalarQueryParameter(
                    "line_total", "NUMERIC", str(item.get("line_total", 0) or 0)
                ),
            )
        )

    return bigquery.ArrayQueryParameter("rows", struct_type, values)


def log_purchase(
    message_id: str,
    store_name: str,
    purchase_date: str,
    currency: str,
    total_amount: Any,
    items: list[dict[str, Any]],
    receipt_uri: str = "",
    settings: Settings | None = None,
    location: Any = None,
    store_id: str = "",
    store_address: str = "",
    tax_id: str = "",
    tax_rates: list | None = None,
) -> tuple[str, bool]:
    """Write one purchase and its line items.

    Returns ``(purchase_id, written)``. ``written`` is False when the purchase
    was already present, which makes this safe to call twice.
    """
    from google.cloud import bigquery

    settings = settings or get_settings()
    client = bigquery_client()
    purchase_id = purchase_id_for(message_id)

    if purchase_exists(purchase_id, settings):
        log_with(
            logger,
            logging.INFO,
            "purchase already recorded; skipping duplicate write",
            purchase_id=purchase_id,
            message_id=message_id,
        )
        return purchase_id, False

    now = datetime.datetime.now(datetime.UTC)
    purchased_at = f"{purchase_date}T00:00:00Z"

    # Provenance travels with the row: an analyst has to be able to tell a
    # tax-table fingerprint from a guess made off the language alone.
    client.query(
        f"""
        INSERT INTO `{settings.table("purchases")}`
        (purchase_id, purchased_at, store_name, total_amount,
         currency, receipt_image_uri, created_at,
         receipt_language, country_code, region, city_printed, city_inferred,
         location_confidence, location_evidence, store_address, tax_id, tax_rates, store_id)
        SELECT @purchase_id, @purchased_at, @store_name, @total_amount,
               @currency, @receipt_image_uri, @created_at,
               @receipt_language, @country_code, @region, @city_printed, @city_inferred,
               @location_confidence, @location_evidence, @store_address, @tax_id, @tax_rates,
               @store_id
        """,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("purchase_id", "STRING", purchase_id),
                bigquery.ScalarQueryParameter("purchased_at", "TIMESTAMP", purchased_at),
                bigquery.ScalarQueryParameter("store_name", "STRING", store_name),
                bigquery.ScalarQueryParameter("total_amount", "NUMERIC", str(total_amount)),
                bigquery.ScalarQueryParameter("currency", "STRING", currency),
                bigquery.ScalarQueryParameter("receipt_image_uri", "STRING", receipt_uri),
                bigquery.ScalarQueryParameter("created_at", "TIMESTAMP", now),
                bigquery.ScalarQueryParameter(
                    "receipt_language", "STRING", getattr(location, "language", "")
                ),
                bigquery.ScalarQueryParameter(
                    "country_code", "STRING", getattr(location, "country_code", "")
                ),
                bigquery.ScalarQueryParameter("region", "STRING", getattr(location, "region", "")),
                bigquery.ScalarQueryParameter(
                    "city_printed", "STRING", getattr(location, "city_printed", "")
                ),
                bigquery.ScalarQueryParameter(
                    "city_inferred", "STRING", getattr(location, "city_inferred", "")
                ),
                bigquery.ScalarQueryParameter(
                    "location_confidence", "STRING", getattr(location, "confidence", "")
                ),
                bigquery.ScalarQueryParameter(
                    "location_evidence", "STRING", getattr(location, "evidence_text", "")
                ),
                bigquery.ScalarQueryParameter("store_id", "STRING", store_id),
                bigquery.ScalarQueryParameter("store_address", "STRING", store_address),
                bigquery.ScalarQueryParameter("tax_id", "STRING", tax_id),
                bigquery.ArrayQueryParameter(
                    "tax_rates", "NUMERIC", [str(r) for r in (tax_rates or [])]
                ),
            ]
        ),
    ).result()

    if items:
        client.query(
            f"""
            INSERT INTO `{settings.table("purchase_items")}`
            (purchase_id, item_name, normalized_name, category,
             quantity, unit_price, line_total)
            SELECT purchase_id, item_name, normalized_name, category,
                   quantity, unit_price, line_total
            FROM UNNEST(@rows)
            """,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[_item_rows_parameter(purchase_id, items)]
            ),
        ).result()

    log_with(
        logger,
        logging.INFO,
        "purchase recorded",
        purchase_id=purchase_id,
        items=len(items),
        currency=currency,
        statements=2,
    )
    return purchase_id, True

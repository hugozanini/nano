#!/usr/bin/env python3
"""Re-enrich purchases logged before the enrichment existed.

Every receipt image is archived to Cloud Storage at ingest, and this is what
that buys: a prompt improvement can be applied retroactively to every receipt
ever logged, rather than only to the next one. The original photo is the source
of truth; the rows are derived and can be rebuilt.

For each purchase missing the newer fields it re-reads the archived image,
re-runs extraction, locale inference, store resolution, canonicalisation and
currency conversion, then updates the existing rows in place — keeping the same
`purchase_id`, so nothing downstream has to change.

Dry run by default, because it rewrites production data.

    python scripts/backfill_receipt_enrichment.py                  # show the plan
    python scripts/backfill_receipt_enrichment.py --apply          # do it
    python scripts/backfill_receipt_enrichment.py --apply --limit 1

Requires the same environment the service uses: GOOGLE_CLOUD_PROJECT,
BQ_DATASET, RECEIPTS_BUCKET and ADC credentials.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from google.cloud import bigquery
from nano import canonicalise, fx, locale, receipts, stores
from nano.clients import bigquery_client, storage_client
from nano.config import get_settings
from nano.gemini import extract_receipt
from nano.logging_setup import configure_logging

logger = logging.getLogger("backfill")


def candidates(settings, limit: int) -> list[dict]:
    """Purchases with an archived image but no enrichment yet."""
    rows = (
        bigquery_client()
        .query(
            f"""
        SELECT purchase_id, store_name, purchased_at, currency,
               total_amount, receipt_image_uri
        FROM `{settings.table("purchases")}`
        WHERE receipt_image_uri IS NOT NULL
          AND receipt_image_uri != ''
          AND (country_code IS NULL OR country_code = '')
        ORDER BY purchased_at DESC
        LIMIT {int(limit)}
        """
        )
        .result()
    )
    return [dict(row.items()) for row in rows]


def read_image(uri: str) -> tuple[bytes, str]:
    bucket_name, _, blob_path = uri.removeprefix("gs://").partition("/")
    blob = storage_client().bucket(bucket_name).blob(blob_path)
    return blob.download_as_bytes(), (blob.content_type or "image/jpeg")


def enrich(row: dict, settings) -> dict:
    """Recompute every derived field for one purchase, from its image."""
    image, mime_type = read_image(row["receipt_image_uri"])
    extracted = extract_receipt(image, mime_type)

    currency = receipts.normalise_currency(str(extracted.get("currency", "")), settings)
    purchased_on = row["purchased_at"].date()

    where = locale.infer_location(
        language=str(extracted.get("receipt_language", "")),
        currency=currency,
        tax_rates=extracted.get("tax_rates"),
        tax_id=str(extracted.get("tax_id", "")),
        printed_city=str(extracted.get("store_city", "")),
        model_country=str(extracted.get("country_guess", "")),
    )
    store_id = stores.resolve(
        store_name=str(extracted.get("store_name") or row["store_name"]),
        location=where,
        address=str(extracted.get("store_address", "")),
        tax_id=str(extracted.get("tax_id", "")),
        settings=settings,
    )

    items = list(extracted.get("items") or [])
    mapped = canonicalise.canonicalise_items([str(i.get("item_name", "")) for i in items])
    for item, canon in zip(items, mapped, strict=False):
        item["item_name_en"] = canon.get("name_en", "")
        item["normalized_name"] = canon["canonical"]
        item["category"] = canon["category"]

    amount_eur, rate, rate_date = fx.to_base(row["total_amount"], currency, purchased_on, settings)

    return {
        "location": where,
        "store_id": store_id,
        "store_address": str(extracted.get("store_address", "")),
        "tax_id": str(extracted.get("tax_id", "")),
        "tax_rates": locale.parse_rates(extracted.get("tax_rates")),
        "items": items,
        "amount_eur": amount_eur,
        "fx_rate": rate,
        "fx_rate_date": rate_date,
        "currency": currency,
        "purchased_on": purchased_on,
    }


def apply(purchase_id: str, enriched: dict, settings) -> None:
    """Update the purchase and its items in place."""
    where = enriched["location"]

    bigquery_client().query(
        f"""
        UPDATE `{settings.table("purchases")}`
        SET receipt_language = @language, country_code = @country, region = @region,
            city_printed = @city_printed, city_inferred = @city_inferred,
            location_confidence = @confidence, location_evidence = @evidence,
            store_address = @address, tax_id = @tax_id, tax_rates = @tax_rates,
            store_id = @store_id, amount_eur = @amount_eur,
            fx_rate = @fx_rate, fx_rate_date = @fx_rate_date
        WHERE purchase_id = @pid
        """,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("pid", "STRING", purchase_id),
                bigquery.ScalarQueryParameter("language", "STRING", where.language),
                bigquery.ScalarQueryParameter("country", "STRING", where.country_code),
                bigquery.ScalarQueryParameter("region", "STRING", where.region),
                bigquery.ScalarQueryParameter("city_printed", "STRING", where.city_printed),
                bigquery.ScalarQueryParameter("city_inferred", "STRING", where.city_inferred),
                bigquery.ScalarQueryParameter("confidence", "STRING", where.confidence),
                bigquery.ScalarQueryParameter("evidence", "STRING", where.evidence_text),
                bigquery.ScalarQueryParameter("address", "STRING", enriched["store_address"]),
                bigquery.ScalarQueryParameter("tax_id", "STRING", enriched["tax_id"]),
                bigquery.ArrayQueryParameter(
                    "tax_rates", "NUMERIC", [str(r) for r in enriched["tax_rates"]]
                ),
                bigquery.ScalarQueryParameter("store_id", "STRING", enriched["store_id"]),
                bigquery.ScalarQueryParameter(
                    "amount_eur",
                    "NUMERIC",
                    None if enriched["amount_eur"] is None else str(enriched["amount_eur"]),
                ),
                bigquery.ScalarQueryParameter(
                    "fx_rate",
                    "NUMERIC",
                    None if enriched["fx_rate"] is None else str(enriched["fx_rate"]),
                ),
                bigquery.ScalarQueryParameter("fx_rate_date", "DATE", enriched["fx_rate_date"]),
            ]
        ),
    ).result()

    # Item rows are matched on the printed name, which is what the original
    # extraction stored and what a re-extraction of the same image produces.
    for item in enriched["items"]:
        line_eur, _, _ = fx.to_base(
            item.get("line_total", 0), enriched["currency"], enriched["purchased_on"], settings
        )
        bigquery_client().query(
            f"""
            UPDATE `{settings.table("purchase_items")}`
            SET item_name_en = @name_en, normalized_name = @canonical,
                category = @category, line_total_eur = @line_eur
            WHERE purchase_id = @pid AND item_name = @item_name
            """,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("pid", "STRING", purchase_id),
                    bigquery.ScalarQueryParameter(
                        "item_name", "STRING", str(item.get("item_name", ""))
                    ),
                    bigquery.ScalarQueryParameter(
                        "name_en", "STRING", str(item.get("item_name_en", ""))
                    ),
                    bigquery.ScalarQueryParameter(
                        "canonical", "STRING", str(item.get("normalized_name", ""))
                    ),
                    bigquery.ScalarQueryParameter(
                        "category", "STRING", str(item.get("category", "other"))
                    ),
                    bigquery.ScalarQueryParameter(
                        "line_eur", "NUMERIC", None if line_eur is None else str(line_eur)
                    ),
                ]
            ),
        ).result()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write the changes")
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()

    configure_logging()
    settings = get_settings()

    print(f"dataset: {settings.bq_dataset}   mode: {'APPLY' if args.apply else 'dry run'}\n")

    rows = candidates(settings, args.limit)
    if not rows:
        print("Nothing to backfill: every archived receipt already has locale data.")
        return 0

    print(f"{len(rows)} purchase(s) to re-enrich:\n")
    failures = 0

    for row in rows:
        pid = row["purchase_id"]
        print(f"  {pid}  {row['store_name']}  {row['purchased_at'].date()}")
        try:
            enriched = enrich(row, settings)
        except Exception as exc:
            failures += 1
            print(f"      FAILED: {exc}")
            continue

        where = enriched["location"]
        print(
            f"      -> {where.country_code or '?'} / {where.region or '-'} "
            f"({where.confidence}) — {where.evidence_text}"
        )
        print(
            f"      -> {len(enriched['items'])} items, "
            f"EUR {enriched['amount_eur'] if enriched['amount_eur'] is not None else '-'}"
        )

        if args.apply:
            apply(pid, enriched, settings)
            print("      updated")

    if not args.apply:
        print("\nDry run. Re-run with --apply to write these changes.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

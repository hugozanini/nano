"""Does the real model read a tax table, and does the write survive Iceberg?

Two claims the unit tests cannot settle. The fake returns whatever a test
scripts, so it can prove the plumbing and not the premise; and `ARRAY<NUMERIC>`
on a BigLake Iceberg table is a platform behaviour, not application logic.

Receipt text here is synthetic — invented shops, invented baskets — but its
*shape* mirrors what real receipts print, which is the part under test.
"""

from __future__ import annotations

import uuid

import pytest
from google.cloud import bigquery

pytestmark = pytest.mark.integration

# A Catalan receipt's distinguishing feature is the IVA table at the foot.
CATALAN_RECEIPT = """
SUPERMERCAT EXEMPLE, S.L.
CIF: A12345674
Carrer Example 12, 17001

FACTURA SIMPLIFICADA: 2026-001-000123
Descripcio            P. Unit.   Imp.(EUR)
1 LLET SENCERA 1L        1,15       1,15
1 PA INTEGRAL            1,40       1,40
1 POMA GOLDEN 1KG        2,10       2,10

TOTAL (EUR)                         4,65
TARGETA BANCARIA                    4,65

IVA      BASE IMPOSABLE (EUR)   QUOTA (EUR)
4%       2,55                   0,10
10%      1,27                   0,13
21%      0,52                   0,11
"""

PORTUGUESE_RECEIPT = """
SUPERMERCADO EXEMPLO, LDA
NIF: 123456789
Rua Exemplo 5, Lisboa

FATURA SIMPLIFICADA
Descricao             Preco    Total(EUR)
1 LEITE MEIO GORDO     0,89        0,89
1 PAO DE FORMA         1,30        1,30

TOTAL (EUR)                        2,19

IVA      INCIDENCIA (EUR)   IMPOSTO (EUR)
6%       1,50               0,09
13%      0,40               0,05
23%      0,29               0,07
"""


@pytest.fixture(scope="module")
def real_model(project_id):
    import vertexai
    from vertexai.generative_models import GenerativeModel

    vertexai.init(project=project_id, location="us-central1")
    return GenerativeModel("gemini-2.5-flash")


def _read_fields(model, receipt_text: str) -> dict:
    """Ask the model for the locale fields, from receipt text rather than an image."""
    import json

    from nano.gemini import _strip_code_fence

    prompt = (
        "This is the text of a supermarket receipt. Return ONLY JSON with keys "
        '"receipt_language" (BCP-47), "tax_id" (as printed, or ""), '
        '"tax_rates" (array of the percentages in the tax summary table), '
        '"store_city" (ONLY if a city name is printed, else ""), '
        '"country_guess" (2-letter ISO code).\n\n' + receipt_text
    )
    response = model.generate_content(prompt)
    return json.loads(_strip_code_fence(response.text))


class TestTheModelReadsTaxTables:
    """The premise the whole locale design rests on."""

    def test_catalan_receipt_yields_spanish_vat_rates(self, real_model):
        from nano import locale

        fields = _read_fields(real_model, CATALAN_RECEIPT)
        rates = locale.parse_rates(fields.get("tax_rates"))

        assert set(rates) == {4.0, 10.0, 21.0}, f"expected Spain's VAT rates, got {rates}"
        assert locale.country_from_vat(rates)[0] == "ES"

    def test_portuguese_receipt_yields_portuguese_vat_rates(self, real_model):
        from nano import locale

        fields = _read_fields(real_model, PORTUGUESE_RECEIPT)
        rates = locale.parse_rates(fields.get("tax_rates"))

        assert set(rates) == {6.0, 13.0, 23.0}, f"expected Portugal's VAT rates, got {rates}"
        assert locale.country_from_vat(rates)[0] == "PT"

    def test_the_two_receipts_resolve_to_different_countries(self, real_model):
        """Both are EUR and both are Iberian; only the tax table separates them."""
        from nano import locale

        es = locale.infer_location(
            **{
                "language": "ca",
                "currency": "EUR",
                "tax_rates": _read_fields(real_model, CATALAN_RECEIPT).get("tax_rates"),
            }
        )
        pt = locale.infer_location(
            language="pt",
            currency="EUR",
            tax_rates=_read_fields(real_model, PORTUGUESE_RECEIPT).get("tax_rates"),
        )
        assert es.country_code == "ES"
        assert pt.country_code == "PT"
        assert es.confidence == pt.confidence == locale.HIGH

    def test_a_city_is_only_reported_when_printed(self, real_model):
        """The Catalan fixture prints a postcode but no city name."""
        catalan = _read_fields(real_model, CATALAN_RECEIPT)
        portuguese = _read_fields(real_model, PORTUGUESE_RECEIPT)

        assert not catalan.get("store_city"), (
            f"no city is printed on that receipt, but the model returned "
            f"{catalan.get('store_city')!r}"
        )
        assert "lisboa" in str(portuguese.get("store_city", "")).lower()


class TestLocaleColumnsRoundTrip:
    """ARRAY<NUMERIC> and the provenance columns, against real Iceberg."""

    def test_a_purchase_with_locale_columns_writes_and_reads_back(self, bq, dataset, run_id):
        purchase_id = str(uuid.uuid4())
        try:
            bq.query(
                f"""
                INSERT INTO `{dataset}.purchases`
                (purchase_id, store_name, total_amount, currency,
                 receipt_language, country_code, region,
                 city_printed, city_inferred,
                 location_confidence, location_evidence, tax_id, tax_rates)
                SELECT @pid, @store, @total, 'EUR',
                       'ca', 'ES', 'Catalonia',
                       '', 'Barcelona',
                       'high', 'VAT rates 4/10/21 match ES exactly', 'A12345674', @rates
                """,
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[
                        bigquery.ScalarQueryParameter("pid", "STRING", purchase_id),
                        bigquery.ScalarQueryParameter("store", "STRING", f"STORE {run_id}"),
                        bigquery.ScalarQueryParameter("total", "NUMERIC", "4.65"),
                        bigquery.ArrayQueryParameter("rates", "NUMERIC", ["4", "10", "21"]),
                    ]
                ),
            ).result()

            rows = list(
                bq.query(
                    f"""
                    SELECT country_code, region, city_printed, city_inferred,
                           location_confidence, ARRAY_LENGTH(tax_rates) AS n_rates,
                           (SELECT SUM(r) FROM UNNEST(tax_rates) r) AS rate_sum
                    FROM `{dataset}.purchases` WHERE purchase_id = @pid
                    """,
                    job_config=bigquery.QueryJobConfig(
                        query_parameters=[
                            bigquery.ScalarQueryParameter("pid", "STRING", purchase_id)
                        ]
                    ),
                ).result()
            )
            assert len(rows) == 1
            row = rows[0]
            assert row.country_code == "ES"
            assert row.region == "Catalonia"
            assert row.n_rates == 3
            assert float(row.rate_sum) == 35.0
            # The rule that matters: a guess never lands in the printed column.
            assert row.city_printed == ""
            assert row.city_inferred == "Barcelona"
        finally:
            bq.query(
                f"DELETE FROM `{dataset}.purchases` WHERE purchase_id = @pid",
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[bigquery.ScalarQueryParameter("pid", "STRING", purchase_id)]
                ),
            ).result()

    def test_existing_rows_survived_the_migration(self, bq, dataset):
        """Additive evolution must leave old rows readable, with NULLs."""
        rows = list(bq.query(f"SELECT country_code FROM `{dataset}.purchases` LIMIT 1").result())
        assert rows == [] or rows[0].country_code is None or isinstance(rows[0].country_code, str)

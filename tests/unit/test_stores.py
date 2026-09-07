"""Resolving a shop once, so a `GROUP BY` sees one row per shop.

Inferring location per receipt gives inconsistent answers for the same shop —
"Catalonia" from one photo, nothing from the next when the address was cropped.
Inconsistency in a dimension is worse than missing data, because it splits one
supermarket into several rows in every aggregate.
"""

from __future__ import annotations

import pytest
from nano import stores
from nano.config import load_settings
from nano.locale import HIGH, LocationInference

from tests.fakes.bigquery import FakeBigQueryClient


@pytest.fixture
def settings():
    return load_settings()


@pytest.fixture
def wired(monkeypatch, fake_bq: FakeBigQueryClient):
    monkeypatch.setattr(stores, "bigquery_client", lambda: fake_bq)
    return fake_bq


@pytest.fixture
def catalan_location():
    return LocationInference(
        country_code="ES",
        region="Catalonia",
        city_printed="",
        city_inferred="Barcelona",
        language="ca",
        confidence=HIGH,
        evidence=["VAT rates 4/10/21 match ES exactly"],
    )


class TestBrandNormalisation:
    """Identity depends on this, so it is deterministic and model-free."""

    @pytest.mark.parametrize(
        ("printed", "expected"),
        [
            ("SUPERMERCAT EXEMPLE, S.L.", "Supermercat Exemple"),
            ("SUPERMERCAT EXEMPLE S.L", "Supermercat Exemple"),
            ("SUPERMERCAT EXEMPLE SL", "Supermercat Exemple"),
            ("SUPERMERCADO EXEMPLO, LDA", "Supermercado Exemplo"),
            ("MERCEARIA EXEMPLO LTDA", "Mercearia Exemplo"),
            ("EXAMPLE GROCER LTD", "Example Grocer"),
            ("  SUPERMERCAT   EXEMPLE  ", "Supermercat Exemple"),
        ],
    )
    def test_legal_suffixes_and_spacing_are_stripped(self, printed, expected):
        assert stores.normalise_brand(printed) == expected

    def test_mixed_case_names_keep_their_styling(self):
        assert stores.normalise_brand("Botiga Exemple") == "Botiga Exemple"

    def test_empty_input_is_safe(self):
        assert stores.normalise_brand("") == ""
        assert stores.normalise_brand("  ,. ") == ""

    def test_variants_of_one_shop_normalise_together(self):
        """The point: three printings, one brand, therefore one dimension row."""
        variants = [
            "SUPERMERCAT EXEMPLE, S.L.",
            "SUPERMERCAT EXEMPLE SL",
            "Supermercat Exemple",
        ]
        assert len({stores.normalise_brand(v) for v in variants}) == 1


class TestStoreIdentity:
    def test_the_id_is_deterministic(self):
        first = stores.store_id_for("Supermercat Exemple", "A12345674")
        second = stores.store_id_for("Supermercat Exemple", "A12345674")
        assert first == second

    def test_punctuation_in_the_tax_id_does_not_change_identity(self):
        """The same CNPJ printed two ways is the same shop."""
        assert stores.store_id_for("Loja", "12.345.678/0001-95") == stores.store_id_for(
            "Loja", "12345678000195"
        )

    def test_different_shops_get_different_ids(self):
        assert stores.store_id_for("Shop A", "A12345674") != stores.store_id_for(
            "Shop B", "A12345674"
        )

    def test_country_substitutes_when_no_tax_id_was_printed(self):
        spain = stores.store_id_for("Same Name", "", "ES")
        brazil = stores.store_id_for("Same Name", "", "BR")
        assert spain != brazil, "a shared brand in two countries is two shops"

    def test_the_tax_id_wins_over_the_country(self):
        """A tax id identifies a business outright; the country adds nothing."""
        assert stores.store_id_for("Shop", "A12345674", "ES") == stores.store_id_for(
            "Shop", "A12345674", "PT"
        )


class TestResolve:
    def test_a_new_shop_is_inserted(self, wired, settings, catalan_location):
        store_id = stores.resolve(
            "SUPERMERCAT EXEMPLE, S.L.", catalan_location, tax_id="A12345674", settings=settings
        )
        assert store_id
        assert wired.count_matching("MERGE `nano_test.stores`") == 1

    def test_the_merge_carries_the_location(self, wired, settings, catalan_location):
        stores.resolve("SUPERMERCAT EXEMPLE, S.L.", catalan_location, settings=settings)
        merge = wired.statements_matching("MERGE `nano_test.stores`")[-1]
        assert merge.param("country_code") == "ES"
        assert merge.param("region") == "Catalonia"
        assert merge.param("store_brand") == "Supermercat Exemple"

    def test_a_guess_stays_out_of_the_printed_column(self, wired, settings, catalan_location):
        stores.resolve("SUPERMERCAT EXEMPLE, S.L.", catalan_location, settings=settings)
        merge = wired.statements_matching("MERGE `nano_test.stores`")[-1]
        assert merge.param("city_printed") == ""
        assert merge.param("city_inferred") == "Barcelona"

    def test_the_same_shop_resolves_to_one_id_across_receipts(
        self, wired, settings, catalan_location
    ):
        """Two photos of the same shop must not become two dimension rows."""
        first = stores.resolve(
            "SUPERMERCAT EXEMPLE, S.L.", catalan_location, tax_id="A12345674", settings=settings
        )
        second = stores.resolve(
            "SUPERMERCAT EXEMPLE SL", catalan_location, tax_id="A-12345674", settings=settings
        )
        assert first == second

    def test_a_shop_with_no_usable_name_is_skipped(self, wired, settings, catalan_location):
        assert stores.resolve("", catalan_location, settings=settings) == ""
        assert wired.queries == []

    def test_a_merge_failure_never_costs_the_purchase(self, wired, settings, catalan_location):
        """The dimension is analytics; the purchase carries the money."""
        wired.fail("MERGE `nano_test.stores`", RuntimeError("bq unavailable"))
        store_id = stores.resolve("SUPERMERCAT EXEMPLE", catalan_location, settings=settings)
        assert store_id, "an id is still returned so the purchase can reference it"

    def test_a_lookup_failure_is_survivable(self, wired, settings, catalan_location):
        wired.fail("SELECT store_id", RuntimeError("bq unavailable"))
        assert stores.resolve("SUPERMERCAT EXEMPLE", catalan_location, settings=settings)


class TestUpgradeNotOverwrite:
    """A later, worse receipt must not erase what an earlier one established."""

    def test_the_merge_preserves_existing_non_empty_values(self, wired, settings):
        stores.resolve(
            "SUPERMERCAT EXEMPLE",
            LocationInference(country_code="", region=""),
            settings=settings,
        )
        sql = " ".join(wired.statements_matching("MERGE `nano_test.stores`")[-1].sql.split())
        # IFNULL(NULLIF(existing, ''), incoming) keeps what is already there.
        assert "IFNULL(NULLIF(T.country_code, ''), @country_code)" in sql
        assert "IFNULL(NULLIF(T.city_printed, ''), @city_printed)" in sql

    def test_repeat_visits_are_counted(self, wired, settings, catalan_location):
        stores.resolve("SUPERMERCAT EXEMPLE", catalan_location, settings=settings)
        sql = " ".join(wired.statements_matching("MERGE `nano_test.stores`")[-1].sql.split())
        assert "receipt_count = IFNULL(T.receipt_count, 0) + 1" in sql


class TestLookup:
    def test_an_unseen_store_returns_none(self, wired, settings):
        assert stores.lookup("nope", settings) is None

    def test_a_known_store_is_returned(self, wired, settings):
        wired.when(
            "SELECT store_id",
            [
                {
                    "store_id": "abc",
                    "store_name_original": "SUPERMERCAT EXEMPLE, S.L.",
                    "store_brand": "Supermercat Exemple",
                    "tax_id": "A12345674",
                    "country_code": "ES",
                    "region": "Catalonia",
                    "city_printed": "",
                    "city_inferred": "Barcelona",
                    "address": "Carrer Example 1",
                    "location_confidence": "high",
                    "location_evidence": "VAT rates 4/10/21 match ES exactly",
                }
            ],
        )
        store = stores.lookup("abc", settings)
        assert store.store_brand == "Supermercat Exemple"
        assert store.country_code == "ES"

    def test_cached_location_is_reusable(self, wired, settings):
        wired.when(
            "SELECT store_id",
            [
                {
                    "store_id": "abc",
                    "store_name_original": "S",
                    "store_brand": "S",
                    "tax_id": "",
                    "country_code": "PT",
                    "region": "",
                    "city_printed": "Lisboa",
                    "city_inferred": "",
                    "address": "",
                    "location_confidence": "high",
                    "location_evidence": "VAT rates 6/13/23 match PT exactly",
                }
            ],
        )
        assert stores.location_for("abc", settings)["country_code"] == "PT"

    def test_no_store_id_yields_nothing(self, wired, settings):
        assert stores.location_for("", settings) == {}

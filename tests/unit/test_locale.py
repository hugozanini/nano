"""Where did this receipt come from, and how much should we trust the answer?

The design claim under test: VAT rates beat language. A tax table of 4/10/21 is
Spain and nowhere else, while Catalan is also spoken in Andorra, southern France
and Sardinia, and Spanish spans two continents.

The second claim: a city is only ever recorded when it was printed. A guess
lives in its own column, so nobody can accidentally `GROUP BY` a hunch.
"""

from __future__ import annotations

import pytest
from nano import locale
from nano.locale import HIGH, LOW, MEDIUM, NONE


class TestParseRates:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ([4, 10, 21], [4.0, 10.0, 21.0]),
            (["4%", "10%", "21%"], [4.0, 10.0, 21.0]),
            (["5,5", "10", "20"], [5.5, 10.0, 20.0]),  # European decimal comma
            ([21.0, 21.0, 10.0], [10.0, 21.0]),  # deduplicated and sorted
            (21, [21.0]),  # a bare scalar
            ([], []),
            (None, []),
            (["nonsense", 10], [10.0]),  # junk dropped, rest kept
            ([0, 10, 250], [10.0]),  # implausible rates dropped
        ],
    )
    def test_parsing(self, raw, expected):
        assert locale.parse_rates(raw) == expected


class TestVatFingerprint:
    """The core claim: a tax table identifies a country outright."""

    @pytest.mark.parametrize(
        ("rates", "country"),
        [
            ([4, 10, 21], "ES"),
            ([6, 13, 23], "PT"),
            ([5.5, 10, 20], "FR"),
            ([4, 5, 10, 22], "IT"),
            ([7, 19], "DE"),
        ],
    )
    def test_full_signatures_match_exactly(self, rates, country):
        found, note = locale.country_from_vat(rates)
        assert found == country
        assert "match" in note

    def test_a_distinctive_rate_alone_is_enough(self):
        """23% is Portugal; no other country in the table uses it.

        It resolves via the subset rule rather than the distinctive-rate table,
        which is the stronger route, so this asserts the conclusion not the path.
        """
        found, note = locale.country_from_vat([23])
        assert found == "PT"
        assert note

    def test_an_ambiguous_rate_alone_decides_nothing(self):
        """21% is Spain, Belgium and the Netherlands. Refuse to guess."""
        assert locale.country_from_vat([21])[0] == ""

    def test_a_partial_table_resolves_when_only_one_country_fits(self):
        """A receipt of only zero-rated and standard-rated goods shows a subset."""
        assert locale.country_from_vat([4, 21])[0] == "ES"

    def test_no_rates_decides_nothing(self):
        assert locale.country_from_vat([])[0] == ""


class TestTaxIdFingerprint:
    @pytest.mark.parametrize(
        ("tax_id", "country"),
        [
            ("A12345674", "ES"),  # CIF
            ("12345678Z", "ES"),  # NIF
            ("12.345.678/0001-95", "BR"),  # CNPJ, formatted
            ("12345678000195", "BR"),  # CNPJ, bare
        ],
    )
    def test_recognised_formats(self, tax_id, country):
        assert locale.country_from_tax_id(tax_id)[0] == country

    def test_unrecognised_input_decides_nothing(self):
        assert locale.country_from_tax_id("not-an-id")[0] == ""
        assert locale.country_from_tax_id("")[0] == ""


class TestEvidencePrecedence:
    """Stronger evidence must win, and must raise confidence with it."""

    def test_vat_beats_language(self):
        """The whole point. Catalan text, but the tax table settles it."""
        result = locale.infer_location(language="ca", currency="EUR", tax_rates=[4, 10, 21])
        assert result.country_code == "ES"
        assert result.confidence == HIGH
        assert "VAT rates 4/10/21 match ES exactly" in result.evidence_text

    def test_vat_beats_a_wrong_model_guess(self):
        result = locale.infer_location(
            language="ca", currency="EUR", tax_rates=[4, 10, 21], model_country="AD"
        )
        assert result.country_code == "ES"

    def test_tax_id_used_when_there_is_no_tax_table(self):
        result = locale.infer_location(language="pt", currency="BRL", tax_id="12.345.678/0001-95")
        assert result.country_code == "BR"
        assert result.confidence == HIGH

    def test_currency_separates_portugal_from_brazil(self):
        """'Portuguese' alone is ambiguous; the currency is decisive."""
        portugal = locale.infer_location(language="pt", currency="EUR")
        brazil = locale.infer_location(language="pt", currency="BRL")
        assert portugal.country_code == "PT"
        assert brazil.country_code == "BR"
        assert portugal.confidence == MEDIUM
        assert brazil.confidence == MEDIUM

    def test_language_alone_is_low_confidence(self):
        result = locale.infer_location(language="es")
        assert result.country_code == "ES"
        assert result.confidence == LOW
        assert "suggests" in result.evidence_text

    def test_the_model_guess_is_the_last_resort_and_never_high(self):
        result = locale.infer_location(model_country="ES")
        assert result.country_code == "ES"
        assert result.confidence == LOW

    def test_nothing_at_all_yields_nothing(self):
        result = locale.infer_location()
        assert result.country_code == ""
        assert result.confidence == NONE


class TestCityHandling:
    """A city is a fact only when printed. Guesses live apart."""

    def test_a_printed_city_is_recorded_as_printed(self):
        result = locale.infer_location(
            language="ca", currency="EUR", tax_rates=[4, 10, 21], printed_city="Barcelona"
        )
        assert result.city_printed == "Barcelona"

    def test_a_guess_never_reaches_the_printed_column(self):
        """'Catalan, therefore Barcelona' must not become a recorded fact."""
        result = locale.infer_location(
            language="ca", currency="EUR", tax_rates=[4, 10, 21], model_city="Barcelona"
        )
        assert result.city_printed == ""
        assert result.city_inferred == "Barcelona"

    def test_a_guess_matching_the_printed_value_is_not_duplicated(self):
        result = locale.infer_location(printed_city="Girona", model_city="Girona")
        assert result.city_printed == "Girona"
        assert result.city_inferred == ""

    def test_no_city_anywhere_leaves_both_empty(self):
        result = locale.infer_location(language="ca", currency="EUR", tax_rates=[4, 10, 21])
        assert result.city_printed == ""
        assert result.city_inferred == ""


class TestRegions:
    def test_catalan_points_at_catalonia(self):
        result = locale.infer_location(language="ca", currency="EUR", tax_rates=[4, 10, 21])
        assert result.region == "Catalonia"

    def test_a_language_with_no_regional_meaning_leaves_region_empty(self):
        result = locale.infer_location(language="es", currency="EUR", tax_rates=[4, 10, 21])
        assert result.region == ""


class TestContradictions:
    """Disagreeing signals are recorded and downgrade confidence.

    Silently picking a winner is how a dataset ends up confidently wrong.
    """

    def test_currency_that_cannot_belong_to_the_country_is_flagged(self):
        result = locale.infer_location(language="ca", currency="BRL", tax_rates=[4, 10, 21])
        assert "CONTRADICTION" in result.evidence_text
        assert result.confidence != HIGH

    def test_a_contradiction_downgrades_from_high(self):
        clean = locale.infer_location(language="ca", currency="EUR", tax_rates=[4, 10, 21])
        conflicted = locale.infer_location(language="ca", currency="BRL", tax_rates=[4, 10, 21])
        assert clean.confidence == HIGH
        assert conflicted.confidence == LOW

    def test_an_unusual_language_for_the_country_is_noted_not_fatal(self):
        result = locale.infer_location(language="de", currency="EUR", tax_rates=[4, 10, 21])
        assert result.country_code == "ES"
        assert "unusual" in result.evidence_text


class TestEvidenceIsReadable:
    def test_evidence_explains_the_conclusion(self):
        result = locale.infer_location(
            language="ca", currency="EUR", tax_rates=[4, 10, 21], tax_id="A12345674"
        )
        text = result.evidence_text
        assert "VAT" in text
        assert "EUR" in text
        assert "ca" in text

    def test_country_name_is_resolved_for_display(self):
        result = locale.infer_location(tax_rates=[4, 10, 21])
        assert result.country_name == "Spain"


class TestLanguageNormalisation:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("ca", "ca"), ("ca-ES", "ca"), ("PT_br", "pt-br"), ("EN-GB", "en"), ("", "")],
    )
    def test_tags(self, raw, expected):
        assert locale.normalise_language(raw) == expected


class TestRealisticReceipts:
    """End-to-end shapes for each language the household will actually send.

    Values are synthetic; only the structure mirrors real receipts.
    """

    def test_catalan_supermarket(self):
        result = locale.infer_location(
            language="ca", currency="EUR", tax_rates=[4, 10, 21], tax_id="A12345674"
        )
        assert (result.country_code, result.region, result.confidence) == (
            "ES",
            "Catalonia",
            HIGH,
        )

    def test_portuguese_from_portugal(self):
        result = locale.infer_location(language="pt", currency="EUR", tax_rates=[6, 13, 23])
        assert (result.country_code, result.confidence) == ("PT", HIGH)

    def test_portuguese_from_brazil(self):
        result = locale.infer_location(
            language="pt-br", currency="BRL", tax_id="12.345.678/0001-95"
        )
        assert (result.country_code, result.confidence) == ("BR", HIGH)

    def test_english_receipt_with_no_tax_table(self):
        """The weak case, and it should look weak."""
        result = locale.infer_location(language="en", currency="GBP")
        assert result.country_code == "GB"
        assert result.confidence == MEDIUM

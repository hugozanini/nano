"""Receipt handling, list events, and history answers.

Each class here corresponds to something that was observed to be wrong in
production rather than something imagined.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import ClassVar

import pytest
from nano import events, history, receipts
from nano.config import load_settings

from tests.fakes.bigquery import FakeBigQueryClient
from tests.fakes.storage import FakeStorageClient


@pytest.fixture
def settings():
    return load_settings()


@pytest.fixture
def wired_history(monkeypatch, fake_bq: FakeBigQueryClient):
    monkeypatch.setattr(history, "bigquery_client", lambda: fake_bq)
    return fake_bq


@pytest.fixture
def wired_events(monkeypatch, fake_bq: FakeBigQueryClient):
    monkeypatch.setattr(events, "bigquery_client", lambda: fake_bq)
    return fake_bq


@pytest.fixture
def wired_storage(monkeypatch, fake_storage: FakeStorageClient):
    monkeypatch.setattr(receipts, "storage_client", lambda: fake_storage)
    return fake_storage


# --------------------------------------------------------------------------
# Receipt dates
# --------------------------------------------------------------------------


class TestSanitisePurchaseDate:
    TODAY = datetime.date(2026, 9, 7)

    def test_a_plausible_date_is_taken_as_is(self):
        date, note = receipts.sanitise_purchase_date("2026-08-29", today=self.TODAY)
        assert date == datetime.date(2026, 8, 29)
        assert note == ""

    def test_an_unambiguous_date_is_never_swapped(self):
        """29 August has no valid swap — there is no month 29.

        This matters: the real receipt in production was 2026-08-29 and was read
        correctly. A date-repair rule that damaged it would be worse than none.
        """
        date, note = receipts.sanitise_purchase_date("2026-08-29", today=self.TODAY)
        assert date == datetime.date(2026, 8, 29)
        assert note == ""

    def test_a_future_date_with_a_valid_swap_is_reinterpreted(self):
        """2026-12-03 read on 7 Sept is really 3 December -> 12 March."""
        date, note = receipts.sanitise_purchase_date("2026-12-03", today=self.TODAY)
        assert date == datetime.date(2026, 3, 12)
        assert "future" in note

    def test_a_future_date_without_a_valid_swap_falls_back_to_today(self):
        date, note = receipts.sanitise_purchase_date("2027-01-15", today=self.TODAY)
        assert date == self.TODAY
        assert "future" in note

    def test_a_very_old_date_with_a_valid_swap_is_reinterpreted(self):
        date, note = receipts.sanitise_purchase_date("2026-01-09", today=self.TODAY)
        assert date == datetime.date(2026, 9, 1)
        assert "too old" in note

    def test_a_genuinely_old_receipt_is_kept(self):
        """Old but unambiguous: no month 25, so it cannot be a swap."""
        date, note = receipts.sanitise_purchase_date("2025-03-25", today=self.TODAY)
        assert date == datetime.date(2025, 3, 25)
        assert note == ""

    @pytest.mark.parametrize("raw", ["", "not a date", "29/08/2026", None])
    def test_unparseable_values_fall_back_to_today(self, raw):
        date, note = receipts.sanitise_purchase_date(raw or "", today=self.TODAY)
        assert date == self.TODAY
        assert note

    def test_an_impossible_date_falls_back(self):
        date, _ = receipts.sanitise_purchase_date("2026-02-31", today=self.TODAY)
        assert date == self.TODAY


class TestNormaliseCurrency:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("EUR", "EUR"), ("eur", "EUR"), ("  brl ", "BRL"), ("€", "EUR"), ("$", "USD")],
    )
    def test_recognised_values(self, raw, expected, settings):
        assert receipts.normalise_currency(raw, settings) == expected

    @pytest.mark.parametrize("raw", ["", "euros", "?", "XYZQ"])
    def test_unrecognised_values_use_the_household_default(self, raw, settings):
        assert receipts.normalise_currency(raw, settings) == "EUR"


# --------------------------------------------------------------------------
# Receipt archival
# --------------------------------------------------------------------------


class TestArchiveReceipt:
    def test_uploads_and_returns_a_gs_uri(self, wired_storage, settings):
        uri = receipts.archive_receipt(
            b"jpegbytes", "image/jpeg", "abc-123", datetime.date(2026, 8, 29), settings
        )
        assert uri == "gs://test-receipts-bucket/receipts/2026/08/abc-123.jpg"
        assert wired_storage.uploads[0].content == b"jpegbytes"
        assert wired_storage.uploads[0].content_type == "image/jpeg"

    def test_path_is_browsable_by_year_and_month(self, wired_storage, settings):
        receipts.archive_receipt(b"x", "image/png", "id", datetime.date(2026, 1, 5), settings)
        assert wired_storage.paths == ["receipts/2026/01/id.png"]

    def test_a_storage_failure_returns_empty_rather_than_raising(self, wired_storage, settings):
        """Losing the image must never lose the purchase, which has the money in it."""
        wired_storage.fails(RuntimeError("bucket on fire"))
        assert (
            receipts.archive_receipt(b"x", "image/jpeg", "id", datetime.date(2026, 8, 29), settings)
            == ""
        )

    def test_no_bucket_configured_is_handled(self, monkeypatch, wired_storage):
        monkeypatch.setenv("RECEIPTS_BUCKET", "")
        assert (
            receipts.archive_receipt(
                b"x", "image/jpeg", "id", datetime.date(2026, 8, 29), load_settings()
            )
            == ""
        )


# --------------------------------------------------------------------------
# list_events
# --------------------------------------------------------------------------


class TestListEvents:
    def test_one_row_per_item_in_a_single_statement(self, wired_events, settings):
        written = events.record(events.ADD_ITEM, ["coffee", "milk"], "alex", settings, "wamid.A")
        assert written == 2
        assert wired_events.count_matching("INSERT INTO `nano_test.list_events`") == 1

    def test_rows_carry_the_sender(self, wired_events, settings):
        events.record(events.ADD_ITEM, ["coffee"], "alex", settings, "wamid.A")
        rows = wired_events.queries[-1].param("rows")
        assert rows[0]["sender"] == "alex"
        assert rows[0]["event_type"] == "add_item"
        assert rows[0]["item_name"] == "coffee"

    def test_close_list_is_a_single_event_with_no_item(self, wired_events, settings):
        written = events.record(events.CLOSE_LIST, ["coffee", "milk"], "alex", settings, "wamid.A")
        assert written == 1
        assert wired_events.queries[-1].param("rows")[0]["item_name"] == ""

    def test_event_ids_are_deterministic_for_the_same_message(self, wired_events, settings):
        events.record(events.ADD_ITEM, ["coffee"], "alex", settings, "wamid.A")
        first = wired_events.queries[-1].param("rows")[0]["event_id"]
        events.record(events.ADD_ITEM, ["coffee"], "alex", settings, "wamid.A")
        second = wired_events.queries[-1].param("rows")[0]["event_id"]
        assert first == second, "a reprocessed message must not double-count in the pattern data"

    def test_a_bigquery_failure_never_raises(self, wired_events, settings):
        """Analytics must not break the user's action."""
        wired_events.fail("INSERT INTO `nano_test.list_events`", RuntimeError("bq down"))
        assert events.record(events.ADD_ITEM, ["coffee"], "alex", settings, "wamid.A") == 0

    def test_unknown_event_types_are_refused(self, wired_events, settings):
        assert events.record("teleport", ["coffee"], "alex", settings, "wamid.A") == 0
        assert wired_events.queries == []

    def test_empty_item_lists_write_nothing(self, wired_events, settings):
        assert events.record(events.ADD_ITEM, ["", "   "], "alex", settings, "wamid.A") == 0


# --------------------------------------------------------------------------
# History answers
# --------------------------------------------------------------------------


class TestTotalSpend:
    def test_empty_period_reports_the_most_recent_trip(self, wired_history, settings):
        """The 0.00 bug.

        The query was right and the answer was useless: asked moments after
        logging a receipt, `You've spent 0.00 across 0 shopping trip(s)` reads
        as data loss. The purchase was simply in the previous month.
        """
        wired_history.when("GROUP BY currency", [])
        wired_history.when(
            "ORDER BY p.purchased_at DESC",
            [
                {
                    "purchase_id": "p-1",
                    "purchased_at": datetime.datetime(2026, 8, 29),
                    "store_name": "SUPERMERCAT EXEMPLE, S.L.",
                    "total_amount": Decimal("21.70"),
                    "currency": "EUR",
                }
            ],
        )
        answer = history.answer_history_query("total_spend", "this_month", None, settings)

        assert "0.00" not in answer
        assert "29 Aug" in answer
        assert "SUPERMERCAT EXEMPLE, S.L." in answer
        assert "EUR 21.70" in answer

    def test_no_purchases_at_all_says_so_helpfully(self, wired_history, settings):
        wired_history.when("GROUP BY currency", [])
        wired_history.when("ORDER BY p.purchased_at DESC", [])
        answer = history.answer_history_query("total_spend", "this_month", None, settings)
        assert "haven't logged any receipts yet" in answer

    def test_single_currency_total(self, wired_history, settings):
        wired_history.when(
            "GROUP BY currency", [{"currency": "EUR", "total": Decimal("120.40"), "trips": 4}]
        )
        answer = history.answer_history_query("total_spend", "this_month", None, settings)
        assert "EUR 120.40" in answer
        assert "4 shopping trips" in answer

    def test_multiple_currencies_are_reported_separately(self, wired_history, settings):
        """Summing EUR and BRL produces a number that means nothing."""
        wired_history.when(
            "GROUP BY currency",
            [
                {"currency": "EUR", "total": Decimal("120.40"), "trips": 4},
                {"currency": "BRL", "total": Decimal("300.00"), "trips": 2},
            ],
        )
        answer = history.answer_history_query("total_spend", "this_month", None, settings)
        assert "EUR 120.40" in answer
        assert "BRL 300.00" in answer
        assert "6 shopping trips" in answer

    def test_singular_trip_reads_correctly(self, wired_history, settings):
        wired_history.when(
            "GROUP BY currency", [{"currency": "EUR", "total": Decimal("21.70"), "trips": 1}]
        )
        assert "1 shopping trip." in history.answer_history_query(
            "total_spend", "this_month", None, settings
        )


class TestItemsInLastPurchase:
    def test_prices_are_not_nested_inside_the_name_brackets(self, wired_history, settings):
        """ "eggs (24 OUS FRESC) (EUR 10.50)" reads badly on a phone."""
        wired_history.when(
            "ORDER BY p.purchased_at DESC",
            [
                {
                    "purchase_id": "p1",
                    "purchased_at": datetime.datetime(2026, 8, 29),
                    "store_name": "Supermercat Exemple",
                    "total_amount": Decimal("1.15"),
                    "currency": "EUR",
                }
            ],
        )
        wired_history.when(
            "ORDER BY line_total DESC",
            [
                {
                    "item_name": "LLET SENCERA 1L",
                    "item_name_en": "whole milk, 1L",
                    "line_total": Decimal("1.15"),
                }
            ],
        )
        answer = history.answer_history_query("items_in_last_purchase", settings=settings)
        assert "(LLET SENCERA 1L) (EUR" not in answer
        assert "(LLET SENCERA 1L) \u2014 EUR 1.15" in answer

    def test_lists_the_items_from_the_most_recent_trip(self, wired_history, settings):
        """ "What did we buy?" had no intent and fell through to the shopping list."""
        wired_history.when(
            "ORDER BY p.purchased_at DESC",
            [
                {
                    "purchase_id": "p-1",
                    "purchased_at": datetime.datetime(2026, 8, 29),
                    "store_name": "SUPERMERCAT EXEMPLE, S.L.",
                    "total_amount": Decimal("21.70"),
                    "currency": "EUR",
                }
            ],
        )
        wired_history.when(
            "ORDER BY line_total DESC",
            [
                {"item_name": "OLI OLIVA 1L", "line_total": Decimal("6.45")},
                {"item_name": "LLET SENCERA 1L", "line_total": Decimal("1.15")},
            ],
        )
        answer = history.answer_history_query("items_in_last_purchase", settings=settings)
        assert "OLI OLIVA 1L" in answer
        assert "LLET SENCERA 1L" in answer
        assert "2 items" in answer

    def test_no_receipts_yet(self, wired_history, settings):
        wired_history.when("ORDER BY p.purchased_at DESC", [])
        answer = history.answer_history_query("items_in_last_purchase", settings=settings)
        assert "haven't logged any receipts yet" in answer


class TestItemQueries:
    def test_spend_by_item_groups_by_currency(self, wired_history, settings):
        wired_history.when(
            "GROUP BY p.currency", [{"currency": "EUR", "total": Decimal("9.20"), "times": 8}]
        )
        answer = history.answer_history_query("spend_by_item", item_name="milk", settings=settings)
        assert "EUR 9.20" in answer
        assert "8 times" in answer

    def test_unknown_item_says_so(self, wired_history, settings):
        wired_history.when("GROUP BY p.currency", [])
        answer = history.answer_history_query(
            "spend_by_item", item_name="caviar", settings=settings
        )
        assert "caviar" in answer

    def test_item_query_without_a_name_asks_for_one(self, wired_history, settings):
        assert "Which item" in history.answer_history_query("spend_by_item", settings=settings)

    def test_a_query_failure_degrades_gracefully(self, wired_history, settings):
        wired_history.fail("GROUP BY currency", RuntimeError("bq unavailable"))
        answer = history.answer_history_query("total_spend", "this_month", None, settings)
        assert "couldn't reach" in answer


class TestDateRanges:
    def test_ranges_use_the_household_timezone(self, monkeypatch, settings):
        """UTC would put a late-evening Madrid shop on the following day."""
        monkeypatch.setenv("HOUSEHOLD_TIMEZONE", "Europe/Madrid")
        madrid = history.today_in_household_tz(load_settings())
        monkeypatch.setenv("HOUSEHOLD_TIMEZONE", "Pacific/Kiritimati")
        kiritimati = history.today_in_household_tz(load_settings())
        assert (kiritimati - madrid).days in (0, 1)

    def test_an_unknown_timezone_falls_back_to_utc(self, monkeypatch):
        monkeypatch.setenv("HOUSEHOLD_TIMEZONE", "Middle/Earth")
        assert history.today_in_household_tz(load_settings()) is not None

    def test_this_month_starts_on_the_first(self, settings):
        start, end = history.period_to_date_range("this_month", settings)
        assert start.day == 1
        assert start <= end

    def test_last_month_covers_the_whole_previous_month(self, settings):
        start, end = history.period_to_date_range("last_month", settings)
        assert start.day == 1
        assert end.month == start.month
        assert (end + datetime.timedelta(days=1)).month != start.month


class TestItemsAreScopedToOnePurchase:
    """Regression: "what did we buy?" listed 24 items from a 12-item receipt.

    The query filtered by `DATE(purchased_at)` rather than by `purchase_id`.
    Two purchases happened to share a date — the same shop photographed twice —
    so the join returned every line item twice.
    """

    def test_only_the_latest_purchase_is_listed(self, wired_history, settings):
        wired_history.when(
            "ORDER BY p.purchased_at DESC",
            [
                {
                    "purchase_id": "newest",
                    "purchased_at": datetime.datetime(2026, 8, 29),
                    "store_name": "SUPERMERCAT EXEMPLE, S.L.",
                    "total_amount": Decimal("21.70"),
                    "currency": "EUR",
                }
            ],
        )
        wired_history.when(
            "ORDER BY line_total DESC",
            [{"item_name": "OLI OLIVA 1L", "line_total": Decimal("6.45")}],
        )
        answer = history.answer_history_query("items_in_last_purchase", settings=settings)

        assert "1 items" in answer
        item_query = wired_history.statements_matching("ORDER BY line_total DESC")[-1]
        assert item_query.param("purchase_id") == "newest"

    def test_the_item_query_does_not_filter_by_date(self, wired_history, settings):
        """Filtering by date is what let a second purchase bleed in."""
        wired_history.when(
            "ORDER BY p.purchased_at DESC",
            [
                {
                    "purchase_id": "newest",
                    "purchased_at": datetime.datetime(2026, 8, 29),
                    "store_name": "S",
                    "total_amount": Decimal("1.00"),
                    "currency": "EUR",
                }
            ],
        )
        wired_history.when("ORDER BY line_total DESC", [])
        history.answer_history_query("items_in_last_purchase", settings=settings)

        sql = wired_history.statements_matching("ORDER BY line_total DESC")[-1].sql
        assert "purchase_id = @purchase_id" in " ".join(sql.split())
        assert "DATE(p.purchased_at)" not in sql


class TestItemNamesAreShownInEnglish:
    """A list of MADUIXA, PIT FAM. and GREC NATURAL P-6 is unreadable.

    Asked "what have I purchased last time?", Nano listed twelve Catalan
    abbreviations — the English translations had been stored since the
    translation work but were never read back.
    """

    def _latest(self):
        return [
            {
                "purchase_id": "p1",
                "purchased_at": datetime.datetime(2026, 8, 29),
                "store_name": "Supermercat Exemple",
                "total_amount": Decimal("21.70"),
                "currency": "EUR",
            }
        ]

    def test_english_leads_with_the_printed_name_beside_it(self, wired_history, settings):
        wired_history.when("ORDER BY p.purchased_at DESC", self._latest())
        wired_history.when(
            "ORDER BY line_total DESC",
            [
                {
                    "item_name": "LLET SENCERA 1L",
                    "item_name_en": "whole milk, 1L",
                    "line_total": Decimal("1.15"),
                }
            ],
        )
        answer = history.answer_history_query("items_in_last_purchase", settings=settings)
        assert "whole milk, 1L (LLET SENCERA 1L)" in answer

    def test_the_printed_name_is_used_when_there_is_no_translation(self, wired_history, settings):
        """Older rows predate the translation column; they must still render."""
        wired_history.when("ORDER BY p.purchased_at DESC", self._latest())
        wired_history.when(
            "ORDER BY line_total DESC",
            [{"item_name": "MADUIXA", "item_name_en": None, "line_total": Decimal("3.11")}],
        )
        answer = history.answer_history_query("items_in_last_purchase", settings=settings)
        assert "MADUIXA" in answer
        assert "()" not in answer, "an empty bracket is worse than no bracket"

    def test_an_identical_translation_is_not_repeated(self, wired_history, settings):
        """ "BANANA" translates to "banana"; printing both is noise."""
        wired_history.when("ORDER BY p.purchased_at DESC", self._latest())
        wired_history.when(
            "ORDER BY line_total DESC",
            [{"item_name": "BANANA", "item_name_en": "banana", "line_total": Decimal("1.36")}],
        )
        answer = history.answer_history_query("items_in_last_purchase", settings=settings)
        assert "banana (BANANA)" not in answer

    def test_the_normalised_brand_is_preferred_over_the_legal_name(self, wired_history, settings):
        """ "Mercadona" reads better than "MERCADONA, S.A."."""
        sql = " ".join(
            history.most_recent_purchase.__doc__ or "",
        )
        wired_history.when("ORDER BY p.purchased_at DESC", self._latest())
        wired_history.when("ORDER BY line_total DESC", [])
        history.answer_history_query("items_in_last_purchase", settings=settings)

        query = wired_history.statements_matching("ORDER BY p.purchased_at DESC")[-1].sql
        assert "store_brand" in query, f"should join the store dimension {sql}"


class TestEnglishIsAlwaysTheOutputLanguage:
    """Standardising receipts into one language is one of Nano's purposes.

    Receipts arrive in Catalan, Spanish, Portuguese or English; the answer is
    always English. This is a product rule rather than a per-feature option, so
    it is asserted here for every path that surfaces an item name — trusting
    each site to remember is how `_last_purchase` kept returning `MADUIXA` long
    after the translations were being stored.
    """

    LATEST: ClassVar[list[dict]] = [
        {
            "purchase_id": "p1",
            "purchased_at": datetime.datetime(2026, 8, 29),
            "store_name": "Supermercat Exemple",
            "total_amount": Decimal("3.11"),
            "currency": "EUR",
        }
    ]

    def test_the_last_purchase_list_is_english(self, wired_history, settings):
        wired_history.when("ORDER BY p.purchased_at DESC", self.LATEST)
        wired_history.when(
            "ORDER BY line_total DESC",
            [
                {
                    "item_name": "MADUIXA",
                    "item_name_en": "strawberry",
                    "line_total": Decimal("3.11"),
                }
            ],
        )
        answer = history.answer_history_query("items_in_last_purchase", settings=settings)
        assert "strawberry" in answer

    def test_when_did_we_last_buy_is_english(self, wired_history, settings):
        """The path that was still returning the printed Catalan name."""
        wired_history.when(
            "ORDER BY p.purchased_at DESC",
            [
                {
                    "purchased_at": datetime.datetime(2026, 8, 29),
                    "store_name": "Supermercat Exemple",
                    "item_name": "MADUIXA",
                    "item_name_en": "strawberry",
                    "line_total": Decimal("3.11"),
                    "currency": "EUR",
                }
            ],
        )
        answer = history.answer_history_query(
            "last_purchase", item_name="strawberry", settings=settings
        )
        assert "strawberry" in answer
        assert "strawberry (MADUIXA)" in answer, "the printed name stays for receipt matching"

    def test_the_printed_name_is_the_fallback_not_the_default(self, wired_history, settings):
        """Rows predating the translations must still render, from the original."""
        wired_history.when("ORDER BY p.purchased_at DESC", self.LATEST)
        wired_history.when(
            "ORDER BY line_total DESC",
            [{"item_name": "MADUIXA", "item_name_en": "", "line_total": Decimal("3.11")}],
        )
        answer = history.answer_history_query("items_in_last_purchase", settings=settings)
        assert "MADUIXA" in answer

    def test_every_item_query_selects_the_english_column(self, wired_history, settings):
        """A path that never selects it can never render it.

        Cheaper and more durable than asserting on each rendered string: if a new
        query forgets the column, this fails without anyone having to think of it.
        """
        wired_history.when("ORDER BY p.purchased_at DESC", self.LATEST)
        wired_history.when("ORDER BY line_total DESC", [])
        history.answer_history_query("items_in_last_purchase", settings=settings)
        history.answer_history_query("last_purchase", item_name="milk", settings=settings)

        item_queries = [
            q for q in wired_history.queries if "purchase_items" in q.sql and "item_name" in q.sql
        ]
        assert item_queries, "expected queries that read item names"
        for query in item_queries:
            assert "item_name_en" in query.sql, (
                f"this query shows item names but never selects the translation:\n{query.sql}"
            )


class TestStoreNamesAreNormalised:
    """ "Mercadona" reads better than "MERCADONA, S.A." — and the same shop
    should not appear under two different names in two different answers."""

    def test_every_answer_that_names_a_store_joins_the_dimension(self, wired_history, settings):
        wired_history.when("ORDER BY p.purchased_at DESC", [])
        history.answer_history_query("items_in_last_purchase", settings=settings)
        history.answer_history_query("last_purchase", item_name="milk", settings=settings)

        store_queries = [q for q in wired_history.queries if "store_name" in q.sql]
        assert store_queries
        for query in store_queries:
            assert "store_brand" in query.sql, (
                f"this query names a store but not from the dimension:\n{query.sql}"
            )

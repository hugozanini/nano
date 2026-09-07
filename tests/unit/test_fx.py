"""One comparable currency, without ever risking the purchase.

A household shopping in Spain and Brazil cannot sum EUR and BRL, so every amount
also lands in EUR — at the rate that applied on the purchase date, not today's,
so last year's totals still reconcile this year.

The failure behaviour matters as much as the arithmetic. The ECB publishes on
business days only and the feed can simply be unreachable, so a missing rate
must produce a NULL converted amount and a logged warning, never a lost receipt
and never a wrong number.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from nano import fx
from nano.config import load_settings

MONDAY = datetime.date(2026, 9, 7)
SATURDAY = datetime.date(2026, 9, 5)
FRIDAY = datetime.date(2026, 9, 4)


@pytest.fixture
def settings():
    return load_settings()


@pytest.fixture
def wired(monkeypatch, fake_firestore):
    monkeypatch.setattr(fx, "firestore_client", lambda: fake_firestore)
    # No unit test reaches the network; the feed is explicitly unavailable
    # unless a test scripts it.
    monkeypatch.setattr(fx, "fetch_ecb_rates", dict)
    return fake_firestore


def cache(store, settings, on: datetime.date, **rates):
    store.store[f"{settings.collection('fx_rates')}/{on.isoformat()}"] = {
        "rates": {k: str(v) for k, v in rates.items()},
        "source": "ecb",
    }


class TestBaseCurrency:
    def test_euro_needs_no_lookup(self, wired, settings):
        rate, on = fx.rate_for("EUR", MONDAY, settings)
        assert rate == Decimal("1")
        assert on == MONDAY
        assert wired.reads == [], "EUR must not touch the cache at all"

    def test_converting_euros_is_the_identity(self, wired, settings):
        amount, rate, _ = fx.to_base("43.06", "EUR", MONDAY, settings)
        assert amount == Decimal("43.06")
        assert rate == Decimal("1")


class TestConversion:
    def test_a_cached_rate_is_used(self, wired, settings):
        cache(wired, settings, MONDAY, BRL=Decimal("6.20"))
        amount, rate, rate_date = fx.to_base("310.00", "BRL", MONDAY, settings)
        assert amount == Decimal("50.00")
        assert rate == Decimal("6.20")
        assert rate_date == MONDAY

    def test_the_result_is_rounded_to_cents(self, wired, settings):
        cache(wired, settings, MONDAY, BRL=Decimal("6.13"))
        amount, _, _ = fx.to_base("100.00", "BRL", MONDAY, settings)
        assert amount == Decimal("16.31")

    def test_ecb_quotes_are_units_per_euro(self, wired, settings):
        """BRL 6.20 to the euro means 620 BRL is 100 EUR, not 3844."""
        cache(wired, settings, MONDAY, BRL=Decimal("6.20"))
        amount, _, _ = fx.to_base("620.00", "BRL", MONDAY, settings)
        assert amount == Decimal("100.00")


class TestHistoricalAccuracy:
    def test_the_rate_from_the_purchase_date_is_used(self, wired, settings):
        """A 2024 purchase must keep its 2024 rate, or old totals drift."""
        old = datetime.date(2024, 3, 15)
        cache(wired, settings, old, BRL=Decimal("5.00"))
        cache(wired, settings, MONDAY, BRL=Decimal("6.20"))

        amount, rate, rate_date = fx.to_base("100.00", "BRL", old, settings)
        assert rate == Decimal("5.00")
        assert rate_date == old
        assert amount == Decimal("20.00")

    def test_the_rate_and_its_date_are_returned_for_auditing(self, wired, settings):
        cache(wired, settings, MONDAY, BRL=Decimal("6.20"))
        _, rate, rate_date = fx.to_base("10", "BRL", MONDAY, settings)
        assert rate is not None and rate_date is not None


class TestWeekendsAndHolidays:
    def test_a_weekend_purchase_uses_the_previous_business_day(self, wired, settings):
        """The ECB does not publish on Saturdays; walking back is expected."""
        cache(wired, settings, FRIDAY, BRL=Decimal("6.20"))
        amount, _rate, rate_date = fx.to_base("62.00", "BRL", SATURDAY, settings)
        assert rate_date == FRIDAY
        assert amount == Decimal("10.00")

    def test_a_rate_older_than_the_limit_is_refused(self, wired, settings):
        """Beyond a fortnight the number stops being meaningful."""
        stale = MONDAY - datetime.timedelta(days=40)
        cache(wired, settings, stale, BRL=Decimal("6.20"))
        amount, rate, _ = fx.to_base("100", "BRL", MONDAY, settings)
        assert amount is None
        assert rate is None


class TestFailureNeverLosesThePurchase:
    def test_an_unreachable_feed_yields_no_amount_rather_than_a_wrong_one(self, wired, settings):
        amount, rate, rate_date = fx.to_base("100", "BRL", MONDAY, settings)
        assert (amount, rate, rate_date) == (None, None, None)

    def test_an_unknown_currency_is_survivable(self, wired, settings):
        cache(wired, settings, MONDAY, BRL=Decimal("6.20"))
        assert fx.to_base("100", "XYZ", MONDAY, settings)[0] is None

    def test_an_unparseable_amount_is_survivable(self, wired, settings):
        cache(wired, settings, MONDAY, BRL=Decimal("6.20"))
        assert fx.to_base("not a number", "BRL", MONDAY, settings)[0] is None

    def test_a_firestore_failure_is_survivable(self, monkeypatch, settings):
        class Exploding:
            def collection(self, *_):
                raise RuntimeError("firestore down")

        monkeypatch.setattr(fx, "firestore_client", lambda: Exploding())
        monkeypatch.setattr(fx, "fetch_ecb_rates", dict)
        assert fx.to_base("100", "BRL", MONDAY, settings)[0] is None

    def test_a_zero_rate_does_not_divide_by_zero(self, wired, settings):
        cache(wired, settings, MONDAY, BRL=Decimal("0"))
        assert fx.to_base("100", "BRL", MONDAY, settings)[0] is None

    def test_nothing_raises_for_any_input(self, wired, settings):
        for amount, currency in [(None, "BRL"), ("", ""), ("10", None), ({}, "EUR")]:
            fx.to_base(amount, currency, MONDAY, settings)


class TestFeedRefresh:
    def test_an_empty_cache_triggers_a_fetch_and_caches_the_result(
        self, monkeypatch, wired, settings
    ):
        monkeypatch.setattr(fx, "fetch_ecb_rates", lambda: {MONDAY: {"BRL": Decimal("6.20")}})
        amount, _rate, rate_date = fx.to_base("62.00", "BRL", MONDAY, settings)

        assert amount == Decimal("10.00")
        assert rate_date == MONDAY
        assert any("fx_rates" in path for path in wired.store), "the fetch should be cached"

    def test_a_cached_rate_avoids_the_feed_entirely(self, monkeypatch, wired, settings):
        calls = []
        monkeypatch.setattr(fx, "fetch_ecb_rates", lambda: calls.append(1) or {})
        cache(wired, settings, MONDAY, BRL=Decimal("6.20"))

        fx.to_base("100", "BRL", MONDAY, settings)
        assert calls == [], "a cached rate must not hit the network"


class TestFeedParsing:
    def test_the_ecb_xml_shape_is_parsed(self, monkeypatch):
        xml = b"""<?xml version="1.0" encoding="UTF-8"?>
        <gesmes:Envelope xmlns:gesmes="http://www.gesmes.org/xml/2002-08-01"
                         xmlns="http://www.ecb.int/vocabulary/2002-08-01/eurofxref">
          <Cube>
            <Cube time="2026-09-07">
              <Cube currency="USD" rate="1.0850"/>
              <Cube currency="BRL" rate="6.2000"/>
            </Cube>
            <Cube time="2026-09-04">
              <Cube currency="BRL" rate="6.1500"/>
            </Cube>
          </Cube>
        </gesmes:Envelope>"""

        class Response:
            content = xml

            def raise_for_status(self):
                pass

        monkeypatch.setattr(fx.requests, "get", lambda *a, **k: Response())
        rates = fx.fetch_ecb_rates()

        assert rates[MONDAY]["BRL"] == Decimal("6.2000")
        assert rates[MONDAY]["USD"] == Decimal("1.0850")
        assert rates[FRIDAY]["BRL"] == Decimal("6.1500")

    def test_malformed_xml_returns_nothing_rather_than_raising(self, monkeypatch):
        class Response:
            content = b"<not xml"

            def raise_for_status(self):
                pass

        monkeypatch.setattr(fx.requests, "get", lambda *a, **k: Response())
        assert fx.fetch_ecb_rates() == {}

    def test_a_network_error_returns_nothing_rather_than_raising(self, monkeypatch):
        def explode(*a, **k):
            raise fx.requests.RequestException("unreachable")

        monkeypatch.setattr(fx.requests, "get", explode)
        assert fx.fetch_ecb_rates() == {}

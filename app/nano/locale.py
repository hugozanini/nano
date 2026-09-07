"""Working out where a receipt came from, and how confident we are.

Receipts arrive in whatever language the shop prints, and analysis needs to know
where a purchase happened. The obvious signal is the language, and it is the
weakest one available.

**VAT rates are a deterministic country fingerprint.** A Spanish receipt prints
its tax table:

    IVA    BASE IMPOSABLE (EUR)   QUOTA (EUR)
    4%     20,79                  0,83
    10%    12,67                  1,27
    21%    6,20                   1,30

4/10/21 is Spain and nowhere else. Portugal is 6/13/23, France 5.5/10/20. That
is stronger evidence than Catalan, which is also spoken in Andorra, southern
France and Sardinia — and stronger than Spanish, spoken across two continents.

**Language identifies a region, never a city.** "Catalan, therefore Barcelona"
is a fair human hunch and a terrible thing to write into a table someone will
`GROUP BY city`. So a city is only ever recorded when it was printed on the
receipt; a guess is kept in a separate field, and both travel with a confidence
and a plain-language note saying what the conclusion rests on.

The model proposes and this module decides — the same division of labour as
`receipts.sanitise_purchase_date`, which overrides the model only when its
answer is impossible or implausible. Everything here is a pure function.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

HIGH = "high"
MEDIUM = "medium"
LOW = "low"
NONE = "none"

# Standard VAT rate sets, as printed in a receipt's tax table. Matching is on
# the full set, so a receipt showing only some of a country's rates falls
# through to a subset check rather than matching the wrong country outright.
VAT_SIGNATURES: dict[frozenset[float], str] = {
    frozenset({4.0, 10.0, 21.0}): "ES",
    frozenset({6.0, 13.0, 23.0}): "PT",
    frozenset({5.5, 10.0, 20.0}): "FR",
    frozenset({4.0, 5.0, 10.0, 22.0}): "IT",
    frozenset({7.0, 19.0}): "DE",
    frozenset({5.0, 20.0}): "GB",
    frozenset({6.0, 12.0, 21.0}): "BE",
    frozenset({9.0, 21.0}): "NL",
}

# The rate that is distinctive on its own. 21% alone is ambiguous (ES, BE, NL),
# so only genuinely unique rates appear here.
DISTINCTIVE_RATES: dict[float, str] = {
    23.0: "PT",
    5.5: "FR",
    22.0: "IT",
    19.0: "DE",
}

# Tax identifiers. Matched after stripping punctuation, because the same id is
# printed as "12.345.678/0001-95" or "12345678000195" depending on the shop, and
# a pattern per formatting style is a bug waiting to happen. Anchored, because
# these appear inside longer lines and a loose match would fire on card numbers.
TAX_ID_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^\d{14}$"), "BR"),  # CNPJ, 14 digits
    (re.compile(r"^[ABCDEFGHJNPQRSUVW]\d{7}[0-9A-J]$", re.I), "ES"),  # CIF
    (re.compile(r"^\d{8}[A-Z]$", re.I), "ES"),  # NIF, persona física
    (re.compile(r"^(PT)?[123568]\d{8}$", re.I), "PT"),  # NIF, 9 digits
]

# A language narrows the country, and sometimes the region within it. Regions
# are listed most-likely-first and are only used when nothing stronger exists.
LANGUAGE_COUNTRIES: dict[str, list[str]] = {
    "ca": ["ES", "AD"],
    "es": ["ES", "MX", "AR", "CO", "CL"],
    "pt": ["PT", "BR"],
    "pt-br": ["BR"],
    "pt-pt": ["PT"],
    "gl": ["ES"],
    "eu": ["ES"],
    "fr": ["FR", "BE", "CH"],
    "it": ["IT"],
    "de": ["DE", "AT", "CH"],
    "nl": ["NL", "BE"],
    "en": ["GB", "US", "IE"],
}

# Regions a language points at, when the country is already settled. Catalan is
# spoken across three Spanish regions, so this is a hint, never a fact.
LANGUAGE_REGIONS: dict[tuple[str, str], str] = {
    ("ca", "ES"): "Catalonia",
    ("gl", "ES"): "Galicia",
    ("eu", "ES"): "Basque Country",
}

# Currency alone cannot identify a country, but it can eliminate one. This is
# what separates Portugal from Brazil when the text is simply "Portuguese".
CURRENCY_COUNTRIES: dict[str, set[str]] = {
    "EUR": {"ES", "PT", "FR", "IT", "DE", "NL", "BE", "AT", "IE", "AD"},
    "BRL": {"BR"},
    "GBP": {"GB"},
    "USD": {"US"},
    "CHF": {"CH"},
    "MXN": {"MX"},
    "ARS": {"AR"},
}

COUNTRY_NAMES: dict[str, str] = {
    "ES": "Spain",
    "PT": "Portugal",
    "BR": "Brazil",
    "FR": "France",
    "IT": "Italy",
    "DE": "Germany",
    "GB": "United Kingdom",
    "NL": "Netherlands",
    "BE": "Belgium",
    "US": "United States",
}


@dataclass(frozen=True)
class LocationInference:
    """What we concluded, and what it rests on."""

    country_code: str = ""
    region: str = ""
    city_printed: str = ""
    city_inferred: str = ""
    language: str = ""
    confidence: str = NONE
    evidence: list[str] = field(default_factory=list)

    @property
    def evidence_text(self) -> str:
        return "; ".join(self.evidence)

    @property
    def country_name(self) -> str:
        return COUNTRY_NAMES.get(self.country_code, self.country_code)


def normalise_language(raw: str) -> str:
    """`ca-ES` -> `ca`, `PT_br` -> `pt-br`, `en-GB` -> `en`.

    Regional tags are kept only where the region changes the meaning: `pt-br`
    and `pt-pt` resolve to different countries, while `en-GB` and `en-US` are
    both just `en` for this purpose and the currency separates them.

    An unrecognised language keeps its primary subtag rather than being
    discarded — it still records what was printed, and `infer_location` simply
    finds no country for it.
    """
    tag = str(raw or "").strip().lower().replace("_", "-")
    if not tag:
        return ""
    if tag in LANGUAGE_COUNTRIES:
        return tag
    return tag.split("-")[0]


def parse_rates(raw) -> list[float]:
    """Read tax rates from whatever shape the model returned.

    Accepts numbers, strings like "21%" or "5,5", and mixed lists. Anything
    outside a plausible VAT range is dropped rather than trusted.
    """
    if raw is None:
        return []
    values = raw if isinstance(raw, list | tuple) else [raw]

    rates: list[float] = []
    for value in values:
        if isinstance(value, int | float):
            candidate = float(value)
        else:
            text = str(value).strip().replace("%", "").replace(",", ".")
            try:
                candidate = float(text)
            except ValueError:
                continue
        if 0.0 < candidate <= 30.0:
            rates.append(round(candidate, 1))

    return sorted(set(rates))


def country_from_vat(rates: list[float]) -> tuple[str, str]:
    """Match a tax table against known VAT sets. Returns (country, evidence)."""
    if not rates:
        return "", ""

    observed = frozenset(rates)

    if country := VAT_SIGNATURES.get(observed):
        return country, f"VAT rates {_fmt(rates)} match {country} exactly"

    # A partial table — a receipt with only zero-rated and standard-rated goods
    # shows a subset. Accept only when exactly one country could contain it.
    candidates = {c for sig, c in VAT_SIGNATURES.items() if observed < sig}
    if len(candidates) == 1:
        country = candidates.pop()
        return country, f"VAT rates {_fmt(rates)} are a subset of {country}'s"

    for rate in rates:
        if country := DISTINCTIVE_RATES.get(rate):
            return country, f"VAT rate {_fmt([rate])} is distinctive to {country}"

    return "", ""


def country_from_tax_id(tax_id: str) -> tuple[str, str]:
    candidate = re.sub(r"[^0-9A-Za-z]", "", str(tax_id or ""))
    if not candidate:
        return "", ""
    for pattern, country in TAX_ID_PATTERNS:
        if pattern.match(candidate):
            return country, f"tax id format matches {country}"
    return "", ""


def _fmt(rates: list[float]) -> str:
    return "/".join(f"{r:g}" for r in rates)


def infer_location(
    language: str = "",
    currency: str = "",
    tax_rates=None,
    tax_id: str = "",
    printed_city: str = "",
    model_country: str = "",
    model_city: str = "",
) -> LocationInference:
    """Decide where a receipt came from, in strict order of evidence quality.

    1. A matching VAT signature      -> high
    2. A matching tax id format      -> high
    3. Language narrowed by currency -> medium
    4. Language alone                -> low, region only
    """
    language = normalise_language(language)
    currency = str(currency or "").strip().upper()
    rates = parse_rates(tax_rates)
    printed_city = str(printed_city or "").strip()
    model_country = str(model_country or "").strip().upper()[:2]
    model_city = str(model_city or "").strip()

    evidence: list[str] = []
    country = ""
    confidence = NONE

    vat_country, vat_note = country_from_vat(rates)
    if vat_country:
        country, confidence = vat_country, HIGH
        evidence.append(vat_note)

    if not country:
        id_country, id_note = country_from_tax_id(tax_id)
        if id_country:
            country, confidence = id_country, HIGH
            evidence.append(id_note)

    if not country and language:
        possible = LANGUAGE_COUNTRIES.get(language, [])
        allowed = CURRENCY_COUNTRIES.get(currency)
        narrowed = [c for c in possible if c in allowed] if allowed else possible

        if len(narrowed) == 1:
            country, confidence = narrowed[0], MEDIUM
            evidence.append(f"{language} text with {currency} narrows to {country}")
        elif narrowed:
            country, confidence = narrowed[0], LOW
            evidence.append(f"{language} text suggests {'/'.join(narrowed)}; assumed {narrowed[0]}")

    # The model's own guess is a last resort, and never better than low.
    if not country and model_country:
        country, confidence = model_country, LOW
        evidence.append(f"no hard signal; model suggested {model_country}")

    if language and not any(language in note for note in evidence):
        evidence.append(f"{language} text")
    if currency:
        evidence.append(f"{currency}")

    region = LANGUAGE_REGIONS.get((language, country), "")
    if region:
        evidence.append(f"{language} points to {region}")

    # Contradictions never resolve silently: they downgrade and are recorded.
    contradictions = detect_contradictions(country, currency, rates, language)
    if contradictions:
        evidence.extend(contradictions)
        confidence = LOW if confidence == HIGH else (NONE if confidence == LOW else LOW)

    return LocationInference(
        country_code=country,
        region=region,
        # A city is only ever a fact when it was printed. Everything else is a
        # guess and lives in its own column so analysis can tell them apart.
        city_printed=printed_city,
        city_inferred=model_city if model_city != printed_city else "",
        language=language,
        confidence=confidence,
        evidence=evidence,
    )


def detect_contradictions(
    country: str, currency: str, rates: list[float], language: str
) -> list[str]:
    """Flag signals that disagree, rather than picking a winner quietly."""
    problems: list[str] = []

    if country and currency:
        allowed = CURRENCY_COUNTRIES.get(currency)
        if allowed and country not in allowed:
            problems.append(f"CONTRADICTION: {currency} is not used in {country}")

    if country and rates:
        vat_country, _ = country_from_vat(rates)
        if vat_country and vat_country != country:
            problems.append(
                f"CONTRADICTION: VAT rates {_fmt(rates)} suggest {vat_country}, not {country}"
            )

    if country and language:
        possible = LANGUAGE_COUNTRIES.get(language)
        if possible and country not in possible:
            problems.append(f"note: {language} is unusual for {country}")

    return problems

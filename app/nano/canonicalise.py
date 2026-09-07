"""Mapping receipt line items onto searchable canonical names.

The problem, using real extracted data from a Mercadona receipt:

    item_name              normalized_name          actually
    MADUIXA                maduixa                  strawberries
    PASTANAGA 500 G        pastanaga 500 g          carrots
    GREC NATURAL P-6       grec natural p-6         greek yogurt
    CINTES DE BACON        cintes de bacon          bacon
    MAQUINA VENUS START3   maquina venus start3     a razor, not food

`normalized_name` was only ever `lower(item_name)`, and `spend_by_item` matches
it with `LIKE`, so "how much did we spend on strawberries?" could never match
`maduixa`. For a household mixing Catalan, Spanish, Portuguese and English,
string matching is hopeless — the words share no letters.

So each line is canonicalised to an English term plus a category, in one batched
model call per receipt, and questions are canonicalised the same way before
matching. The categories exist because a supermarket receipt is not all
groceries; a razor should not appear in the food budget.

A deterministic fallback runs whenever the model is unavailable or returns
something unusable, so a canonicalisation failure degrades to today's behaviour
rather than losing the receipt.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from typing import Any

from nano.logging_setup import log_with

logger = logging.getLogger(__name__)

CATEGORIES = [
    "produce",
    "dairy",
    "meat",
    "seafood",
    "bakery",
    "pantry",
    "frozen",
    "drinks",
    "snacks",
    "household",
    "personal_care",
    "other",
]

PROMPT = """You map supermarket receipt line items to canonical English names.

Receipts are printed in Catalan, Spanish, Portuguese or English, heavily abbreviated, and include pack sizes and brand names. For each input line return:

- "canonical": the plain English name of the product, singular, lowercase, no brand, no pack size, no quantity. Examples: "MADUIXA" -> "strawberry", "PASTANAGA 500 G" -> "carrot", "GREC NATURAL P-6" -> "greek yogurt", "CINTES DE BACON" -> "bacon", "LLET SENCERA 1L" -> "milk".
- "category": exactly one of {categories}.

Return ONLY a JSON array, one object per input line, in the same order, each shaped:
{{"canonical": string, "category": string}}

Input lines:
{lines}"""

# Words that carry no product meaning, stripped by the deterministic fallback.
_NOISE = re.compile(
    r"\b(\d+([.,]\d+)?\s*(g|kg|ml|l|cl|u|ud|uds|un|pack|p)?|p-\d+|\d+x\d+)\b",
    re.IGNORECASE,
)


def strip_accents(value: str) -> str:
    """`maduixa` and `maduíxa` must not be different products."""
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def fallback_normalise(name: str) -> str:
    """Deterministic best effort: lowercase, de-accented, size words removed.

    This is what `normalized_name` should always have been. It does not solve
    cross-language matching — nothing deterministic can — but it is stable, free,
    and strictly better than `lower()`.
    """
    cleaned = strip_accents(str(name or "")).lower()
    cleaned = _NOISE.sub(" ", cleaned)
    cleaned = re.sub(r"[^a-z0-9\s]", " ", cleaned)
    return " ".join(cleaned.split()).strip()


def _parse_response(text: str, expected: int) -> list[dict[str, str]] | None:
    """Read the model's JSON array, rejecting anything that does not line up."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = "\n".join(
            line for line in raw.splitlines() if not line.strip().startswith("```")
        ).strip()

    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None

    if not isinstance(parsed, list) or len(parsed) != expected:
        return None

    result: list[dict[str, str]] = []
    for entry in parsed:
        if not isinstance(entry, dict):
            return None
        canonical = str(entry.get("canonical", "")).strip().lower()
        category = str(entry.get("category", "")).strip().lower()
        result.append(
            {
                "canonical": canonical,
                "category": category if category in CATEGORIES else "other",
            }
        )
    return result


def canonicalise_items(names: list[str], model: Any = None) -> list[dict[str, str]]:
    """Map receipt lines to {'canonical', 'category'}, one call for the lot.

    Always returns one entry per input, in order. Falls back to deterministic
    normalisation for the whole batch if the model call fails or returns
    something that does not line up with the input.
    """
    cleaned = [str(n or "").strip() for n in names]
    if not cleaned:
        return []

    fallback = [
        {"canonical": fallback_normalise(name) or name.lower(), "category": "other"}
        for name in cleaned
    ]

    if model is None:
        from nano.gemini import extraction_model

        try:
            model = extraction_model()
        except Exception as exc:
            log_with(logger, logging.WARNING, "no model for canonicalisation", error=str(exc))
            return fallback

    numbered = "\n".join(f"{i + 1}. {name}" for i, name in enumerate(cleaned))
    prompt = PROMPT.format(categories=", ".join(CATEGORIES), lines=numbered)

    try:
        response = model.generate_content(prompt)
        text = getattr(response, "text", "") or ""
    except Exception as exc:
        log_with(
            logger,
            logging.WARNING,
            "canonicalisation call failed; using deterministic fallback",
            error=str(exc),
            items=len(cleaned),
        )
        return fallback

    parsed = _parse_response(text, len(cleaned))
    if parsed is None:
        log_with(
            logger,
            logging.WARNING,
            "canonicalisation response unusable; using deterministic fallback",
            items=len(cleaned),
        )
        return fallback

    # An empty canonical is worse than the deterministic guess.
    for index, entry in enumerate(parsed):
        if not entry["canonical"]:
            entry["canonical"] = fallback[index]["canonical"]

    log_with(logger, logging.INFO, "canonicalised receipt items", items=len(parsed))
    return parsed


def canonicalise_query(item_name: str, model: Any = None) -> str:
    """Canonicalise a term from a question, so it matches what was stored.

    "how much on milk?" has to reach rows stored as `llet` and `leite`, which
    only works if both sides go through the same mapping.
    """
    term = str(item_name or "").strip()
    if not term:
        return ""
    result = canonicalise_items([term], model=model)
    return result[0]["canonical"] if result else fallback_normalise(term)

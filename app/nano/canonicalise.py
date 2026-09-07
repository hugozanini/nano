"""Mapping receipt line items onto searchable canonical names.

The problem, in the shape it takes on a Catalan supermarket receipt:

    item_name              normalized_name          actually
    POMA GOLDEN 1KG        poma golden 1kg          apples
    ARROS RODO 1KG         arros rodo 1kg           rice
    IOGURT NATURAL P-4     iogurt natural p-4       yogurt
    OUS M-12               ous m-12                 eggs
    PILES AAA P-4          piles aaa p-4            batteries, not food

`normalized_name` was only ever `lower(item_name)`, and `spend_by_item` matches
it with `LIKE`, so "how much did we spend on apples?" could never match
`poma golden 1kg`. For a household mixing Catalan, Spanish, Portuguese and
English, string matching is hopeless — the words often share no letters.

So each line is canonicalised to an English term plus a category, in one batched
model call per receipt, and questions are canonicalised the same way before
matching. The categories exist because a supermarket receipt is not all
groceries; batteries should not appear in the food budget.

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

# Worked examples, kept as data rather than embedded in the prompt text.
#
# They live here because a find-and-replace over prompt *prose* once rewrote the
# left-hand side of an example and left the right-hand side alone, producing the
# instruction `"OUS M-12" -> "bacon"`. The model obeyed it faithfully, and at
# temperature 0 it obeyed it every single time. A corrupted example is not a
# typo; it is a false instruction. `test_canonicalise.py` asserts this table
# against known-correct translations, so the same mistake fails a test instead
# of quietly poisoning the data.
TRANSLATION_EXAMPLES: dict[str, str] = {
    "POMA GOLDEN 1KG": "apple",
    "ARROS RODO 1KG": "rice",
    "LLET SENCERA 1L": "milk",
    "OUS M-12": "egg",
    "PA INTEGRAL": "bread",
    "IOGURT NATURAL P-4": "yogurt",
    "PILES AAA P-4": "battery",
}

PROMPT = """You translate supermarket receipt line items into canonical English product names.

Receipts are printed in Catalan, Spanish, Portuguese or English, heavily abbreviated, with pack sizes, counts and codes appended.

Follow this method literally:
1. Find the product word or words in the line, ignoring trailing size and pack codes such as "1KG", "P-4", "M-12" or "500 G".
2. Translate only those words into English.
3. Return the singular, lowercase English product name, with no brand, no pack size and no quantity.

Do not infer a product from what commonly appears on a supermarket receipt. If the words say eggs, the answer is egg, however unfamiliar the rest of the line looks. Translating the words is the whole task.

Worked examples:
{examples}

For each input line return:
- "canonical": as described above.
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
    """`dolc` and `dolç` must not be different products."""
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
    examples = "\n".join(
        f'  "{source}" -> "{target}"' for source, target in TRANSLATION_EXAMPLES.items()
    )
    prompt = PROMPT.format(categories=", ".join(CATEGORIES), examples=examples, lines=numbered)

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

"""Gemini: intent classification and receipt extraction.

Moved from the original single module with behaviour preserved, apart from two
crash guards that were unconditional bugs:

- ``candidates[0].content.parts[0]`` raised IndexError whenever the model
  returned no candidates, which happens on a safety block. That propagated out
  of the request handler.
- ``response.text`` raises when a response carries no text part.

Both now degrade to a sensible default instead of failing the request.

The JSON-mode extraction, forced function calling and item canonicalisation
planned for this layer land in the data-path and product PRs, together with the
migration off the deprecated ``vertexai.generative_models`` SDK.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Any

from nano.clients import vertex_initialised
from nano.config import get_settings
from nano.logging_setup import log_with

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are Nano, a grocery list assistant for a couple. "
    "Classify the user's message into exactly one of the available functions.\n"
    "The recent conversation and the current list are given for context: use them "
    "to resolve short follow-ups and corrections. 'no, the decaf one' after adding "
    "coffee means remove_items ['coffee'] and add_items ['decaf coffee']; 'remove "
    "that' refers to whatever was just added; 'undo' reverses the last change.\n"
    "If the message is a greeting, a question about your abilities, or makes no "
    "sense as a grocery request, call help. If it is a request to see the list, "
    "call get_list."
)

RECEIPT_PROMPT = """Extract structured data from this receipt image. Return ONLY valid JSON, no markdown fences, no explanation, matching exactly this shape:
{
  "store_name": string, exactly as printed,
  "purchase_date": string in YYYY-MM-DD format (use today's date if not visible),
  "currency": string, 3-letter code, guess from context if not shown,
  "total_amount": number,
  "receipt_language": string, BCP-47 tag for the language the receipt is PRINTED in, e.g. "ca", "es", "pt-PT", "pt-BR", "en",
  "store_address": string, the address block exactly as printed, or "" if absent,
  "store_city": string, ONLY if a city or town name is actually printed on the receipt. Do NOT infer it from the language. Use "" when it is not printed,
  "store_postcode": string as printed, or "",
  "tax_id": string, the business tax identifier as printed (CIF, NIF, CNPJ, VAT number), or "",
  "tax_rates": array of numbers, every tax percentage in the tax/VAT summary table, e.g. [4, 10, 21]. Use [] if there is no tax table,
  "country_guess": string, 2-letter ISO country code you believe this receipt is from,
  "country_evidence": string, one short sentence saying what that belief is based on,
  "items": [
    {"item_name": string exactly as printed, "quantity": number, "unit_price": number, "line_total": number}
  ]
}

Two rules matter more than the rest:
- "store_city" must come from the receipt itself. If the city is not printed, return "". A guess based on the language belongs in "country_guess" reasoning, never here.
- "tax_rates" should list the percentages from the tax summary (often labelled IVA, VAT, TVA or MWST), not the amounts.

Make your best reasonable estimate for any other field that is not clearly visible rather than omitting it."""


def _function_declarations():
    from vertexai.generative_models import FunctionDeclaration

    return [
        FunctionDeclaration(
            name="add_items",
            description="Add one or more grocery items to the shared shopping list.",
            parameters={
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Grocery items to add, e.g. ['coffee', 'tomatoes']",
                    }
                },
                "required": ["items"],
            },
        ),
        FunctionDeclaration(
            name="remove_items",
            description="Remove one or more items from the shared shopping list.",
            parameters={
                "type": "object",
                "properties": {"items": {"type": "array", "items": {"type": "string"}}},
                "required": ["items"],
            },
        ),
        FunctionDeclaration(
            name="get_list",
            description=(
                "Return the current items on the shared shopping list. Use this for "
                "anything unrelated to adding, removing, closing the list, or asking "
                "about past purchases/spending."
            ),
            parameters={"type": "object", "properties": {}},
        ),
        FunctionDeclaration(
            name="close_list",
            description=(
                "Clear the entire shopping list, typically said right after a shopping "
                "trip is finished."
            ),
            parameters={"type": "object", "properties": {}},
        ),
        FunctionDeclaration(
            name="undo",
            description=(
                "Reverse the most recent change to the list, for messages like "
                "'undo', 'no wait', 'put that back' or 'I didn't mean that'."
            ),
            parameters={"type": "object", "properties": {}},
        ),
        FunctionDeclaration(
            name="help",
            description=(
                "Explain what this assistant can do. Use for greetings, 'help', "
                "'what can you do', or a message that makes no sense as a "
                "grocery request."
            ),
            parameters={"type": "object", "properties": {}},
        ),
        FunctionDeclaration(
            name="query_history",
            description=(
                "Answer a question about past purchases or spending history, based on "
                "logged receipts."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query_type": {
                        "type": "string",
                        "enum": [
                            "total_spend",
                            "last_purchase",
                            "spend_by_item",
                            "items_in_last_purchase",
                        ],
                        "description": (
                            "total_spend: how much was spent in a time period. "
                            "last_purchase: when a specific item was last bought. "
                            "spend_by_item: total ever spent on a specific item. "
                            "items_in_last_purchase: what was bought on the most "
                            "recent shopping trip, e.g. 'what did we buy?'."
                        ),
                    },
                    "period": {
                        "type": "string",
                        "enum": ["today", "this_week", "this_month", "last_month", "all_time"],
                        "description": (
                            "Time period, only relevant for total_spend. Defaults to this_month."
                        ),
                    },
                    "item_name": {
                        "type": "string",
                        "description": "Required for last_purchase and spend_by_item.",
                    },
                },
                "required": ["query_type"],
            },
        ),
    ]


def _deterministic_config():
    """Temperature 0 for every call Nano makes.

    Intent classification, receipt extraction and item canonicalisation are all
    extraction tasks with a right answer — sampling buys nothing and costs
    consistency. Measured on a six-item Catalan basket, the default temperature
    canonicalised `OUS M-12` (eggs) as "bacon" on one run in three while getting
    it right on the other two. The same item mapping differently across receipts
    would quietly undermine every GROUP BY built on `normalized_name`.
    """
    from vertexai.generative_models import GenerationConfig

    return GenerationConfig(temperature=0.0, top_p=1.0)


@lru_cache(maxsize=1)
def classifier_model():
    from vertexai.generative_models import GenerativeModel, Tool

    vertex_initialised()
    tools = Tool(function_declarations=_function_declarations())
    return GenerativeModel(
        get_settings().gemini_model,
        tools=[tools],
        generation_config=_deterministic_config(),
    )


@lru_cache(maxsize=1)
def extraction_model():
    from vertexai.generative_models import GenerativeModel

    vertex_initialised()
    return GenerativeModel(
        get_settings().gemini_model,
        generation_config=_deterministic_config(),
    )


def reset_models() -> None:
    """Test hook; tolerates a monkeypatched getter."""
    for fn in (classifier_model, extraction_model):
        clear = getattr(fn, "cache_clear", None)
        if clear is not None:
            clear()


def _first_part(response: Any):
    """Safely reach the first content part, or None.

    A safety block returns a response with no candidates at all; the original
    code indexed straight into it and raised.
    """
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return None
    parts = getattr(getattr(candidates[0], "content", None), "parts", None) or []
    return parts[0] if parts else None


def build_context(turns: list[dict[str, Any]] | None, current_items: list[str] | None) -> str:
    """Render recent exchanges and list state for the classification prompt.

    Without this every message is classified in isolation, so "no, the decaf
    one" and "remove that" have nothing to refer back to.
    """
    sections = []

    if current_items:
        sections.append("Current list: " + ", ".join(current_items))
    else:
        sections.append("Current list: (empty)")

    if turns:
        lines = []
        for turn in turns[-6:]:
            said = str(turn.get("text", "")).strip()
            intent = str(turn.get("intent", "")).strip()
            if said:
                lines.append(f'- they said "{said}" -> {intent or "unknown"}')
        if lines:
            sections.append("Recent messages:\n" + "\n".join(lines))

    return "\n\n".join(sections)


def classify_message(
    text: str,
    model: Any = None,
    turns: list[dict[str, Any]] | None = None,
    current_items: list[str] | None = None,
) -> tuple[str, dict[str, Any]]:
    """Classify a message into (intent, arguments). Falls back to get_list."""
    model = model or classifier_model()
    context = build_context(turns, current_items)
    try:
        chat = model.start_chat()
        response = chat.send_message(f"{SYSTEM_PROMPT}\n\n{context}\n\nMessage: {text}")
    except Exception as exc:
        log_with(logger, logging.ERROR, "gemini classification failed", error=str(exc))
        return "get_list", {}

    part = _first_part(response)
    if part is None:
        logger.warning("gemini returned no usable candidate; defaulting to get_list")
        return "get_list", {}

    call = getattr(part, "function_call", None)
    if call and getattr(call, "name", ""):
        return call.name, dict(call.args or {})

    return "get_list", {}


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if not text.startswith("```"):
        return text
    lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
    return "\n".join(lines).strip()


def extract_receipt(image_bytes: bytes, mime_type: str, model: Any = None) -> dict[str, Any]:
    """Read a receipt photo into structured data. Raises on unusable output."""
    from vertexai.generative_models import Part

    model = model or extraction_model()
    part = Part.from_data(data=image_bytes, mime_type=mime_type)
    response = model.generate_content([RECEIPT_PROMPT, part])

    text_part = _first_part(response)
    if text_part is None:
        raise ValueError("Gemini returned no candidates for the receipt image")

    raw = getattr(text_part, "text", "") or ""
    return json.loads(_strip_code_fence(raw))

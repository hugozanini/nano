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
    "Classify the user's message into exactly one of the available functions. "
    "Only handle grocery-list and purchase-history actions; if the message doesn't "
    "clearly match add_items, remove_items, close_list, or query_history, call get_list."
)

RECEIPT_PROMPT = """Extract structured data from this grocery receipt image. Return ONLY valid JSON, no markdown fences, no explanation, matching exactly this shape:
{
  "store_name": string,
  "purchase_date": string in YYYY-MM-DD format (use today's date if not visible),
  "currency": string, 3-letter code, guess from context if not shown,
  "total_amount": number,
  "items": [
    {"item_name": string, "quantity": number, "unit_price": number, "line_total": number}
  ]
}
Make your best reasonable estimate for any field that isn't clearly visible rather than omitting it."""


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


@lru_cache(maxsize=1)
def classifier_model():
    from vertexai.generative_models import GenerativeModel, Tool

    vertex_initialised()
    tools = Tool(function_declarations=_function_declarations())
    return GenerativeModel(get_settings().gemini_model, tools=[tools])


@lru_cache(maxsize=1)
def extraction_model():
    from vertexai.generative_models import GenerativeModel

    vertex_initialised()
    return GenerativeModel(get_settings().gemini_model)


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


def classify_message(text: str, model: Any = None) -> tuple[str, dict[str, Any]]:
    """Classify a message into (intent, arguments). Falls back to get_list."""
    model = model or classifier_model()
    try:
        chat = model.start_chat()
        response = chat.send_message(f"{SYSTEM_PROMPT}\n\nMessage: {text}")
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

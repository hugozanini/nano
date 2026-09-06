import os
import json
import datetime
import uuid
import requests
from fastapi import FastAPI, Request
from google.cloud import firestore, bigquery
import vertexai
from vertexai.generative_models import GenerativeModel, FunctionDeclaration, Tool, Part

app = FastAPI()

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "changeme")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_ACCESS_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
HOUSEHOLD_NUMBERS = [n.strip() for n in os.environ.get("HOUSEHOLD_NUMBERS", "").split(",") if n.strip()]
PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT")
LOCATION = os.environ.get("VERTEX_LOCATION", "us-central1")

db = firestore.Client()
LIST_DOC = db.collection("lists").document("shared_list")
bq_client = bigquery.Client()

vertexai.init(project=PROJECT_ID, location=LOCATION)

# --- Gemini function declarations ---

add_items_fn = FunctionDeclaration(
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
)

remove_items_fn = FunctionDeclaration(
    name="remove_items",
    description="Remove one or more items from the shared shopping list.",
    parameters={
        "type": "object",
        "properties": {"items": {"type": "array", "items": {"type": "string"}}},
        "required": ["items"],
    },
)

get_list_fn = FunctionDeclaration(
    name="get_list",
    description="Return the current items on the shared shopping list. Use this for anything unrelated to adding, removing, closing the list, or asking about past purchases/spending.",
    parameters={"type": "object", "properties": {}},
)

close_list_fn = FunctionDeclaration(
    name="close_list",
    description="Clear the entire shopping list, typically said right after a shopping trip is finished.",
    parameters={"type": "object", "properties": {}},
)

query_history_fn = FunctionDeclaration(
    name="query_history",
    description="Answer a question about past purchases or spending history, based on logged receipts.",
    parameters={
        "type": "object",
        "properties": {
            "query_type": {
                "type": "string",
                "enum": ["total_spend", "last_purchase", "spend_by_item"],
                "description": (
                    "total_spend: how much was spent in a time period. "
                    "last_purchase: when a specific item was last bought. "
                    "spend_by_item: total ever spent on a specific item."
                ),
            },
            "period": {
                "type": "string",
                "enum": ["today", "this_week", "this_month", "last_month", "all_time"],
                "description": "Time period, only relevant for total_spend. Defaults to this_month.",
            },
            "item_name": {
                "type": "string",
                "description": "Required for last_purchase and spend_by_item.",
            },
        },
        "required": ["query_type"],
    },
)

tools = Tool(
    function_declarations=[
        add_items_fn,
        remove_items_fn,
        get_list_fn,
        close_list_fn,
        query_history_fn,
    ]
)

# Check console.cloud.google.com/vertex-ai/model-garden for the latest available
# Gemini flash model name in your project/region before deploying.
model = GenerativeModel("gemini-2.5-flash", tools=[tools])
extraction_model = GenerativeModel("gemini-2.5-flash")

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


def classify_message(text: str):
    chat = model.start_chat()
    response = chat.send_message(f"{SYSTEM_PROMPT}\n\nMessage: {text}")
    part = response.candidates[0].content.parts[0]
    if part.function_call and part.function_call.name:
        return part.function_call.name, dict(part.function_call.args)
    return "get_list", {}


# --- Firestore helpers ---

def get_current_items():
    doc = LIST_DOC.get()
    return doc.to_dict().get("items", []) if doc.exists else []


def add_items(items):
    current = get_current_items()
    current_lower = [c.lower() for c in current]
    updated = current + [i for i in items if i.lower() not in current_lower]
    LIST_DOC.set({"items": updated, "updated_at": firestore.SERVER_TIMESTAMP})
    return updated


def remove_items(items):
    to_remove = [i.lower() for i in items]
    updated = [c for c in get_current_items() if c.lower() not in to_remove]
    LIST_DOC.set({"items": updated, "updated_at": firestore.SERVER_TIMESTAMP})
    return updated


def close_list():
    LIST_DOC.set({"items": [], "updated_at": firestore.SERVER_TIMESTAMP})
    return []


def clear_matched_items(purchased_item_names):
    current = get_current_items()
    purchased_lower = [n.lower() for n in purchased_item_names]
    remaining = [
        c for c in current
        if not any(p in c.lower() or c.lower() in p for p in purchased_lower)
    ]
    LIST_DOC.set({"items": remaining, "updated_at": firestore.SERVER_TIMESTAMP})
    return remaining


def format_list(items):
    if not items:
        return "The list is empty right now \U0001F389"
    bullets = "\n".join(f"- {i}" for i in items)
    return f"Here's the current list ({len(items)} items):\n{bullets}"


# --- WhatsApp send + media download ---

def send_whatsapp_message(to_number: str, text: str):
    url = f"https://graph.facebook.com/v20.0/{PHONE_NUMBER_ID}/messages"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to_number,
        "type": "text",
        "text": {"body": text},
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=10)
    resp.raise_for_status()


def notify_household(text: str):
    for number in HOUSEHOLD_NUMBERS:
        send_whatsapp_message(number, text)


def download_whatsapp_media(media_id: str):
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    meta_resp = requests.get(f"https://graph.facebook.com/v20.0/{media_id}", headers=headers, timeout=10)
    meta_resp.raise_for_status()
    meta = meta_resp.json()
    media_resp = requests.get(meta["url"], headers=headers, timeout=20)
    media_resp.raise_for_status()
    return media_resp.content, meta.get("mime_type", "image/jpeg")


# --- Phase 2: receipt extraction + Iceberg writes ---

def extract_receipt(image_bytes: bytes, mime_type: str):
    part = Part.from_data(data=image_bytes, mime_type=mime_type)
    response = extraction_model.generate_content([RECEIPT_PROMPT, part])
    text = response.text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    return json.loads(text)


def log_purchase(store_name, purchase_date, currency, total_amount, items):
    purchase_id = str(uuid.uuid4())
    now = datetime.datetime.utcnow().isoformat()
    purchased_at = f"{purchase_date}T00:00:00"

    bq_client.query(
        """
        INSERT INTO `nano.purchases`
        (purchase_id, purchased_at, store_name, total_amount, currency, receipt_image_uri, created_at)
        VALUES (@purchase_id, @purchased_at, @store_name, @total_amount, @currency, @receipt_image_uri, @created_at)
        """,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("purchase_id", "STRING", purchase_id),
                bigquery.ScalarQueryParameter("purchased_at", "TIMESTAMP", purchased_at),
                bigquery.ScalarQueryParameter("store_name", "STRING", store_name),
                bigquery.ScalarQueryParameter("total_amount", "NUMERIC", total_amount),
                bigquery.ScalarQueryParameter("currency", "STRING", currency),
                bigquery.ScalarQueryParameter("receipt_image_uri", "STRING", ""),
                bigquery.ScalarQueryParameter("created_at", "TIMESTAMP", now),
            ]
        ),
    ).result()

    for item in items:
        bq_client.query(
            """
            INSERT INTO `nano.purchase_items`
            (purchase_id, item_name, normalized_name, quantity, unit_price, line_total)
            VALUES (@purchase_id, @item_name, @normalized_name, @quantity, @unit_price, @line_total)
            """,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("purchase_id", "STRING", purchase_id),
                    bigquery.ScalarQueryParameter("item_name", "STRING", item["item_name"]),
                    bigquery.ScalarQueryParameter("normalized_name", "STRING", item["item_name"].lower().strip()),
                    bigquery.ScalarQueryParameter("quantity", "NUMERIC", item.get("quantity", 1)),
                    bigquery.ScalarQueryParameter("unit_price", "NUMERIC", item.get("unit_price", 0)),
                    bigquery.ScalarQueryParameter("line_total", "NUMERIC", item.get("line_total", 0)),
                ]
            ),
        ).result()

    return purchase_id


# --- Phase 3: history queries ---

def period_to_date_range(period: str):
    today = datetime.date.today()
    if period == "today":
        return today, today
    if period == "this_week":
        return today - datetime.timedelta(days=today.weekday()), today
    if period == "last_month":
        first_this_month = today.replace(day=1)
        last_month_end = first_this_month - datetime.timedelta(days=1)
        return last_month_end.replace(day=1), last_month_end
    if period == "all_time":
        return datetime.date(2000, 1, 1), today
    return today.replace(day=1), today  # this_month, default


def answer_history_query(query_type, period="this_month", item_name=None):
    if query_type == "total_spend":
        start, end = period_to_date_range(period)
        rows = list(
            bq_client.query(
                """
                SELECT SUM(total_amount) AS total, COUNT(*) AS trips
                FROM `nano.purchases`
                WHERE DATE(purchased_at) BETWEEN @start AND @end
                """,
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[
                        bigquery.ScalarQueryParameter("start", "DATE", start),
                        bigquery.ScalarQueryParameter("end", "DATE", end),
                    ]
                ),
            ).result()
        )
        total = rows[0].total or 0
        trips = rows[0].trips or 0
        return f"You've spent {total:.2f} across {trips} shopping trip(s) in that period."

    if query_type == "last_purchase" and item_name:
        rows = list(
            bq_client.query(
                """
                SELECT p.purchased_at, p.store_name
                FROM `nano.purchase_items` pi
                JOIN `nano.purchases` p ON p.purchase_id = pi.purchase_id
                WHERE LOWER(pi.normalized_name) LIKE @pattern
                ORDER BY p.purchased_at DESC
                LIMIT 1
                """,
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[
                        bigquery.ScalarQueryParameter("pattern", "STRING", f"%{item_name.lower()}%"),
                    ]
                ),
            ).result()
        )
        if not rows:
            return f"I don't see any past purchases of {item_name}."
        r = rows[0]
        return f"You last bought {item_name} on {r.purchased_at.date()} at {r.store_name}."

    if query_type == "spend_by_item" and item_name:
        rows = list(
            bq_client.query(
                """
                SELECT SUM(line_total) AS total, COUNT(*) AS times
                FROM `nano.purchase_items`
                WHERE LOWER(normalized_name) LIKE @pattern
                """,
                job_config=bigquery.QueryJobConfig(
                    query_parameters=[
                        bigquery.ScalarQueryParameter("pattern", "STRING", f"%{item_name.lower()}%"),
                    ]
                ),
            ).result()
        )
        total = rows[0].total or 0
        times = rows[0].times or 0
        return f"You've spent {total:.2f} on {item_name} across {times} purchase(s)."

    return "I couldn't quite figure out how to answer that — try rephrasing?"


# --- Webhook endpoints ---

@app.get("/webhook")
def verify(request: Request):
    params = request.query_params
    if params.get("hub.verify_token") == VERIFY_TOKEN:
        return int(params.get("hub.challenge"))
    return {"error": "invalid verify token"}


@app.post("/webhook")
async def receive(request: Request):
    body = await request.json()
    try:
        entry = body["entry"][0]["changes"][0]["value"]
        if "messages" not in entry:
            return {"status": "ignored"}
        message = entry["messages"][0]
        sender = message["from"]
    except (KeyError, IndexError):
        return {"status": "ignored"}

    if message["type"] == "image":
        try:
            image_bytes, mime_type = download_whatsapp_media(message["image"]["id"])
            receipt = extract_receipt(image_bytes, mime_type)
            log_purchase(
                receipt["store_name"],
                receipt["purchase_date"],
                receipt["currency"],
                receipt["total_amount"],
                receipt["items"],
            )
            item_names = [i["item_name"] for i in receipt["items"]]
            remaining = clear_matched_items(item_names)
            reply = (
                f"Logged your receipt from {receipt['store_name']} "
                f"({receipt['currency']} {receipt['total_amount']}). {format_list(remaining)}"
            )
        except Exception:
            reply = "I couldn't read that receipt clearly — could you try a clearer photo?"
        notify_household(reply)
        return {"status": "handled", "intent": "log_receipt"}

    if message["type"] != "text":
        send_whatsapp_message(sender, "I can only read text and receipt photos for now.")
        return {"status": "unsupported_type"}

    text = message["text"]["body"]
    intent, args = classify_message(text)

    if intent == "add_items":
        items = add_items(args.get("items", []))
        reply = f"Added! {format_list(items)}"
    elif intent == "remove_items":
        items = remove_items(args.get("items", []))
        reply = f"Removed. {format_list(items)}"
    elif intent == "close_list":
        close_list()
        reply = "List cleared — happy shopping! 🛒"
    elif intent == "query_history":
        reply = answer_history_query(
            args.get("query_type"), args.get("period", "this_month"), args.get("item_name")
        )
    else:
        reply = format_list(get_current_items())

    notify_household(reply)
    return {"status": "handled", "intent": intent}
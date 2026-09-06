import os
import requests
from fastapi import FastAPI, Request
from google.cloud import firestore
import vertexai
from vertexai.generative_models import GenerativeModel, FunctionDeclaration, Tool

app = FastAPI()

VERIFY_TOKEN = os.environ.get("VERIFY_TOKEN", "changeme")
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_ACCESS_TOKEN")
PHONE_NUMBER_ID = os.environ.get("PHONE_NUMBER_ID")
HOUSEHOLD_NUMBERS = [n.strip() for n in os.environ.get("HOUSEHOLD_NUMBERS", "").split(",") if n.strip()]
PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT")
LOCATION = os.environ.get("VERTEX_LOCATION", "us-central1")

db = firestore.Client()
LIST_DOC = db.collection("lists").document("shared_list")

vertexai.init(project=PROJECT_ID, location=LOCATION)

# --- Gemini function declarations: one per intent Nano can act on ---

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
        "properties": {
            "items": {"type": "array", "items": {"type": "string"}}
        },
        "required": ["items"],
    },
)

get_list_fn = FunctionDeclaration(
    name="get_list",
    description="Return the current items on the shared shopping list. Use this for anything unrelated to adding, removing, or closing the list.",
    parameters={"type": "object", "properties": {}},
)

close_list_fn = FunctionDeclaration(
    name="close_list",
    description="Clear the entire shopping list, typically said right after a shopping trip is finished.",
    parameters={"type": "object", "properties": {}},
)

tools = Tool(function_declarations=[add_items_fn, remove_items_fn, get_list_fn, close_list_fn])

# Check console.cloud.google.com/vertex-ai/model-garden for the latest available
# Gemini flash model name in your project/region before deploying.
model = GenerativeModel("gemini-2.5-flash", tools=[tools])

SYSTEM_PROMPT = (
    "You are Nano, a grocery list assistant for a couple. "
    "Classify the user's message into exactly one of the available functions. "
    "Only handle grocery-list actions; if the message doesn't clearly match "
    "add_items, remove_items, or close_list, call get_list."
)


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


def format_list(items):
    if not items:
        return "The list is empty right now \U0001F389"
    bullets = "\n".join(f"- {i}" for i in items)
    return f"Here's the current list ({len(items)} items):\n{bullets}"


# --- WhatsApp send ---

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
    # Fan-out: everyone in the household sees the same update, regardless of
    # who triggered it. This is what makes two 1:1 chats feel like one shared bot.
    for number in HOUSEHOLD_NUMBERS:
        send_whatsapp_message(number, text)


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
            return {"status": "ignored"}  # delivery/read status update, not a message
        message = entry["messages"][0]
        sender = message["from"]
        if message["type"] != "text":
            send_whatsapp_message(sender, "I can only read text messages for now \u2014 photos are coming soon!")
            return {"status": "unsupported_type"}
        text = message["text"]["body"]
    except (KeyError, IndexError):
        return {"status": "ignored"}

    intent, args = classify_message(text)

    if intent == "add_items":
        items = add_items(args.get("items", []))
        reply = f"Added! {format_list(items)}"
    elif intent == "remove_items":
        items = remove_items(args.get("items", []))
        reply = f"Removed. {format_list(items)}"
    elif intent == "close_list":
        close_list()
        reply = "List cleared \u2014 happy shopping! \U0001F6D2"
    else:
        reply = format_list(get_current_items())

    notify_household(reply)
    return {"status": "handled", "intent": intent}
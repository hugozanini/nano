#!/usr/bin/env python3
"""Drive a correctly-signed WhatsApp webhook at a local server.

Exercises the real code path — signature check, allowlist, dedupe, dispatch,
processing — with no phone, no Meta, and no cost. This is the fastest way to
see a change work end to end.

    # terminal 1
    cd app && TASK_BACKEND=inline \
      VERIFY_TOKEN=local PHONE_NUMBER_ID=000000000000000 \
      HOUSEHOLD_MEMBERS="15550100001:you" WHATSAPP_APP_SECRET=local-secret \
      ../.venv/bin/uvicorn main:app --reload --port 8080

    # terminal 2
    python scripts/replay_webhook.py text "add coffee"
    python scripts/replay_webhook.py duplicate   # same id twice
    python scripts/replay_webhook.py forged      # bad signature -> 403
    python scripts/replay_webhook.py stranger    # unknown sender -> ignored
    python scripts/replay_webhook.py status      # delivery callback -> ignored

Note that outbound WhatsApp calls will fail against a fake token; that is
expected and, by design, must not stop the webhook returning 200.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import sys
import time
import urllib.error
import urllib.request

DEFAULT_URL = "http://127.0.0.1:8080/webhook"
DEFAULT_SECRET = "local-secret"
DEFAULT_SENDER = "15550100001"
STRANGER = "15550100999"


def envelope(value: dict) -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "0", "changes": [{"field": "messages", "value": value}]}],
    }


def text_payload(body: str, sender: str, message_id: str) -> dict:
    return envelope(
        {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": "15550100000", "phone_number_id": "0" * 15},
            "contacts": [{"profile": {"name": "Local"}, "wa_id": sender}],
            "messages": [
                {
                    "from": sender,
                    "id": message_id,
                    "timestamp": str(int(time.time())),
                    "type": "text",
                    "text": {"body": body},
                }
            ],
        }
    )


def status_payload(message_id: str) -> dict:
    return envelope(
        {
            "messaging_product": "whatsapp",
            "metadata": {"display_phone_number": "15550100000", "phone_number_id": "0" * 15},
            "statuses": [
                {"id": message_id, "status": "delivered", "timestamp": str(int(time.time()))}
            ],
        }
    )


def post(url: str, payload: dict, secret: str, valid_signature: bool = True) -> tuple[int, str]:
    body = json.dumps(payload).encode()
    digest = hmac.new(
        (secret if valid_signature else "wrong-secret").encode(), body, hashlib.sha256
    ).hexdigest()
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": f"sha256={digest}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()
    except urllib.error.URLError as exc:
        print(f"could not reach {url}: {exc.reason}", file=sys.stderr)
        print("is the local server running? see the docstring at the top.", file=sys.stderr)
        raise SystemExit(1) from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "scenario",
        choices=["text", "duplicate", "forged", "stranger", "status"],
        help="what to send",
    )
    parser.add_argument("message", nargs="?", default="add coffee")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--secret", default=DEFAULT_SECRET)
    args = parser.parse_args()

    message_id = f"wamid.LOCAL{int(time.time() * 1000)}"

    if args.scenario == "text":
        status, body = post(
            args.url, text_payload(args.message, DEFAULT_SENDER, message_id), args.secret
        )
        print(f"HTTP {status}  {body}")

    elif args.scenario == "duplicate":
        payload = text_payload(args.message, DEFAULT_SENDER, message_id)
        for attempt in (1, 2):
            status, body = post(args.url, payload, args.secret)
            print(f"delivery {attempt}: HTTP {status}  {body}")
        print("\nthe second should be accepted but not acted on; check the server log")

    elif args.scenario == "forged":
        status, body = post(
            args.url,
            text_payload(args.message, DEFAULT_SENDER, message_id),
            args.secret,
            valid_signature=False,
        )
        print(f"HTTP {status}  {body}")
        print("expected 403 when REQUIRE_SIGNATURE is on")

    elif args.scenario == "stranger":
        status, body = post(args.url, text_payload(args.message, STRANGER, message_id), args.secret)
        print(f"HTTP {status}  {body}")
        print("expected 200 with status=ignored, and no reply sent")

    elif args.scenario == "status":
        status, body = post(args.url, status_payload(message_id), args.secret)
        print(f"HTTP {status}  {body}")
        print("expected 200 with status=ignored — these are most of the real traffic")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

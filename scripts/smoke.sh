#!/usr/bin/env bash
# Smoke test against the deployed Cloud Run service.
#
# The third tier from the testing table: unit tests prove the logic, integration
# tests prove it against real GCP, and this proves the thing that is actually
# running answers correctly. Deploys fail in ways no test tier can see — a
# missing env var, a secret that did not mount, a revision serving no traffic —
# and all of those look fine right up until a message arrives.
#
# Safe to run against production. It writes nothing: the one POST it sends is
# signed correctly but comes from a number in the +1 555 01xx block reserved for
# fiction, which is not in the household, so the service records it as ignored
# before touching Firestore, BigQuery or WhatsApp.
#
# Secrets are read into variables and never printed. The verify token does reach
# Cloud Run's request log as a query parameter, because that is how Meta's
# handshake is specified — worth knowing, not something this script can avoid.
#
# Usage:  scripts/smoke.sh [--project ID] [--region REGION] [--service NAME]
set -euo pipefail

PROJECT="${NANO_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${NANO_REGION:-us-central1}"
SERVICE="${NANO_SERVICE:-nano-webhook}"

while [ $# -gt 0 ]; do
  case "$1" in
    --project) PROJECT="$2"; shift 2 ;;
    --region)  REGION="$2";  shift 2 ;;
    --service) SERVICE="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

[ -n "$PROJECT" ] || { echo "no project set; pass --project or set NANO_PROJECT" >&2; exit 2; }

pass=0
fail=0

# `set -e` plus a mistake in this script must not look like success. Without
# this, an aborted run prints checks, no summary, and exits 1 -- which is
# exactly how the first version of the WhatsApp check failed.
trap 'status=$?; if [ "$status" -ne 0 ] && [ "$fail" -eq 0 ]; then
  echo; echo "ABORTED — the smoke script itself failed (exit $status), checks are incomplete"; fi' EXIT

check() { # check <name> <expected> <actual>
  if [ "$2" = "$3" ]; then
    printf '  ok   %-44s %s\n' "$1" "$3"; pass=$((pass + 1))
  else
    printf '  FAIL %-44s expected %s, got %s\n' "$1" "$2" "$3"; fail=$((fail + 1))
  fi
}

echo "Smoke test: $SERVICE in $REGION ($PROJECT)"

# --- the revision that is actually serving --------------------------------
URL=$(gcloud run services describe "$SERVICE" --region="$REGION" --project="$PROJECT" \
        --format='value(status.url)')
READY=$(gcloud run services describe "$SERVICE" --region="$REGION" --project="$PROJECT" \
        --format='value(status.latestReadyRevisionName)')
SERVING=$(gcloud run services describe "$SERVICE" --region="$REGION" --project="$PROJECT" \
        --format='value(status.traffic[0].revisionName)')
echo "  url      ${URL}"
echo "  revision ${SERVING}"
# A revision that is ready but not serving means the deploy half-landed: the new
# code exists and every message still reaches the old one.
check "newest revision is the one serving" "$READY" "$SERVING"

# --- the verification handshake -------------------------------------------
VERIFY_TOKEN=$(gcloud run services describe "$SERVICE" --region="$REGION" --project="$PROJECT" \
  --format='value(spec.template.spec.containers[0].env)' \
  | tr ';' '\n' | grep -i 'VERIFY_TOKEN' | head -1 | sed "s/.*'value': '//;s/'.*//" || true)

if [ -n "$VERIFY_TOKEN" ]; then
  challenge="nano-smoke-$$"
  body=$(curl -sS "${URL}/webhook?hub.mode=subscribe&hub.verify_token=${VERIFY_TOKEN}&hub.challenge=${challenge}")
  # Meta re-runs this whenever the webhook is reconfigured; if it breaks, the
  # subscription cannot be re-established.
  check "verification echoes the challenge" "$challenge" "$body"
else
  echo "  SKIP verification handshake (no VERIFY_TOKEN on the service)"
fi

code=$(curl -sS -o /dev/null -w '%{http_code}' \
  "${URL}/webhook?hub.mode=subscribe&hub.verify_token=definitely-wrong&hub.challenge=x")
check "a wrong verify token is refused" "403" "$code"

# --- signature enforcement -------------------------------------------------
PAYLOAD='{"object":"whatsapp_business_account","entry":[{"id":"0","changes":[{"field":"messages","value":{"messaging_product":"whatsapp","metadata":{"display_phone_number":"15550100000","phone_number_id":"000000000000000"},"messages":[{"from":"15550100099","id":"wamid.SMOKE","timestamp":"0","type":"text","text":{"body":"smoke test"}}]}}]}]}'

code=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "${URL}/webhook" \
  -H 'Content-Type: application/json' --data "$PAYLOAD")
# Unsigned traffic is how a forged message would arrive.
check "an unsigned POST is refused" "403" "$code"

code=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "${URL}/webhook" \
  -H 'Content-Type: application/json' -H 'X-Hub-Signature-256: sha256=deadbeef' --data "$PAYLOAD")
check "a forged signature is refused" "403" "$code"

APP_SECRET=$(gcloud secrets versions access latest --secret=whatsapp-app-secret \
  --project="$PROJECT" 2>/dev/null || echo "")
if [ -n "$APP_SECRET" ]; then
  sig=$(printf '%s' "$PAYLOAD" \
        | openssl dgst -sha256 -hmac "$APP_SECRET" -hex 2>/dev/null \
        | sed 's/^.*= //')
  code=$(curl -sS -o /dev/null -w '%{http_code}' -X POST "${URL}/webhook" \
    -H 'Content-Type: application/json' -H "X-Hub-Signature-256: sha256=${sig}" --data "$PAYLOAD")
  # Correctly signed, but from a number outside the household: accepted at the
  # door and dropped before anything is written.
  check "a correctly signed message is accepted" "200" "$code"
else
  echo "  SKIP signed POST (cannot read the app secret)"
fi

# --- the half that is not ours --------------------------------------------
# Everything above proves our service is healthy. All of it passed, six for
# six, at a moment when Nano had been dead for fifteen hours: Meta had blocked
# API access, so nothing could be sent and no webhook was being delivered. Not
# one check noticed, because not one of them touched WhatsApp.
#
# A smoke test that reports success while the product is unreachable is worse
# than no smoke test, because it also stops anyone looking.
API_VERSION=$(gcloud run services describe "$SERVICE" --region="$REGION" --project="$PROJECT" \
  --format='value(spec.template.spec.containers[0].env)' \
  | tr ';' '\n' | grep -i 'GRAPH_API_VERSION' | head -1 | sed "s/.*'value': '//;s/'.*//" || true)
API_VERSION="${API_VERSION:-v23.0}"

PHONE_NUMBER_ID=$(gcloud run services describe "$SERVICE" --region="$REGION" --project="$PROJECT" \
  --format='value(spec.template.spec.containers[0].env)' \
  | tr ';' '\n' | grep -i 'PHONE_NUMBER_ID' | head -1 | sed "s/.*'value': '//;s/'.*//" || true)

ACCESS_TOKEN=$(gcloud secrets versions access latest --secret=whatsapp-access-token \
  --project="$PROJECT" 2>/dev/null || echo "")

if [ -n "$ACCESS_TOKEN" ] && [ -n "$PHONE_NUMBER_ID" ]; then
  # Reads the number's own record: the cheapest call that proves the token is
  # accepted and the account is not restricted. Nothing is sent.
  meta=$(curl -sS "https://graph.facebook.com/${API_VERSION}/${PHONE_NUMBER_ID}?fields=id&access_token=${ACCESS_TOKEN}" || echo '{"error":{"message":"could not reach graph.facebook.com"}}')
  if printf '%s' "$meta" | grep -q '"error"'; then
    # The type, code and message — never the token, never the number.
    detail=$(printf '%s' "$meta" | python3 -c 'import json,sys;e=json.load(sys.stdin)["error"];print(e.get("type","?"),e.get("code","?"),"-",e.get("message","?"))' 2>/dev/null || echo "unparseable error")
    check "WhatsApp API accepts our credentials" "ok" "$detail"
  else
    check "WhatsApp API accepts our credentials" "ok" "ok"
  fi
else
  echo "  SKIP WhatsApp API check (no access token or phone number id)"
fi

# Informational, never a failure: nobody messaging Nano overnight is normal. It
# earns its place next to the check above — "credentials fine, nothing received
# in two days" and "credentials blocked" are different problems.
last_inbound=$(gcloud logging read \
  "resource.labels.service_name=\"${SERVICE}\" AND jsonPayload.message=\"message handled\"" \
  --project="$PROJECT" --limit=1 --freshness=7d --format='value(timestamp)' 2>/dev/null | head -1)
echo "  info last message handled: ${last_inbound:-none in the last 7 days}"

echo
if [ "$fail" -gt 0 ]; then
  echo "FAILED — ${pass} passed, ${fail} failed"
  exit 1
fi
echo "All ${pass} checks passed."

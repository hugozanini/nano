#!/usr/bin/env bash
# Expire processed-message dedupe records automatically.
#
# Each inbound message writes one small document keyed by its WhatsApp id. They
# are only useful while Meta might still redeliver (hours), so a TTL policy on
# `expires_at` reclaims them instead of growing the collection forever.
set -euo pipefail

PROJECT_ID="${GCP_PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
COLLECTION="${FIRESTORE_PREFIX:-}processed_messages"

gcloud firestore fields ttls update expires_at \
  --collection-group="$COLLECTION" \
  --enable-ttl \
  --project="$PROJECT_ID" \
  --async

echo "TTL policy requested for ${COLLECTION}.expires_at (takes a few minutes to apply)."

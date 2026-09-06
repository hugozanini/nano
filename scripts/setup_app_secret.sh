#!/usr/bin/env bash
# Store the Meta App Secret, which is what makes webhook signature verification
# possible. Until this holds the real value, the service accepts unsigned
# webhooks and logs a CRITICAL line on every request.
#
# Find it at: Meta App Dashboard -> Settings -> Basic -> App Secret ("Show").
#
#   ./scripts/setup_app_secret.sh            # prompts, input hidden
#   APP_SECRET=xxx ./scripts/setup_app_secret.sh
set -euo pipefail

PROJECT_ID="${GCP_PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
PREFIX="${NANO_RESOURCE_PREFIX:-nano}"
RUNTIME_SA="${GCP_RUNTIME_SA:-${PREFIX}-cloudrun-sa@${PROJECT_ID}.iam.gserviceaccount.com}"
SECRET_NAME="whatsapp-app-secret"

if [[ -z "${APP_SECRET:-}" ]]; then
  read -rsp "Meta App Secret: " APP_SECRET
  echo
fi

if [[ -z "$APP_SECRET" || "$APP_SECRET" == REPLACE_ME* ]]; then
  echo "ERROR: refusing to store an empty or placeholder value." >&2
  exit 1
fi

if gcloud secrets describe "$SECRET_NAME" --project="$PROJECT_ID" >/dev/null 2>&1; then
  printf '%s' "$APP_SECRET" | gcloud secrets versions add "$SECRET_NAME" \
    --data-file=- --project="$PROJECT_ID"
else
  printf '%s' "$APP_SECRET" | gcloud secrets create "$SECRET_NAME" \
    --data-file=- --replication-policy=automatic --project="$PROJECT_ID"
fi

gcloud secrets add-iam-policy-binding "$SECRET_NAME" \
  --member="serviceAccount:${RUNTIME_SA}" \
  --role="roles/secretmanager.secretAccessor" --project="$PROJECT_ID" >/dev/null

cat <<'SUMMARY'

Stored. Now turn enforcement on:

  gh variable set REQUIRE_SIGNATURE --body true
  gh workflow run "Deploy to Cloud Run"

Then confirm the startup log reads `signature=enforced`, and send a real
message to check it is accepted.
SUMMARY

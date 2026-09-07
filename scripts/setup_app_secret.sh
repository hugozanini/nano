#!/usr/bin/env bash
# Store the Meta App Secret, which is what makes webhook signature verification
# possible. Until this holds the real value, the service accepts unsigned
# webhooks and logs a CRITICAL line on every request.
#
# Find it at: Meta App Dashboard -> Settings -> Basic -> App Secret ("Show").
#
# Three ways to supply it, in order of preference:
#
#   1. From a file — the safest when you are working through a tool that logs
#      your commands, because the value never appears on a command line:
#
#          ./scripts/setup_app_secret.sh --from-file /tmp/app_secret.txt
#
#      The file is overwritten and deleted afterwards.
#
#   2. Interactively, in a real terminal (input is hidden):
#
#          ./scripts/setup_app_secret.sh
#
#   3. From the environment, if you already have it in a variable:
#
#          APP_SECRET="$(cat somewhere)" ./scripts/setup_app_secret.sh
#
#      Avoid typing the literal value here: it lands in your shell history.
set -euo pipefail

PROJECT_ID="${GCP_PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
PREFIX="${NANO_RESOURCE_PREFIX:-nano}"
RUNTIME_SA="${GCP_RUNTIME_SA:-${PREFIX}-cloudrun-sa@${PROJECT_ID}.iam.gserviceaccount.com}"
SECRET_NAME="whatsapp-app-secret"
SECRET_FILE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --from-file)
      SECRET_FILE="${2:-}"
      [[ -n "$SECRET_FILE" ]] || { echo "ERROR: --from-file needs a path." >&2; exit 1; }
      shift 2
      ;;
    -h|--help)
      sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument $1" >&2
      exit 1
      ;;
  esac
done

if [[ -n "$SECRET_FILE" ]]; then
  [[ -r "$SECRET_FILE" ]] || { echo "ERROR: cannot read $SECRET_FILE" >&2; exit 1; }
  # Strip a trailing newline an editor will have added; a stray \n makes every
  # signature comparison fail, which is a miserable thing to debug.
  APP_SECRET="$(tr -d '\r\n' < "$SECRET_FILE")"
elif [[ -z "${APP_SECRET:-}" ]]; then
  # `read` returns non-zero at EOF, and under `set -e` that exits the script
  # silently — which is exactly what happened when this was first run through a
  # non-interactive shell: it appeared to succeed and stored nothing. Check for
  # a terminal explicitly and say so.
  if [[ ! -t 0 ]]; then
    cat >&2 <<'MSG'
ERROR: no terminal available to prompt for the secret.

This shell has no interactive stdin, so the hidden prompt cannot be used.
Use the file form instead, which also keeps the value out of any command log:

    1. Put the App Secret in a file, with your editor:
           /tmp/app_secret.txt

    2. Run:
           ./scripts/setup_app_secret.sh --from-file /tmp/app_secret.txt

The file is overwritten and deleted afterwards.
MSG
    exit 1
  fi
  read -rsp "Meta App Secret: " APP_SECRET || true
  echo
fi

if [[ -z "${APP_SECRET:-}" || "$APP_SECRET" == REPLACE_ME* ]]; then
  echo "ERROR: refusing to store an empty or placeholder value." >&2
  exit 1
fi

# Meta app secrets are 32 hex characters. Warn rather than refuse, in case the
# format ever changes, but make a paste error obvious.
if [[ ! "$APP_SECRET" =~ ^[0-9a-fA-F]{32}$ ]]; then
  echo "WARNING: that does not look like a Meta App Secret (expected 32 hex characters," >&2
  echo "         got ${#APP_SECRET}). Continuing anyway." >&2
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

if [[ -n "$SECRET_FILE" ]]; then
  # Overwrite before unlinking, so the value is not left in free space.
  dd if=/dev/urandom of="$SECRET_FILE" bs=1 count="${#APP_SECRET}" conv=notrunc 2>/dev/null || true
  rm -f "$SECRET_FILE"
  echo "Removed ${SECRET_FILE}."
fi

cat <<'SUMMARY'

Stored. Now turn enforcement on:

    gh variable set REQUIRE_SIGNATURE --body true
    gh workflow run "Deploy to Cloud Run"

Then confirm the startup log reads `signature=enforced`:

    gcloud run services logs read nano-webhook --region us-central1 --limit=20

and send yourself a real WhatsApp message to check it is still accepted.
SUMMARY

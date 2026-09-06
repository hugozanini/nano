#!/usr/bin/env bash
# Create (or recreate) the nano_test dataset used by the integration test tier.
#
# The test dataset mirrors production's schema exactly but writes to a separate
# warehouse prefix, so integration tests can exercise the real Iceberg write path
# — including read-after-write behaviour, which fakes cannot reproduce — without
# ever touching the household's live purchase history.
#
# Reads configuration from the environment so no project identifiers live in the
# repo. Defaults come from the active gcloud config.
#
#   ./scripts/setup_test_dataset.sh
#   ./scripts/setup_test_dataset.sh --recreate   # drop and rebuild
set -euo pipefail

PROJECT_ID="${GCP_PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
LOCATION="${GCP_LOCATION:-us-central1}"
DATASET="${BQ_TEST_DATASET:-nano_test}"
CONNECTION="${BQ_CONNECTION:-nano_lakehouse_conn}"
BUCKET="${LAKEHOUSE_BUCKET:-}"
WAREHOUSE_PREFIX="${WAREHOUSE_PREFIX:-warehouse_test}"

if [[ -z "$PROJECT_ID" ]]; then
  echo "ERROR: no project. Set GCP_PROJECT_ID or run 'gcloud config set project ...'." >&2
  exit 1
fi

# Derive the lakehouse bucket from the production table if not given explicitly,
# so the script needs no hardcoded bucket name.
if [[ -z "$BUCKET" ]]; then
  BUCKET=$(bq show --format=prettyjson "${PROJECT_ID}:nano.purchases" 2>/dev/null \
    | python3 -c "import json,sys,re; u=json.load(sys.stdin)['biglakeConfiguration']['storageUri']; print(re.match(r'gs://([^/]+)/', u).group(1))" 2>/dev/null || true)
fi
if [[ -z "$BUCKET" ]]; then
  echo "ERROR: could not determine the lakehouse bucket. Set LAKEHOUSE_BUCKET." >&2
  exit 1
fi

WAREHOUSE="gs://${BUCKET}/${WAREHOUSE_PREFIX}"

echo "project    : ${PROJECT_ID}"
echo "dataset    : ${DATASET} (${LOCATION})"
echo "connection : ${CONNECTION}"
echo "warehouse  : ${WAREHOUSE}"
echo

if [[ "${1:-}" == "--recreate" ]]; then
  echo "Dropping ${DATASET} and its warehouse data..."
  bq rm -r -f -d "${PROJECT_ID}:${DATASET}" || true
  gsutil -m rm -r "${WAREHOUSE}" 2>/dev/null || true
fi

echo "Creating dataset..."
bq --location="${LOCATION}" mk --dataset \
  --description "Integration-test mirror of the nano dataset. Safe to drop." \
  "${PROJECT_ID}:${DATASET}" 2>/dev/null || echo "  (already exists)"

render_and_run() {
  sed -e "s|\${DATASET}|${PROJECT_ID}.${DATASET}|g" \
      -e "s|\${WAREHOUSE}|${WAREHOUSE}|g" \
      -e "s|\${CONNECTION}|${CONNECTION}|g" \
      "$1" \
    | grep -v '^CREATE SCHEMA' \
    | grep -v '^OPTIONS (location' \
    | bq query --use_legacy_sql=false --project_id="${PROJECT_ID}"
}

echo "Creating Iceberg tables..."
render_and_run "$(dirname "$0")/../sql/001_create_tables.sql"

echo
echo "Tables in ${DATASET}:"
bq ls "${PROJECT_ID}:${DATASET}"

echo
echo "Done. Run the integration tier with:"
echo "  BQ_DATASET=${DATASET} FIRESTORE_PREFIX=nano_test_ \\"
echo "    .venv/bin/python -m pytest tests/integration -m integration -q"

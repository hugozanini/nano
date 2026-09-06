#!/usr/bin/env bash
# Provision the Cloud Tasks queue and the OIDC invoker identity that lets the
# webhook hand slow work off the request path.
#
# Idempotent: safe to re-run. Reads configuration from the environment so no
# project identifiers live in the repo.
set -euo pipefail

PROJECT_ID="${GCP_PROJECT_ID:-$(gcloud config get-value project 2>/dev/null)}"
PROJECT_NUMBER="${GCP_PROJECT_NUMBER:-$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')}"
REGION="${GCP_LOCATION:-us-central1}"
# Resource names are derived from a single prefix so nothing is hardcoded and
# the script is reusable in another project.
PREFIX="${NANO_RESOURCE_PREFIX:-nano}"
QUEUE="${TASKS_QUEUE:-${PREFIX}-webhook-work}"
SERVICE="${CLOUD_RUN_SERVICE:-${PREFIX}-webhook}"
INVOKER_NAME="${TASKS_INVOKER_NAME:-${PREFIX}-tasks-invoker}"
RUNTIME_SA="${GCP_RUNTIME_SA:-${PREFIX}-cloudrun-sa@${PROJECT_ID}.iam.gserviceaccount.com}"
DEPLOY_SA="${GCP_DEPLOY_SA:-${PREFIX}-deployer@${PROJECT_ID}.iam.gserviceaccount.com}"
INVOKER_SA="${INVOKER_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
TASKS_AGENT="service-${PROJECT_NUMBER}@gcp-sa-cloudtasks.iam.gserviceaccount.com"

echo "project ${PROJECT_ID} (${PROJECT_NUMBER}), region ${REGION}"

gcloud services enable cloudtasks.googleapis.com --project="$PROJECT_ID"

gcloud tasks queues create "$QUEUE" --location="$REGION" --project="$PROJECT_ID" \
  2>/dev/null || echo "queue ${QUEUE} already exists"

gcloud iam service-accounts create "$INVOKER_NAME" \
  --display-name="Cloud Tasks OIDC invoker for the webhook" \
  --project="$PROJECT_ID" 2>/dev/null || echo "invoker service account already exists"

# The runtime service account creates tasks and attaches an OIDC token minted
# for the invoker identity, so it needs both the enqueuer role and actAs.
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${RUNTIME_SA}" --role="roles/cloudtasks.enqueuer" --condition=None >/dev/null

gcloud iam service-accounts add-iam-policy-binding "$INVOKER_SA" \
  --member="serviceAccount:${RUNTIME_SA}" --role="roles/iam.serviceAccountUser" \
  --project="$PROJECT_ID" >/dev/null

# Cloud Tasks itself mints the OIDC token, so its service agent needs to be
# able to create tokens for the invoker identity.
gcloud beta services identity create --service=cloudtasks.googleapis.com \
  --project="$PROJECT_ID" --quiet >/dev/null 2>&1 || true

gcloud iam service-accounts add-iam-policy-binding "$INVOKER_SA" \
  --member="serviceAccount:${TASKS_AGENT}" --role="roles/iam.serviceAccountTokenCreator" \
  --project="$PROJECT_ID" >/dev/null

gcloud run services add-iam-policy-binding "$SERVICE" --region="$REGION" \
  --member="serviceAccount:${INVOKER_SA}" --role="roles/run.invoker" \
  --project="$PROJECT_ID" >/dev/null

gcloud iam service-accounts add-iam-policy-binding "$INVOKER_SA" \
  --member="serviceAccount:${DEPLOY_SA}" --role="roles/iam.serviceAccountUser" \
  --project="$PROJECT_ID" >/dev/null

SERVICE_URL=$(gcloud run services describe "$SERVICE" --region="$REGION" \
  --project="$PROJECT_ID" --format='value(status.url)')

cat <<SUMMARY

Done. Set these repository secrets:

  TASKS_INVOKER_SA  = ${INVOKER_SA}
  TASKS_TARGET_URL  = ${SERVICE_URL}/tasks/process

  gh secret set TASKS_INVOKER_SA --body "${INVOKER_SA}"
  gh secret set TASKS_TARGET_URL --body "${SERVICE_URL}/tasks/process"
SUMMARY

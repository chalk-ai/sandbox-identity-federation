#!/usr/bin/env bash
# Create everything the demo needs in a Google Cloud project you own:
#   - a workload identity pool + OIDC provider that trusts your Chalk API's issuer, limited to one
#     Chalk environment and the three agents' subjects
#   - a BigQuery dataset with four small tables of fictional data
#   - two service accounts, each able to read one table, and the bindings that let exactly one
#     agent act as each of them; the third agent gets no binding at all
#   - a table that every identity in the Chalk environment may read directly
#
# Usage:
#   ./setup_gcp.sh <gcp-project> <chalk-api-server> <release-subject> <support-subject> <scratch-subject>
# The subjects are the v1:env:... strings print_subjects.py prints.
set -euo pipefail

PROJECT=$1
API_SERVER=${2%/}
RELEASE_SUB=$3
SUPPORT_SUB=$4
SCRATCH_SUB=$5
POOL=chalk-sandbox-identity-demo
PROVIDER=chalk
DATASET=sandbox_identity_demo
ENV_ID=$(echo "$RELEASE_SUB" | cut -d: -f3)

ISSUER=$(curl -fsS "$API_SERVER/.well-known/openid-configuration" | python3 -c 'import json,sys; print(json.load(sys.stdin)["issuer"])')
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
POOL_NAME="projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/$POOL"
HERE=$(cd "$(dirname "$0")" && pwd)

gcloud iam workload-identity-pools create "$POOL" --project="$PROJECT" --location=global \
  --display-name="Chalk sandbox identity demo"
gcloud iam workload-identity-pools providers create-oidc "$PROVIDER" --project="$PROJECT" \
  --location=global --workload-identity-pool="$POOL" --issuer-uri="$ISSUER" \
  --attribute-mapping="google.subject=assertion.sub,attribute.environment_id=assertion.environment_id" \
  --attribute-condition="assertion.environment_id == '$ENV_ID' && assertion.sub in ['$RELEASE_SUB', '$SUPPORT_SUB', '$SCRATCH_SUB']"

bq --project_id="$PROJECT" mk --dataset --location=US "$PROJECT:$DATASET"
load() {
  bq --project_id="$PROJECT" load --source_format=CSV --skip_leading_rows=1 --replace \
    "$PROJECT:$DATASET.$1" "$HERE/data/$1.csv" "$2"
}
load release_metrics build_id:STRING,service:STRING,released_at:TIMESTAMP,p95_latency_ms:INT64,error_rate:FLOAT64
load support_tickets ticket_id:STRING,opened_at:TIMESTAMP,category:STRING,priority:STRING,summary:STRING
load customer_payments customer_id:STRING,full_name:STRING,card_last4:STRING,amount_usd:NUMERIC,billing_country:STRING
load service_status component:STRING,status:STRING,updated_at:TIMESTAMP

for SA in release support; do
  gcloud iam service-accounts create "sbx-demo-$SA" --project="$PROJECT" \
    --display-name="Sandbox identity demo: $SA agent"
done
gcloud iam service-accounts add-iam-policy-binding "sbx-demo-release@$PROJECT.iam.gserviceaccount.com" \
  --project="$PROJECT" --role=roles/iam.workloadIdentityUser \
  --member="principal://iam.googleapis.com/$POOL_NAME/subject/$RELEASE_SUB"
gcloud iam service-accounts add-iam-policy-binding "sbx-demo-support@$PROJECT.iam.gserviceaccount.com" \
  --project="$PROJECT" --role=roles/iam.workloadIdentityUser \
  --member="principal://iam.googleapis.com/$POOL_NAME/subject/$SUPPORT_SUB"

bq --project_id="$PROJECT" add-iam-policy-binding --role=roles/bigquery.dataViewer \
  --member="serviceAccount:sbx-demo-release@$PROJECT.iam.gserviceaccount.com" "$PROJECT:$DATASET.release_metrics"
bq --project_id="$PROJECT" add-iam-policy-binding --role=roles/bigquery.dataViewer \
  --member="serviceAccount:sbx-demo-support@$PROJECT.iam.gserviceaccount.com" "$PROJECT:$DATASET.support_tickets"
bq --project_id="$PROJECT" add-iam-policy-binding --role=roles/bigquery.dataViewer \
  --member="principalSet://iam.googleapis.com/$POOL_NAME/attribute.environment_id/$ENV_ID" \
  "$PROJECT:$DATASET.service_status"

echo
echo "Done. Deploy with:"
echo "  ./deploy.py --agents-env agents.env --gcp-project $PROJECT --gcp-project-number $PROJECT_NUMBER"

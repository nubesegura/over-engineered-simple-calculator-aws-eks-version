#!/usr/bin/env bash
# Deploys the calculator release. Run by the deployment build project (DEPLOY_COMMAND=deploy) from the verified
# source archive, with the kubeconfig of the cluster already written by the buildspec. Steps:
#   1. check the inputs and resolve everything else from the deployed stacks (read-only calls);
#   2. apply the namespace, the service accounts, the migration network policy and the migration Job, wait for the
#      Job: a failed migration stops the deployment before the release is touched (requirement 11.4);
#   3. helm upgrade --install --atomic --wait: a failed rollout rolls back to the previous release and the script
#      exits non-zero with the cause (requirement 9.5);
#   4. wait for the rollout of every deployment, wait for the load balancer address, publish its DNS name and
#      canonical hosted zone id as SSM parameters and print a short summary.
#
# Passed by the workflow with StartBuild (non-secret):
#   IMAGE_DIGEST            sha256:<64 hex> of the image just pushed
#   RUN_ID                  short lowercase label (the workflow run number); names the migration Job
#   WEB_ORIGIN              https origin of the page (CORS), parameter WebOrigin
#   COGNITO_USER_POOL_ID    Cognito pool of the webpage repository (GitHub variable)
#   COGNITO_APP_CLIENT_ID   app client of that pool (GitHub variable)
#   WAF_ACL_ARN             optional; required by the chart in prod
#   INGEST_QUEUE_URL        URL of the CSV ingestion queue (output IngestQueueUrl of the storage stack)
#   INGEST_BUCKET           name of the CSV ingestion bucket (output IngestBucketName of the storage stack)
#   ACCESS_LOG_BUCKET       optional; load balancer access logs stay off while it is empty (output AlbLogBucketName)
# From the project environment: ENV_TYPE, CONTEXT, REGION_CODE, CLUSTER_NAME, REPOSITORY_URI, AWS_REGION and the
# tag values TAG_TEAM_OWNER, TAG_PROJECT_NAME, TAG_APP_NAME, TAG_REPO_NAME (tags of the published parameters).
# After the rollout the script publishes the load balancer registration for the shared edge (task 5.4):
#   /<context>/<env>/api-backends/eks/dns-name and .../hosted-zone-id (only this role may write that path).
# Nothing here prints a secret value or a resolved ARN: only names and counts.
set -euo pipefail

NAMESPACE="calculator"
RELEASE="calculator"
HELM_TIMEOUT="10m"
ROLLOUT_TIMEOUT="300s"
ADDRESS_TIMEOUT_SECONDS="${ADDRESS_TIMEOUT_SECONDS:-300}"
ADDRESS_POLL_SECONDS="${ADDRESS_POLL_SECONDS:-10}"
MIGRATE_TIMEOUT_SECONDS="${MIGRATE_TIMEOUT_SECONDS:-960}"
MIGRATE_POLL_SECONDS="${MIGRATE_POLL_SECONDS:-5}"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
chart_dir="${CHART_DIR:-${script_dir}/../chart}"

fail() {
  echo "helm_deploy: $*" >&2
  exit 1
}

require() {
  local name
  for name in "$@"; do
    [ -n "${!name:-}" ] || fail "environment variable ${name} is required"
  done
}

# ---------------------------------------------------------------- 1. inputs
require IMAGE_DIGEST RUN_ID ENV_TYPE CONTEXT REGION_CODE CLUSTER_NAME REPOSITORY_URI WEB_ORIGIN \
  COGNITO_USER_POOL_ID COGNITO_APP_CLIENT_ID INGEST_QUEUE_URL INGEST_BUCKET CONTEXT \
  TAG_TEAM_OWNER TAG_PROJECT_NAME TAG_APP_NAME TAG_REPO_NAME
region="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
[ -n "$region" ] || fail "AWS_REGION is required"

[[ "$IMAGE_DIGEST" =~ ^sha256:[0-9a-f]{64}$ ]] || fail "IMAGE_DIGEST must look like sha256:<64 hex characters>"
[[ "$RUN_ID" =~ ^[a-z0-9]([-a-z0-9]{0,30}[a-z0-9])?$ ]] || fail "RUN_ID must be a short lowercase DNS label"
[[ "$ENV_TYPE" =~ ^(dev|prod)$ ]] || fail "ENV_TYPE must be dev or prod"
[[ "$WEB_ORIGIN" =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?$ ]] || fail "WEB_ORIGIN must be an https origin without a path"
[[ "$COGNITO_USER_POOL_ID" =~ ^[a-z0-9-]+_[A-Za-z0-9]+$ ]] || fail "COGNITO_USER_POOL_ID has an unexpected format"
[[ "$COGNITO_APP_CLIENT_ID" =~ ^[a-z0-9]+$ ]] || fail "COGNITO_APP_CLIENT_ID has an unexpected format"
[[ "$INGEST_QUEUE_URL" =~ ^https://sqs\.[a-z0-9-]+\.amazonaws\.com/[0-9]{12}/[A-Za-z0-9_-]+$ ]] || fail "INGEST_QUEUE_URL has an unexpected format"
[[ "$INGEST_BUCKET" =~ ^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$ ]] || fail "INGEST_BUCKET has an unexpected format"
[ -f "${chart_dir}/Chart.yaml" ] || fail "chart not found in ${chart_dir}"

stack_prefix="stk-${REGION_CODE}-${CONTEXT}-eks"

# stack_output <unit> <output key>: read-only, fails with a clear message when the output is missing
stack_output() {
  local value
  value="$(aws cloudformation describe-stacks --stack-name "${stack_prefix}-$1-${ENV_TYPE}" \
    --query "Stacks[0].Outputs[?OutputKey=='$2'].OutputValue | [0]" --output text)" ||
    fail "cannot read stack ${stack_prefix}-$1-${ENV_TYPE}"
  { [ -n "$value" ] && [ "$value" != "None" ]; } || fail "output $2 of stack $1 is empty"
  echo "$value"
}

proxy_host="$(stack_output database ProxyEndpoint)"
instance_host="$(stack_output database EndpointAddress)"
db_name="$(stack_output database DbName)"
app_secret_arn="$(stack_output database AppSecretArn)"
master_secret_arn="$(stack_output database MasterSecretArn)"
cursor_secret_arn="$(stack_output database CursorSecretArn)"
alerts_topic_arn="$(stack_output security AlertsTopicArn)"
alb_security_group="$(stack_output network AlbSecurityGroupId)"
certificate_arn="$(aws ssm get-parameter --name "/${CONTEXT}/${ENV_TYPE}/api-certificate-arn" \
  --query Parameter.Value --output text)" || fail "cannot read /${CONTEXT}/${ENV_TYPE}/api-certificate-arn"
[ -n "$certificate_arn" ] || fail "the shared certificate parameter is empty"

set_args=(
  --set-string "envType=${ENV_TYPE}"
  --set-string "region=${region}"
  --set-string "image.repository=${REPOSITORY_URI}"
  --set-string "image.digest=${IMAGE_DIGEST}"
  --set-string "app.alertsTopicArn=${alerts_topic_arn}"
  --set-string "app.appSecretArn=${app_secret_arn}"
  --set-string "app.masterSecretArn=${master_secret_arn}"
  --set-string "cursor.secretArn=${cursor_secret_arn}"
  --set-string "app.dbHost=${proxy_host}"
  --set-string "app.dbInstanceHost=${instance_host}"
  --set-string "app.dbName=${db_name}"
  --set-string "app.allowedOrigin=${WEB_ORIGIN}"
  --set-string "ingress.certificateArn=${certificate_arn}"
  --set-string "ingress.securityGroupId=${alb_security_group}"
  --set-string "auth.userPoolId=${COGNITO_USER_POOL_ID}"
  --set-string "auth.appClientId=${COGNITO_APP_CLIENT_ID}"
  --set-string "ingest.queueUrl=${INGEST_QUEUE_URL}"
  --set-string "ingest.bucketName=${INGEST_BUCKET}"
)
[ -z "${WAF_ACL_ARN:-}" ] || set_args+=(--set-string "ingress.wafAclArn=${WAF_ACL_ARN}")
[ -z "${ACCESS_LOG_BUCKET:-}" ] || set_args+=(--set-string "ingress.accessLogs.bucket=${ACCESS_LOG_BUCKET}")

values_args=(-f "${chart_dir}/values.yaml" -f "${chart_dir}/values-${ENV_TYPE}.yaml")
job_name="migrate-${RUN_ID}"

# ---------------------------------------------------------------- 2. namespace, service accounts, migration policy, migration
echo "Applying namespace, service accounts, the migration network policy and the migration job ${job_name}"
helm template "$RELEASE" "$chart_dir" --namespace "$NAMESPACE" "${values_args[@]}" "${set_args[@]}" \
  --set-string "migrate.runId=${RUN_ID}" \
  --show-only templates/namespace.yaml --show-only templates/serviceaccounts.yaml \
  --show-only templates/migrate-networkpolicy.yaml --show-only templates/migrate-job.yaml | kubectl apply -f -

migration_failed() {
  echo "Migration ${job_name} did not succeed: $1" >&2
  kubectl --namespace "$NAMESPACE" logs "job/${job_name}" --tail=200 >&2 || true
  kubectl --namespace "$NAMESPACE" describe "job/${job_name}" >&2 || true
  fail "migration failed, the release was not touched"
}

deadline=$((SECONDS + MIGRATE_TIMEOUT_SECONDS))
while true; do
  state="$(kubectl --namespace "$NAMESPACE" get "job/${job_name}" \
    -o jsonpath='{.status.succeeded}/{.status.failed}')" || migration_failed "cannot read the job"
  succeeded="${state%%/*}"
  failed="${state##*/}"
  [ "${succeeded:-0}" -ge 1 ] 2> /dev/null && break
  [ "${failed:-0}" -ge 1 ] 2> /dev/null && migration_failed "the job failed"
  [ "$SECONDS" -lt "$deadline" ] || migration_failed "timed out after ${MIGRATE_TIMEOUT_SECONDS} seconds"
  sleep "$MIGRATE_POLL_SECONDS"
done
echo "Migration ${job_name} completed"

# ---------------------------------------------------------------- 3. release
echo "Installing release ${RELEASE} (image digest ${IMAGE_DIGEST})"
if ! helm upgrade --install "$RELEASE" "$chart_dir" --namespace "$NAMESPACE" "${values_args[@]}" "${set_args[@]}" \
  --set migrate.enabled=false --atomic --wait --timeout "$HELM_TIMEOUT"; then
  echo "The upgrade failed and was rolled back to the previous release (--atomic). State:" >&2
  helm history "$RELEASE" --namespace "$NAMESPACE" --max 5 >&2 || true
  kubectl --namespace "$NAMESPACE" get pods >&2 || true
  kubectl --namespace "$NAMESPACE" get events --sort-by=.lastTimestamp >&2 || true
  fail "helm upgrade failed; see the output above for the cause"
fi

# ---------------------------------------------------------------- 4. rollouts, address, summary
deployments="$(kubectl --namespace "$NAMESPACE" get deployments -o name)" || fail "cannot list the deployments"
[ -n "$deployments" ] || fail "the release has no deployments"
count=0
for deployment in $deployments; do
  kubectl --namespace "$NAMESPACE" rollout status "$deployment" --timeout "$ROLLOUT_TIMEOUT" ||
    fail "rollout of ${deployment} did not finish"
  count=$((count + 1))
done

# The controller creates the load balancer after the Ingress; wait for its address (it is the registration source)
alb_address=""
deadline=$((SECONDS + ADDRESS_TIMEOUT_SECONDS))
while true; do
  alb_address="$(kubectl --namespace "$NAMESPACE" get ingress calculator-api \
    -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2> /dev/null || true)"
  [ -z "$alb_address" ] || break
  [ "$SECONDS" -lt "$deadline" ] || break
  sleep "$ADDRESS_POLL_SECONDS"
done
[ -n "$alb_address" ] || fail "the ingress has no load balancer address after ${ADDRESS_TIMEOUT_SECONDS} seconds; nothing was registered"
echo "Load balancer address: ${alb_address}"

# ---------------------------------------------------------------- 5. registration in the shared edge
zone_id="$(aws elbv2 describe-load-balancers \
  --query "LoadBalancers[?DNSName=='${alb_address}'].CanonicalHostedZoneId | [0]" --output text)" ||
  fail "cannot read the load balancer"
{ [ -n "$zone_id" ] && [ "$zone_id" != "None" ]; } || fail "no load balancer matches the ingress address"
[[ "$zone_id" =~ ^Z[A-Z0-9]+$ ]] || fail "the canonical hosted zone id has an unexpected format"

registry_path="/${CONTEXT}/${ENV_TYPE}/api-backends/eks"
# publish <leaf> <value> <description>: overwrite the parameter, then tag it (put-parameter cannot tag an overwrite)
publish() {
  aws ssm put-parameter --name "${registry_path}/$1" --value "$2" --type String --overwrite \
    --description "$3" > /dev/null || fail "cannot write ${registry_path}/$1"
  aws ssm add-tags-to-resource --resource-type Parameter --resource-id "${registry_path}/$1" --tags \
    "Key=team-owner,Value=${TAG_TEAM_OWNER}" "Key=project-name,Value=${TAG_PROJECT_NAME}" \
    "Key=app-name,Value=${TAG_APP_NAME}" "Key=repo-name,Value=${TAG_REPO_NAME}" "Key=env-type,Value=${ENV_TYPE}" ||
    fail "cannot tag ${registry_path}/$1"
}
publish dns-name "$alb_address" "DNS name of the EKS API load balancer"
publish hosted-zone-id "$zone_id" "Canonical hosted zone id of the EKS API load balancer"
echo "Registered ${registry_path}/dns-name and ${registry_path}/hosted-zone-id"

echo "Summary: release ${RELEASE} deployed in ${ENV_TYPE}; ${count} deployments rolled out; migration ${job_name} succeeded; image ${IMAGE_DIGEST}"
echo "NEXT STEP: deploy shared-resources again so that Route 53 reads the new API backend registration"

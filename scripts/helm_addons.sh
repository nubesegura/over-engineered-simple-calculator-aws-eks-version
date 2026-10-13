#!/usr/bin/env bash
# Installs the in-cluster controllers with Helm: the AWS Load Balancer Controller first (the Ingress of the
# calculator needs it), then the Cluster Autoscaler. Run by the deployment build project (DEPLOY_COMMAND=addons)
# from the verified source archive, with the kubeconfig of the cluster already written by the buildspec.
#
# Inputs, all from the environment of the build (non-secret):
#   CLUSTER_NAME, VPC_ID, KUBERNETES_VERSION, ENV_TYPE, AWS_REGION (or AWS_DEFAULT_REGION)
#   TAG_TEAM_OWNER, TAG_PROJECT_NAME, TAG_APP_NAME, TAG_REPO_NAME   the mandatory tags for the controller
#   ALLOW_AUTOSCALER_MINOR_SKEW   "true" accepts an autoscaler whose minor differs from the cluster's
#
# Chart versions are pinned together with the SHA-256 of the chart archive: the archive is pulled, checked
# and installed from the local file, so a changed or replaced chart in the repository fails here.
# The *_CHART_SHA256 variables exist only so that tests can use a fake archive; the build never sets them.
#
# Compatibility with the Kubernetes version of the cluster (checked against the upstream releases on 2026-10-08):
#   AWS Load Balancer Controller chart 3.6.0 (app v3.6.0): documented requirement Kubernetes 1.22 or newer,
#     no upper bound; built with the Kubernetes 0.36 client libraries.
#   Cluster Autoscaler chart 9.59.0 with image v1.36.1: the newest published release; v1.37.0 is not
#     published yet. The guard below stops the install on a cluster of another minor unless it is accepted.
set -euo pipefail

LBC_CHART_NAME="aws-load-balancer-controller"
LBC_CHART_REPO="https://aws.github.io/eks-charts"
LBC_CHART_VERSION="3.6.0"
LBC_CHART_SHA256="${LBC_CHART_SHA256:-ee0403fcd713d6f1d28b1ff17d0d6d47e011cc4dd727a0aed0d95ae46897226a}"

CA_CHART_NAME="cluster-autoscaler"
CA_CHART_REPO="https://kubernetes.github.io/autoscaler"
CA_CHART_VERSION="9.59.0"
CA_CHART_SHA256="${CA_CHART_SHA256:-90276dafe65cf5d4328ef8313baf6cfb9d130683e0c9f3c28a03b4d8a9ed8f6e}"

NAMESPACE="kube-system"
HELM_TIMEOUT="10m"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
values_dir="${ADDONS_VALUES_DIR:-${script_dir}/../chart-addons}"

fail() {
  echo "helm_addons: $*" >&2
  exit 1
}

require() {
  local name
  for name in "$@"; do
    [ -n "${!name:-}" ] || fail "environment variable ${name} is required"
  done
}

require CLUSTER_NAME VPC_ID KUBERNETES_VERSION ENV_TYPE TAG_TEAM_OWNER TAG_PROJECT_NAME TAG_APP_NAME TAG_REPO_NAME
region="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
[ -n "$region" ] || fail "AWS_REGION is required"

# "1.37" or "1.37.4" -> "1.37"; "v1.36.1" -> "1.36"
minor_of() {
  local version="${1#v}"
  local major="${version%%.*}"
  local rest="${version#*.}"
  echo "${major}.${rest%%.*}"
}

# The autoscaler image comes from the values file (single source of truth for the tag).
autoscaler_tag="$(sed -n 's/^  tag: *"\{0,1\}\([^" ]*\)"\{0,1\} *$/\1/p' "${values_dir}/cluster-autoscaler.values.yaml" | head -n 1)"
[ -n "$autoscaler_tag" ] || fail "no image tag found in cluster-autoscaler.values.yaml"

install_autoscaler=true
if [ "$(minor_of "$KUBERNETES_VERSION")" != "$(minor_of "$autoscaler_tag")" ]; then
  if [ "${ALLOW_AUTOSCALER_MINOR_SKEW:-false}" = "true" ]; then
    echo "WARNING: Cluster Autoscaler ${autoscaler_tag} runs on Kubernetes ${KUBERNETES_VERSION} (minor skew accepted by the owner)"
  elif [ "$ENV_TYPE" = "prod" ]; then
    # prod must never run without autoscaling by accident (HPA would leave pods Pending): stop before any install
    fail "Cluster Autoscaler ${autoscaler_tag} does not match Kubernetes ${KUBERNETES_VERSION} and the environment is prod; bump the pinned chart and image to the matching release (task 6.2) or set AllowAutoscalerMinorSkew=true after the owner accepts the skew. Nothing was installed"
  else
    install_autoscaler=false
    echo "NOTICE: Cluster Autoscaler ${autoscaler_tag} does not match Kubernetes ${KUBERNETES_VERSION}; skipping it until the matching release is installed (task 6.2) or AllowAutoscalerMinorSkew is true"
  fi
fi

work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT

# pull_chart <name> <repo> <version> <sha256>: downloads the chart archive and checks its SHA-256
pull_chart() {
  local name="$1" repo="$2" version="$3" sha256="$4" archive actual
  helm pull "$name" --repo "$repo" --version "$version" --destination "$work_dir"
  archive="${work_dir}/${name}-${version}.tgz"
  [ -f "$archive" ] || fail "chart archive ${archive} was not created"
  actual="$(sha256sum "$archive" | cut -d ' ' -f 1)"
  [ "$actual" = "$sha256" ] || fail "checksum of ${name} ${version} is ${actual}, expected ${sha256}"
}

lbc_replicas=1
[ "$ENV_TYPE" = "prod" ] && lbc_replicas=2

pull_chart "$LBC_CHART_NAME" "$LBC_CHART_REPO" "$LBC_CHART_VERSION" "$LBC_CHART_SHA256"
helm upgrade --install "$LBC_CHART_NAME" "${work_dir}/${LBC_CHART_NAME}-${LBC_CHART_VERSION}.tgz" \
  --namespace "$NAMESPACE" \
  --atomic --timeout "$HELM_TIMEOUT" \
  --values "${values_dir}/aws-load-balancer-controller.values.yaml" \
  --set "replicaCount=${lbc_replicas}" \
  --set-string "clusterName=${CLUSTER_NAME}" \
  --set-string "region=${region}" \
  --set-string "vpcId=${VPC_ID}" \
  --set-string "defaultTags.team-owner=${TAG_TEAM_OWNER}" \
  --set-string "defaultTags.project-name=${TAG_PROJECT_NAME}" \
  --set-string "defaultTags.app-name=${TAG_APP_NAME}" \
  --set-string "defaultTags.repo-name=${TAG_REPO_NAME}" \
  --set-string "defaultTags.env-type=${ENV_TYPE}"

# Read-only access to nodes and node metrics for the group bound to the bastion access entry. Idempotent;
# skipped with a notice when the cluster does not answer.
if kubectl get --raw=/readyz > /dev/null 2>&1; then
  kubectl apply -f "${values_dir}/operator-rbac.yaml"
else
  echo "NOTICE: cluster not reachable with kubectl; operator-rbac.yaml not applied"
fi

if [ "$install_autoscaler" = "true" ]; then
  pull_chart "$CA_CHART_NAME" "$CA_CHART_REPO" "$CA_CHART_VERSION" "$CA_CHART_SHA256"
  helm upgrade --install "$CA_CHART_NAME" "${work_dir}/${CA_CHART_NAME}-${CA_CHART_VERSION}.tgz"     --namespace "$NAMESPACE"     --atomic --timeout "$HELM_TIMEOUT"     --values "${values_dir}/cluster-autoscaler.values.yaml"     --set-string "autoDiscovery.clusterName=${CLUSTER_NAME}"     --set-string "awsRegion=${region}"
  echo "Summary: installed ${LBC_CHART_NAME} ${LBC_CHART_VERSION}, ${CA_CHART_NAME} ${CA_CHART_VERSION} (image ${autoscaler_tag})"
else
  echo "Summary: installed ${LBC_CHART_NAME} ${LBC_CHART_VERSION}; ${CA_CHART_NAME} PENDING the release matching Kubernetes ${KUBERNETES_VERSION} (task 6.2)"
fi

#!/usr/bin/env bash
# Uninstalls what the deployment build installed in the cluster, in reverse order of the installation. Run by the
# deployment build project (DEPLOY_COMMAND=uninstall) from the verified source archive, with the kubeconfig of the
# cluster already written by the buildspec. The AWS Load Balancer Controller must be running while the Ingress
# is deleted, because it removes the load balancer, target groups and security groups it created; so the
# controllers are removed last. Every step is idempotent: a release or object that is already absent is skipped.
#   1. Ingress objects of the calculator namespace, waiting for their finalizers (the load balancer goes away)
#   2. release "calculator" (namespace calculator), then the namespace itself (created by the deployment, not by Helm)
#   3. TargetGroupBindings that are left (only when the CRD exists)
#   4. releases "cluster-autoscaler" and "aws-load-balancer-controller" in kube-system
# The script does not wait for the load balancer in the AWS API: the destroy command of scripts/cfn_deploy.py does
# that after the build, with the read-only elbv2 and ec2 calls. Prints names only.
#
# Inputs: CLUSTER_NAME (set by the project). Optional: UNINSTALL_TIMEOUT (default 10m).
set -euo pipefail

NAMESPACE="calculator"
RELEASE="calculator"
CONTROLLER_NAMESPACE="kube-system"
CONTROLLERS=("cluster-autoscaler" "aws-load-balancer-controller") # uninstall order
TIMEOUT="${UNINSTALL_TIMEOUT:-10m}"

fail() {
  echo "helm_uninstall: $*" >&2
  exit 1
}

[ -n "${CLUSTER_NAME:-}" ] || fail "environment variable CLUSTER_NAME is required"

# uninstall_release <release> <namespace>: skipped when the release is not installed. A cluster that does not answer
# makes helm list fail, which stops the script (a release is never assumed absent).
uninstall_release() {
  local release="$1" namespace="$2" found
  found="$(helm list --namespace "$namespace" --filter "^${release}\$" --short)" ||
    fail "cannot list the releases of ${namespace}"
  if [ -z "$found" ]; then
    echo "Release ${release} is not installed in ${namespace}: skipped"
    return 0
  fi
  helm uninstall "$release" --namespace "$namespace" --wait --timeout "$TIMEOUT" ||
    fail "cannot uninstall ${release}"
  echo "Release ${release} uninstalled"
}

kubectl get --raw=/readyz > /dev/null 2>&1 || fail "the cluster ${CLUSTER_NAME} does not answer"

# 1. Ingress first: the controller is still running and removes the load balancer when the finalizer is processed
if kubectl get namespace "$NAMESPACE" > /dev/null 2>&1; then
  kubectl --namespace "$NAMESPACE" delete ingress --all --ignore-not-found --wait --timeout "$TIMEOUT" ||
    fail "the ingress objects of ${NAMESPACE} were not deleted"
else
  echo "Namespace ${NAMESPACE} does not exist: no ingress to delete"
fi

# 2. the calculator release and its namespace
uninstall_release "$RELEASE" "$NAMESPACE"
kubectl delete namespace "$NAMESPACE" --ignore-not-found --wait --timeout "$TIMEOUT" ||
  fail "namespace ${NAMESPACE} was not deleted"

# 3. bindings that outlived their ingress would keep target groups alive
if kubectl get crd targetgroupbindings.elbv2.k8s.aws > /dev/null 2>&1; then
  kubectl delete targetgroupbindings.elbv2.k8s.aws --all --all-namespaces --ignore-not-found --wait --timeout "$TIMEOUT" ||
    fail "the target group bindings were not deleted"
fi

# 4. the controllers, last
for controller in "${CONTROLLERS[@]}"; do
  uninstall_release "$controller" "$CONTROLLER_NAMESPACE"
done

echo "Summary: calculator release, namespace and controllers removed from ${CLUSTER_NAME}"

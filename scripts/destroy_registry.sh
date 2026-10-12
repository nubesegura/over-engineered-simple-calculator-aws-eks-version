#!/usr/bin/env bash
# Removes the load balancer registration that scripts/helm_deploy.sh publishes for the shared edge:
#   /<context>/<env>/api-backends/eks/dns-name and /<context>/<env>/api-backends/eks/hosted-zone-id
# A parameter that is already absent is not an error (the destroy can be repeated). Run it before the stacks are
# deleted, once the load balancer is gone (task 7.1 moves it into the uninstall flow of the deployment project).
#
# Environment: ENV_TYPE (dev or prod), CONTEXT (for example oecalc). The AWS region comes from the CLI configuration.
# Needs ssm:DeleteParameter on that path only. Prints names, never values.
set -euo pipefail

fail() {
  echo "destroy_registry: $*" >&2
  exit 1
}

for name in ENV_TYPE CONTEXT; do
  [ -n "${!name:-}" ] || fail "environment variable ${name} is required"
done
[[ "$ENV_TYPE" =~ ^(dev|prod)$ ]] || fail "ENV_TYPE must be dev or prod"
[[ "$CONTEXT" =~ ^[a-z0-9]+$ ]] || fail "CONTEXT has an unexpected format"

registry_path="/${CONTEXT}/${ENV_TYPE}/api-backends/eks"
removed=0
for leaf in dns-name hosted-zone-id; do
  if output="$(aws ssm delete-parameter --name "${registry_path}/${leaf}" 2>&1)"; then
    echo "Removed ${registry_path}/${leaf}"
    removed=$((removed + 1))
  elif [[ "$output" == *ParameterNotFound* ]]; then
    echo "Already absent: ${registry_path}/${leaf}"
  else
    fail "cannot remove ${registry_path}/${leaf}: ${output}"
  fi
done
echo "Registration removed (${removed} of 2 parameters existed). Deploy shared-resources again so Route 53 stops reading it."

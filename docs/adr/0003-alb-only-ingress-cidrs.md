# ADR 0003: API pods accept traffic only from the load balancer subnets

Status: Accepted (2026-10-13)

## Context

The load balancer validates the Cognito token before a request reaches a pod; the pods never verify it. With target type `ip` the load balancer connects from the addresses of its own network interfaces, which no pod or namespace selector can name, so the ingress rule has to be an address range. The first rule admitted the whole VPC range on the pod port. Pod addresses are VPC addresses, so any pod (for example a compromised ingestion worker) could call the API directly with no token.

## Decision

The ingress rule of each HTTP workload admits only `network.albCidrs`: the two public subnets where the load balancer nodes live (slices 1 and 2 of the VPC range: 10.30.1.0/24 and 10.30.2.0/24 in `dev`, 10.31.1.0/24 and 10.31.2.0/24 in `prod`). The values are in `chart/values-<env>.yaml`; a chart test derives them from `parameters/<env>.json` and fails if they drift. The VPC range is still used, but only to exclude it from the HTTPS egress rule. Requirement 3.6 (only the load balancer reaches the pods) now holds at the pod level as well as at the security group level.

## Consequences

- Changing the subnet plan in `templates/network.yaml` requires changing the chart values in the same change.
- A pod in a private subnet can no longer reach another pod's API port; nothing in the design needs it.
- The security group rules (only the load balancer group reaches the pod port) stay as a second layer.

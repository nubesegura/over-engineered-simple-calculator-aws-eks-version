# Architecture decision records

Format: context, decision, consequences. The numbered design decisions D1 to D35 are in [../architecture.md](../architecture.md); the records below explain the ones that need more than a line.

| ADR | Title | Design decision |
|---|---|---|
| [0001](0001-rds-proxy-certificate-trust.md) | Trust the Amazon root certificates for the RDS Proxy | D17, D24 |
| [0002](0002-per-workload-network-policies.md) | One network policy per workload | D8, D25 |
| [0003](0003-alb-only-ingress-cidrs.md) | API pods accept traffic only from the load balancer subnets | D26 |
| [0004](0004-alert-throttling.md) | Throttle error alerts per error type | D27 |
| [0005](0005-ingestion-batching.md) | Save CSV rows in batches | D28 |
| [0006](0006-bounded-waits-and-timeouts.md) | Bounded waits for every dependency | D29 |
| [0007](0007-base-image-and-autoscaler-pinning.md) | Pinned base image, and the autoscaler version rule | D1, D30 |
| [0008](0008-production-path-on-main-with-dry-run.md) | Production path from `main`, with a dry run first | D31 |
| [0009](0009-dependabot-weekly.md) | Weekly Dependabot version updates | D32 |
| [0010](0010-no-security-groups-for-pods.md) | No security groups for pods | D33 |
| [0011](0011-alarm-and-schedule-name-deviation.md) | Alarm and schedule names deviate from the acronym table | D34 |
| [0012](0012-log-level-cap-and-redaction.md) | Library log levels are capped and credentials are redacted | D35 |
| [0013](0013-private-endpoint-codebuild-and-read-only-bastion.md) | Private cluster endpoint, deployment from CodeBuild, read-only bastion | D4, D5, D12 |
| [0014](0014-managed-node-group-and-pod-identity.md) | Managed node group and EKS Pod Identity | D2, D3, D15 |
| [0015](0015-one-image-deployed-by-digest.md) | One image for all workloads, deployed by digest | D6 |
| [0016](0016-newest-kubernetes-version-at-creation.md) | Newest Kubernetes version at creation, standard support only | D1, D22 |
| [0017](0017-scheduled-nat-gateway-in-dev.md) | Scheduled NAT gateway in dev, one per zone in prod | D13 |
| [0018](0018-token-validation-at-the-load-balancer.md) | Token validation at the load balancer, with a separate preflight Ingress | D7 |

Decisions of other repositories that this one depends on: API edge in a shared repository (`over-engineered-simple-calculator-shared-resources`, ADR 0001), Cognito and the web page (`over-engineered-simple-calculator-webpage`).

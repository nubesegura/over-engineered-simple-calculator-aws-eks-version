# ADR 0017: Scheduled NAT gateway in dev, one per zone in prod

Status: Accepted (2026-10-08)

## Context

Private nodes and pods need outbound access to ECR, Secrets Manager, SNS, STS and the Auto Scaling and EC2 APIs. VPC interface endpoints for these services cost more than a NAT gateway; public addresses on nodes are not allowed. A NAT gateway is billed by the hour.

## Decision

In `dev`, a scheduler Lambda outside the VPC (`src/scheduler/`) removes the NAT gateway and the private default route at 22:00 Panama time and creates them again at 08:00, driven by two EventBridge Scheduler schedules. The Elastic IP stays allocated. The function is idempotent, verifies the route after each run, is scoped by tag and ARN to this NAT gateway and route table, and raises an alarm when it fails. The SSM parameter `/oecalc/dev/eks/nat-keep-up` (workflow `Keep NAT`) skips the shutdown on demand. CloudFormation never owns the `dev` NAT gateway. In `prod` there is no schedule: the network stack creates one NAT gateway per Availability Zone with its own route table.

## Consequences

- At night running pods keep answering through the load balancer, but new images, new nodes, secret reads, alert emails, autoscaler actions, bastion sessions and deployments can fail or wait until 08:00. Load tests and deployments are done in the day; the deploy workflow checks the window (`scripts/nat_window.py`).
- The destroy workflow works whether the NAT gateway exists or not, and brings it back only for the uninstall build.
- Cost: about 14 hours of NAT per day are saved in `dev`.

# ADR 0008: Production path from `main`, with a dry run first

Status: Accepted (2026-10-13)

## Context

`prod` uses the same templates and chart as `dev`, but more than thirty of its values differ (Multi-AZ database, two NAT gateways, flow logs, deletion protection, WAF, replicas). They were only checked offline, so the first production deployment would also be the first test of those values. The organization has a single user, so GitHub environments have no required reviewers.

## Decision

- `.github/workflows/deploy-prod.yml` runs on a push to `main` (the owner merges `develop` into `main`) or by hand from `main`; every job and the reusable workflow check the ref. Nothing runs on `develop` or on a pull request.
- Job order: `ci`, then `plan`, then `apply`. `plan` calls the reusable `deploy.yml` with `dry_run: true`: the owner guard, the packaging of the rotation function, the check of the Kubernetes version (`scripts/eks_latest_version.py`) and a change set for every stack from `security` through `storage` with the removal and replacement guard, deleted afterwards and never executed. `apply` needs `plan`, so a failed plan, or a missing GitHub environment variable or secret (checked before AWS is touched), stops everything.
- The `prod` environment needs the variables `AWS_REGION`, `COGNITO_USER_POOL_ID`, `COGNITO_APP_CLIENT_ID`, `WAF_ACL_ARN` and the secrets `ROLE_ARN`, `AWS_ACCOUNT_ID`, `SUPPORT_EMAIL`.
- There is no workflow that destroys `prod`: that is a manual action with a typed confirmation.

## Consequences

- The dry run is the entry gate of production: it finds parameter and template defects in the production account without creating anything. The alarms stack is not part of it, because its dimensions exist only after the release.
- The dry run cannot find everything (for example a service-linked role that is missing); `AutoScalingServiceLinkedRoleExists` and the autoscaler version rule (ADR 0007) are checked by hand.
- Safety rests on the dry run, the guard and the branch rule, not on approvers.

# ADR 0015: One image for all workloads, deployed by digest

Status: Accepted (2026-10-08)

## Context

The workloads share one Python package. Seven images (four operations, history, ingest, migrate) would multiply repositories, scans and builds without separating anything that Pod Identity does not already separate. A mutable tag can be repointed after a review.

## Decision

One image, `ecr-useast2-oecalc-eks-<env>`, started as `calculator <add|sub|mul|div|history|ingest|migrate>`. The ECR repository has immutable tags, scan on push and a lifecycle that keeps the last 10 images. The workflow tags with `<commit>-<run id>`, reads the digest from the registry and compares it with the digest the build reported; the chart accepts only `sha256:<64 hex>` and renders `repository@digest`. The nodes and the deployment role can only pull.

## Consequences

- A change to any workload rebuilds and redeploys the single image; every deployment rolls.
- A vulnerability in the base image affects all workloads (ADR 0007).
- Rollbacks use Helm revisions, which keep the previous digest.

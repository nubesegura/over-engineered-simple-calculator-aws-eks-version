# ADR 0016: Newest Kubernetes version at creation, standard support only

Status: Accepted (2026-10-08, owner decision)

## Context

EKS supports a version for about 14 months in standard support and then moves clusters to paid extended support. A cluster created on an old version would reach the end of support early. The control plane is upgraded by AWS at the end of standard support; the nodes are not.

## Decision

Each time an environment is created it uses the newest Kubernetes version that EKS offers (1.37 in `dev`, supported until 1 December 2027), with the upgrade policy `STANDARD`. `scripts/eks_latest_version.py` compares the parameter with what EKS offers and runs in the production dry run. To avoid being surprised, AWS Health events for EKS go to the alerts topic and a weekly reminder repeats the cluster name, version and end-of-support date ([architecture.md](../architecture.md), D22).

## Consequences

- Add-ons, `kubectl` and the controllers must support the new minor. The Cluster Autoscaler has no matching release yet (ADR 0007).
- The cluster is meant to live for days; the reminder tells the owner to upgrade or destroy it.

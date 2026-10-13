# ADR 0010: No security groups for pods

Status: Accepted (2026-10-13)

## Context

All pods on a node share the node security group, so the rule that opens the database proxy to the node admits every pod of that node (gap G1 of the security design review). EKS can give a pod its own security group ("security groups for pods"), which would close the gap, but it needs pod network interface trunking. The `t4g` instance family used by the node group does not support it.

## Decision

Security groups for pods are not used. The gap is mitigated by the per-workload network policies (ADR 0002), which allow the database port only to the database subnets and only for the workloads that list it, by the limited database user (read and insert on the calculator tables only) and by the rotating credentials registered in the proxy.

## Consequences

- G1 is mitigated, not closed: a compromised pod on a node can still try the proxy port, and the network policy is the only barrier at that level.
- Moving the node group to an instance family with trunking support (and enabling the feature in the VPC CNI) would allow closing it; that is a cost and architecture change for a later spec.

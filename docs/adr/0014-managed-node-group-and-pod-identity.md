# ADR 0014: Managed node group and EKS Pod Identity

Status: Accepted (2026-10-08)

## Context

The goal of the project is to learn EKS, so the owner wants to see and control the nodes. Workloads need AWS permissions without sharing the node role.

## Decision

- A managed node group of `t4g.medium` on-demand arm64 nodes (AL2023), 2 to 4 nodes across two private subnets, 20 GB encrypted gp3 volumes, IMDSv2 required with hop limit 1, no key pair. Two nodes give one node per zone; four is a cost cap. No Auto Mode, no Fargate (a later change to Auto Mode does not require recreating the cluster).
- Pod permissions use EKS Pod Identity, not IRSA: no OIDC provider to create, one fixed trust principal (`pods.eks.amazonaws.com`), and the associations are CloudFormation resources. One service account and one IAM role per workload kind (`calc` for the four operations, `history`, `migrate`, `ingest`, plus the two controllers). The node role holds only what nodes need.
- Pods never use the instance metadata service (hop limit 1), so the controllers get the region and VPC id explicitly.

## Consequences

- The Pod Identity agent add-on is part of the cluster and the pod network policies must allow the agent address (ADR 0002).
- Roles are scoped by resource and tag (D15): the controller and autoscaler policies carry tag conditions; each workload role reads only its own secrets.
- Nodes scale between 2 and 4 with the Cluster Autoscaler, which discovers the group by tag.

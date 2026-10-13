# ADR 0002: One network policy per workload

Status: Accepted (2026-10-13; refines design decision D8 of 2026-10-08)

## Context

The first policy set had a default deny and a few shared allow rules (load balancer to pods, pods to the proxy, DNS, any HTTPS). A shared rule gives every pod the union of what any pod needs: the ingestion worker, which parses uploaded files, could reach everything the API pods could.

## Decision

The chart renders a default deny for the whole namespace and then one `NetworkPolicy` named `<workload>-network` for each of `add`, `sub`, `mul`, `div`, `history`, `ingest` and the `migrate` job, selected by the workload labels. A workload lists in `egress` only the rules it uses; all of them use the same four today:

- `dns`: CoreDNS in `kube-system` on port 53.
- `database`: TCP 5432 to the two private subnets that hold the proxy and the instance (`network.databaseCidrs`).
- `aws`: TCP 443 to any address except the VPC range (AWS APIs through the NAT gateway and the S3 gateway endpoint).
- `podIdentity`: the EKS Pod Identity agent at 169.254.170.23 on port 80, without which a pod gets no AWS credentials.

Ingress exists only for the HTTP workloads (see ADR 0003); the worker and the job accept none. An unknown rule name stops the render.

## Consequences

- A new workload must declare its egress; forgetting it means no traffic, not open traffic.
- The kubelet probes come from the node itself and are not filtered.
- These policies are the main pod-level control: security groups for pods are not available (ADR 0010), and the node security group still admits any pod of the node to the proxy and the database.
- Chart tests render both environments and assert the policies.

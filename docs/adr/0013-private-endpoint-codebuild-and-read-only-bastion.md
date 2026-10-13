# ADR 0013: Private cluster endpoint, deployment from CodeBuild, read-only bastion

Status: Accepted (2026-10-08)

## Context

A public cluster endpoint is reachable from the internet and protected only by IAM. The pipeline still has to run `helm` and `kubectl`, and the owner wants to read pods and logs to learn how the cluster behaves.

## Decision

- The EKS endpoint is private only. Authentication mode is `API` (access entries), with no creator administrator and no `aws-auth` ConfigMap.
- Every `helm` and `kubectl` action of a deployment, an add-on install or an uninstall runs in a CodeBuild project inside the private subnets. The workflow assumes the deployment role by OIDC, uploads the chart and scripts to the SAM artifact bucket, starts the build with the SHA-256 of the archive and waits. The buildspec downloads the archive again, compares the checksum, extracts that verified copy and runs only files from it; `kubectl` and `helm` are installed from pinned versions with checked checksums. The build role is the only cluster administrator.
- A bastion in its own template is reached only through Session Manager (no inbound rule, no public IP, no key pair). Its access entry is read-only (`AmazonEKSViewPolicy`, which has no Secrets, plus a cluster role for nodes); edit rights in the `calculator` namespace need a deliberate parameter. Sessions are logged to an encrypted group. The bastion exists in `dev` only.

## Consequences

- Nothing outside the VPC can talk to the Kubernetes API; people cannot use a console or a laptop against the cluster. Only the bastion works (backlog: console access).
- A principal that can call `codebuild:StartBuild` can still send a buildspec override. The controls are who holds that permission and the CloudTrail record; the fixed buildspec in the project definition does not prevent it.
- Deployments depend on the NAT gateway being present (tool downloads, add-on charts, image pulls).

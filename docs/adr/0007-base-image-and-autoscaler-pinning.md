# ADR 0007: Pinned base image, and the autoscaler version rule

Status: Accepted (2026-10-13)

## Context

The backend image runs every workload. The ECR basic scan of the first deployed image reported operating system findings in glibc, pcre2 and zlib of the Debian base. Separately, the newest published Cluster Autoscaler (v1.36.1) is one minor behind the newest Kubernetes version the cluster uses (1.37), and the autoscaler supports only its own minor.

## Decision

- `src/backend/Dockerfile` takes its base from one build argument that holds the tag and the digest of the multi-architecture index together (`python:3.14-slim-trixie@sha256:...`); `latest` is never used. They are refreshed together, after checking in the Debian security tracker which package versions are fixed, and the result is recorded in the Dockerfile comment. The base of 2026-10-13 carries Python 3.14.8, libpcre2 10.46-1~deb13u3 (fixed), and glibc and zlib for which Debian had published no fix yet. Findings are read by the digest of the arm64 manifest, not by the tag. Dependabot proposes the next refresh (ADR 0009).
- The autoscaler chart and image are pinned (chart by SHA-256, image by tag). When the cluster minor and the autoscaler minor differ, `scripts/helm_addons.sh` installs it only if `AllowAutoscalerMinorSkew=true` (owner decision, `dev`). In `prod` the parameter is `false` and the script stops the deployment with a message instead of skipping the install, so production never runs without autoscaling by accident.

## Consequences

- Remaining base image findings stay until Debian ships fixes; they are re-read after each refresh.
- To run `prod`, either a matching autoscaler release is pinned (chart, image and checksum change together) or the owner accepts the skew in `prod` too.
- Add-on images are pulled by tag from public registries; pinning them by digest is a backlog item.

# ADR 0009: Weekly Dependabot version updates

Status: Accepted (2026-10-13)

## Context

All Python dependencies are pinned with `==`, GitHub Actions by commit SHA and the base image by digest. Exact pins give reproducible builds but nobody notices a newer version. `pip-audit` runs in CI against the runtime requirements, which covers known advisories at build time only.

## Decision

`.github/dependabot.yml` schedules version updates every Monday at 06:00 UTC for three ecosystems: `pip` (the backend, rotation, scheduler and scripts requirement files), `docker` (`src/backend`) and `github-actions` (the workflows). Updates are grouped (minor and patch together for pip; one group each for docker and actions), at most 5 pull requests are open per ecosystem, and the commit prefixes are `deps(pip)`, `deps(docker)` and `deps(actions)`. Security updates are separate and immediate: GitHub raises them from Dependabot alerts whatever the schedule says. Enabling the alerts is a repository setting, owned by the owner.

## Consequences

- Updates arrive as pull requests that go through the same CI as any change; the pin style (SHA, digest, `==`) is kept.
- Not covered by Dependabot, refreshed by hand: the PostgreSQL service image of `ci.yml`, the Helm archive checksum, the add-on chart versions and the autoscaler image.
- A test (`scripts/tests/test_dependabot.py`) keeps the file in line with these rules.

#!/usr/bin/env python3
"""Check that the Kubernetes version of an environment is the newest one in standard support (read-only).

The rule of the project (constitution section 2): a new cluster always takes the newest Kubernetes version that
Amazon EKS offers under standard support. This script asks the service (`aws eks describe-cluster-versions`, a
read-only call), compares the answer with `KubernetesVersion` of parameters/<env>.json (or with --version) and:

* exits 0 when the configured version is the newest in standard support,
* exits 1 when it is not (a newer version exists, or the configured one is out of standard support),
* exits 2 when the service cannot be queried or offers no standard-support version.

It always prints the end-of-standard-support date, so the same output feeds the version reminder (--end-date-only
prints only the date of the configured version, in UTC).

Usage:
    python scripts/eks_latest_version.py --env dev
    python scripts/eks_latest_version.py --version 1.37 --region us-east-2
    python scripts/eks_latest_version.py --env dev --end-date-only
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGION = "us-east-2"
STANDARD = "STANDARD_SUPPORT"

EXIT_OK = 0
EXIT_NOT_NEWEST = 1
EXIT_ERROR = 2


class VersionCheckError(Exception):
    """The versions offered by the service could not be read."""


@dataclass(frozen=True)
class ClusterVersion:
    version: str
    status: str
    end_of_standard_support: str | None

    @property
    def key(self) -> tuple[int, ...]:
        return version_key(self.version)


def version_key(version: str) -> tuple[int, ...]:
    """'1.37' -> (1, 37); raises VersionCheckError for anything else."""
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError as error:
        raise VersionCheckError(f"not a Kubernetes version: {version!r}") from error


def end_date_utc(raw: Any) -> str | None:
    """The date (UTC, YYYY-MM-DD) of an AWS timestamp, which the CLI prints as ISO text or as epoch seconds."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, int | float):
        moment = datetime.fromtimestamp(raw, tz=UTC)
    else:
        try:
            moment = datetime.fromisoformat(str(raw))
        except ValueError as error:
            raise VersionCheckError(f"unreadable end-of-support date: {raw!r}") from error
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).date().isoformat()


def parse_versions(payload: Any) -> list[ClusterVersion]:
    if not isinstance(payload, dict) or not isinstance(payload.get("clusterVersions"), list):
        raise VersionCheckError("unexpected answer of describe-cluster-versions (no clusterVersions list)")
    versions: list[ClusterVersion] = []
    for item in payload["clusterVersions"]:
        status = str(item.get("versionStatus") or item.get("status") or "")
        versions.append(
            ClusterVersion(
                version=str(item["clusterVersion"]),
                status=status,
                end_of_standard_support=end_date_utc(item.get("endOfStandardSupportDate")),
            )
        )
    return versions


def newest_standard(versions: list[ClusterVersion]) -> ClusterVersion:
    standard = [v for v in versions if v.status == STANDARD]
    if not standard:
        raise VersionCheckError("the service offers no Kubernetes version in standard support")
    return max(standard, key=lambda v: v.key)


def describe_cluster_versions(region: str) -> list[ClusterVersion]:
    command = ["aws", "eks", "describe-cluster-versions", "--region", region, "--output", "json"]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)  # noqa: S603
    if completed.returncode != 0:
        raise VersionCheckError(f"aws eks describe-cluster-versions failed: {completed.stderr.strip()[:500]}")
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise VersionCheckError("describe-cluster-versions did not return JSON") from error
    return parse_versions(payload)


def configured_version(env: str) -> str:
    path = REPO_ROOT / "parameters" / f"{env}.json"
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
        return str(config["Parameters"]["KubernetesVersion"])
    except (OSError, KeyError, json.JSONDecodeError) as error:
        raise VersionCheckError(f"{path.name}: cannot read Parameters.KubernetesVersion") from error


def evaluate(configured: str, versions: list[ClusterVersion]) -> tuple[int, list[str], str | None]:
    """The exit code, the report lines and the end-of-standard-support date of the configured version."""
    newest = newest_standard(versions)
    current = next((v for v in versions if v.version == configured), None)
    lines = [
        f"Newest Kubernetes version in standard support: {newest.version}"
        f" (standard support ends {newest.end_of_standard_support or 'unknown'} UTC)"
    ]
    if current is None:
        lines.append(f"Configured version {configured} is not offered by the service.")
        return EXIT_NOT_NEWEST, lines, None
    lines.append(
        f"Configured version {configured}: {current.status or 'unknown status'}"
        f", standard support ends {current.end_of_standard_support or 'unknown'} UTC"
    )
    if current.key == newest.key:
        lines.append("OK: the configured version is the newest in standard support.")
        return EXIT_OK, lines, current.end_of_standard_support
    if current.status != STANDARD:
        lines.append(f"FAIL: {configured} is no longer in standard support; the newest is {newest.version}.")
    else:
        lines.append(f"FAIL: {newest.version} is available; set KubernetesVersion to {newest.version}.")
    return EXIT_NOT_NEWEST, lines, current.end_of_standard_support


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--env", choices=["dev", "prod"], help="read KubernetesVersion from parameters/<env>.json")
    source.add_argument("--version", help="Kubernetes version to check, for example 1.37")
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--end-date-only", action="store_true", help="print only the end date of the configured one")
    args = parser.parse_args(argv)
    try:
        configured = configured_version(args.env) if args.env else str(args.version)
        version_key(configured)
        code, lines, end_date = evaluate(configured, describe_cluster_versions(args.region))
    except VersionCheckError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return EXIT_ERROR
    if args.end_date_only:
        if end_date is None:
            print("ERROR: no end-of-support date for the configured version", file=sys.stderr)
            return EXIT_ERROR
        print(end_date)
        return code
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main())

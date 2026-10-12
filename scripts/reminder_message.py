#!/usr/bin/env python3
"""Build the text of the weekly "cluster still running" reminder (task 5.6, requirements 13.4 and 13.5).

The alarms stack publishes the text every 7 days to the alerts topic (EventBridge Scheduler). The text carries the
cluster name, its Kubernetes version, the date the standard support of that version ends (from
scripts/eks_latest_version.py, a read-only call) and a line about the fixed hourly cost, and is passed to the stack
as the parameter ReminderMessageJson (a JSON string, because the template splices it into the schedule input).

Usage:
    python scripts/reminder_message.py --env dev --cluster-name NAME [--github-env FILE]
        prints the message; --github-env appends CFN_PARAM_ReminderMessageJson=<json string> to that file
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date

import eks_latest_version as versions

# Public price of the EKS control plane in standard support (USD per hour); nodes, NAT gateways, the database and
# the proxy come on top. Informational text only: the figure is not used by any calculation.
CONTROL_PLANE_USD_PER_HOUR = "0.10"
MAX_LENGTH = 1500  # the template parameter allows 2000 characters once JSON-escaped


def days_left(end_date: str, today: date) -> int | None:
    try:
        return (date.fromisoformat(end_date) - today).days
    except ValueError:
        return None


def build_message(env: str, cluster_name: str, version: str, end_date: str | None, today: date | None = None) -> str:
    """The reminder text; the support line says how many days are left, or that the date is unknown."""
    today = today or date.today()
    if end_date:
        left = days_left(end_date, today)
        remaining = "" if left is None else (f" ({left} days left)" if left >= 0 else f" (ended {-left} days ago)")
        support = f"Standard support of Kubernetes {version} ends {end_date} UTC{remaining}."
    else:
        support = (
            f"The end of standard support of Kubernetes {version} is unknown: "
            f"run python scripts/eks_latest_version.py --env {env}."
        )
    lines = [
        f"The EKS cluster {cluster_name} ({env}) is still running.",
        f"Kubernetes version: {version}. {support}",
        (
            f"It costs about USD {CONTROL_PLANE_USD_PER_HOUR} per hour for the control plane alone, plus the nodes, "
            "the NAT gateway, the database and the proxy, whether or not it is used."
        ),
        "Upgrade the Kubernetes version before the date above, or destroy the environment (Destroy DEV).",
    ]
    message = "\n".join(lines)
    if len(message) > MAX_LENGTH:
        raise ValueError(f"the reminder has {len(message)} characters; the limit is {MAX_LENGTH}")
    return message


def support_end_date(version: str) -> str | None:
    """The end-of-standard-support date (UTC, YYYY-MM-DD) of a Kubernetes version; None when the service has none."""
    offered = versions.describe_cluster_versions(versions.DEFAULT_REGION)
    current = next((v for v in offered if v.version == version), None)
    return current.end_of_standard_support if current else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", required=True, choices=["dev", "prod"])
    parser.add_argument("--cluster-name", required=True)
    parser.add_argument("--github-env", metavar="FILE", help="append CFN_PARAM_ReminderMessageJson=<json> to this file")
    args = parser.parse_args(argv)
    try:
        version = versions.configured_version(args.env)
    except versions.VersionCheckError as error:
        print(f"::error::{error}")
        return 1
    try:
        end_date = support_end_date(version)
    except versions.VersionCheckError as error:
        # The reminder must not block a deployment: it says the date is unknown and how to find it.
        print(f"::warning::{error}", file=sys.stderr)
        end_date = None
    message = build_message(args.env, args.cluster_name, version, end_date)
    print(message)
    if args.github_env:
        with open(args.github_env, "a", encoding="utf-8") as handle:
            handle.write(f"CFN_PARAM_ReminderMessageJson={json.dumps(message)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Read-only check that nothing of this repository is left in an environment after a destroy.

1. Resource Groups Tagging API: every resource tagged repo-name=<repository> and env-type=<env>. Entries that
   the index still lists but that no longer exist, or that are only tombstones (deleted NAT gateways, EKS
   clusters, load balancers), are reported apart and do not count.
2. Kinds the tagging API does not return or that carry no tags: EventBridge Scheduler schedules, IAM roles,
   secrets (also those scheduled for deletion), log groups and the SSM parameters of the API backend
   registry, all by the naming convention of this project (<context> and eks in the name, -<env> suffix).
3. Known and expected residues are labelled, not hidden: KMS keys pending deletion, secrets scheduled for
   deletion.

Usage: python post_destroy_check.py --repo-name <repository> --env dev [--profile P] [--region us-east-2]
       [--context oecalc]
Exit code 0 when nothing unexpected is left, 1 otherwise, 2 when a read fails. Only list and describe calls
are made.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable
from typing import Any

Row = tuple[str, str]
RESIDUE_KMS_STATE = "PendingDeletion"


def aws(args: list[str], profile: str | None, region: str) -> dict[str, Any]:
    cmd = ["aws", *args, "--region", region, "--output", "json"] + (["--profile", profile] if profile else [])
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)  # noqa: S603
    if result.returncode != 0:
        redacted = re.sub(r"[0-9]{12}", "************", result.stderr.strip())[:200]
        raise RuntimeError(f"aws {' '.join(args[:2])} failed: {redacted}")
    data: dict[str, Any] = json.loads(result.stdout) if result.stdout.strip() else {}
    return data


def _not_found(exc: RuntimeError) -> bool:
    text = str(exc)
    return "NotFound" in text or "does not exist" in text or "MISSING" in text or "ResourceNotFound" in text


def _ec2_stale(rest: str, profile: str | None, region: str) -> bool:
    kind, _, rid = rest.partition("/")
    if kind == "natgateway":
        found = aws(["ec2", "describe-nat-gateways", "--nat-gateway-ids", rid], profile, region).get("NatGateways", [])
        return not found or found[0]["State"] == "deleted"
    if kind == "vpc-endpoint":
        found = aws(["ec2", "describe-vpc-endpoints", "--vpc-endpoint-ids", rid], profile, region).get(
            "VpcEndpoints", []
        )
        return not found or found[0]["State"] in ("deleted", "deleting")
    by_id = {
        "security-group": ("describe-security-groups", "--group-ids", "SecurityGroups"),
        "subnet": ("describe-subnets", "--subnet-ids", "Subnets"),
        "vpc": ("describe-vpcs", "--vpc-ids", "Vpcs"),
        "route-table": ("describe-route-tables", "--route-table-ids", "RouteTables"),
        "internet-gateway": ("describe-internet-gateways", "--internet-gateway-ids", "InternetGateways"),
        "elastic-ip": ("describe-addresses", "--allocation-ids", "Addresses"),
    }
    if kind in by_id:
        command, flag, key = by_id[kind]
        return not aws(["ec2", command, flag, rid], profile, region).get(key)
    if kind == "security-group-rule":
        rules = aws(["ec2", "describe-security-group-rules", "--security-group-rule-ids", rid], profile, region).get(
            "SecurityGroupRules"
        )
        return not rules
    return False


def _eks_stale(rest: str, profile: str | None, region: str) -> bool:
    kind, _, name = rest.partition("/")
    if kind == "cluster":
        cluster = aws(["eks", "describe-cluster", "--name", name], profile, region).get("cluster", {})
        return not cluster or cluster.get("status") == "DELETING"
    if kind == "nodegroup":
        cluster, _, group = name.partition("/")
        found = aws(
            ["eks", "describe-nodegroup", "--cluster-name", cluster, "--nodegroup-name", group],
            profile,
            region,
        )
        return not found.get("nodegroup")
    return False


def _elb_stale(arn: str, rest: str, profile: str | None, region: str) -> bool:
    if rest.startswith("loadbalancer/"):
        found = aws(["elbv2", "describe-load-balancers", "--load-balancer-arns", arn], profile, region)
        return not found.get("LoadBalancers")
    if rest.startswith("targetgroup/"):
        found = aws(["elbv2", "describe-target-groups", "--target-group-arns", arn], profile, region)
        return not found.get("TargetGroups")
    return False


def is_stale(arn: str, profile: str | None, region: str) -> bool:
    """True when the tagging index still lists a resource that no longer exists or is only a tombstone.

    The tagging API lags behind deletes; NAT gateways stay 'deleted' for about an hour and EKS clusters
    stay visible while deleting. Unknown types are never stale.
    """
    parts = arn.split(":", 5)
    service, rest = parts[2], parts[5]
    try:
        if service == "ec2":
            return _ec2_stale(rest, profile, region)
        if service == "eks":
            return _eks_stale(rest, profile, region)
        if service == "elasticloadbalancing":
            return _elb_stale(arn, rest, profile, region)
    except RuntimeError as exc:
        return _not_found(exc)
    return False


def short(arn: str) -> str:
    parts = arn.split(":", 5)
    return f"{parts[2]}:{parts[5]}" if len(parts) > 5 else arn


def is_project_name(name: str, context: str, suffix: str) -> bool:
    """Naming convention of this project: <context> and 'eks' in the name and the -<env> suffix."""
    return context in name and "eks" in name and (name.endswith(suffix) or f"{suffix}/" in name)


def collect(a: argparse.Namespace) -> tuple[list[Row], list[Row], list[Row], list[str]]:
    suffix = f"-{a.env}"
    left: list[Row] = []  # unexpected
    expected: list[Row] = []  # known residues
    stale: list[Row] = []  # listed by the tagging index but already gone or only tombstones
    skipped: list[str] = []  # checks that could not run (missing permission)

    tagged = aws(
        [
            "resourcegroupstaggingapi",
            "get-resources",
            "--tag-filters",
            f"Key=repo-name,Values={a.repo_name}",
            f"Key=env-type,Values={a.env}",
        ],
        a.profile,
        a.region,
    )
    for item in tagged.get("ResourceTagMappingList", []):
        arn = item["ResourceARN"]
        if ":kms:" in arn and ":key/" in arn:
            state = aws(["kms", "describe-key", "--key-id", arn], a.profile, a.region)["KeyMetadata"]["KeyState"]
            (expected if state == RESIDUE_KMS_STATE else left).append(("kms key", f"{short(arn)} ({state})"))
        elif is_stale(arn, a.profile, a.region):
            stale.append(("stale tag index entry", short(arn)))
        else:
            left.append(("tagged resource", short(arn)))

    def optional(name: str, fn: Callable[[], None]) -> None:
        try:
            fn()
        except RuntimeError as exc:
            if "AccessDenied" in str(exc) or "not authorized" in str(exc):
                skipped.append(name)
            else:
                raise

    def schedules() -> None:
        for s in aws(["scheduler", "list-schedules"], a.profile, a.region).get("Schedules", []):
            if is_project_name(s["Name"], a.context, suffix):
                left.append(("scheduler schedule", s["Name"]))

    def iam_roles() -> None:
        for r in aws(["iam", "list-roles"], a.profile, a.region).get("Roles", []):
            if not is_project_name(r["RoleName"], a.context, suffix):
                continue
            listed = aws(["iam", "list-role-tags", "--role-name", r["RoleName"]], a.profile, a.region)
            tags = {t["Key"]: t["Value"] for t in listed.get("Tags", [])}
            if tags.get("repo-name", a.repo_name) == a.repo_name:
                left.append(("iam role", r["RoleName"]))

    def secrets() -> None:
        listed = aws(["secretsmanager", "list-secrets", "--include-planned-deletion"], a.profile, a.region)
        for s in listed.get("SecretList", []):
            tags = {t["Key"]: t["Value"] for t in s.get("Tags", [])}
            if is_project_name(s["Name"], a.context, suffix) and tags.get("repo-name", a.repo_name) == a.repo_name:
                label = s["Name"] + (" (scheduled for deletion)" if s.get("DeletedDate") else "")
                (expected if s.get("DeletedDate") else left).append(("secret", label))

    def log_groups() -> None:
        listed = aws(["logs", "describe-log-groups", "--log-group-name-pattern", a.context], a.profile, a.region)
        for g in listed.get("logGroups", []):
            name = g["logGroupName"]
            if not is_project_name(name, a.context, suffix):
                continue
            arn = g["arn"].removesuffix(":*")
            tags = aws(["logs", "list-tags-for-resource", "--resource-arn", arn], a.profile, a.region).get("tags", {})
            if tags.get("repo-name", a.repo_name) == a.repo_name:
                left.append(("log group", name))

    def backend_registry() -> None:
        path = f"/{a.context}/{a.env}/api-backends/eks"
        listed = aws(["ssm", "get-parameters-by-path", "--path", path, "--recursive"], a.profile, a.region)
        left.extend(("ssm parameter", p["Name"]) for p in listed.get("Parameters", []))

    for name, fn in (
        ("scheduler", schedules),
        ("iam roles", iam_roles),
        ("secrets", secrets),
        ("log groups", log_groups),
        ("ssm backend registry", backend_registry),
    ):
        optional(name, fn)
    return left, expected, stale, skipped


def print_report(
    a: argparse.Namespace, left: list[Row], expected: list[Row], stale: list[Row], skipped: list[str]
) -> None:
    print(f"Post-destroy check: repo-name={a.repo_name} env={a.env} (read-only)")
    for title, rows in (
        ("LEFT OVER", left),
        ("Expected residues (not an error)", expected),
        ("Tombstones already gone (not an error; the tag index keeps them for a while)", stale),
    ):
        print(f"\n{title}: {len(rows)}")
        counts: dict[str, int] = {}
        for category, _ in rows:
            counts[category] = counts.get(category, 0) + 1
        for category, n in sorted(counts.items()):
            print(f"  {category}: {n}")
        if rows is not stale:
            for category, item in rows[:60]:
                print(f"    - {category}: {item}")
    if skipped:
        print(f"\nSKIPPED (no permission, not verified): {', '.join(skipped)}")
    print("\nNot covered: the CloudFormation artifact bucket and stack managed by SAM (kept on purpose).")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-name", required=True)
    ap.add_argument("--env", required=True)
    ap.add_argument("--profile")
    ap.add_argument("--region", default="us-east-2")
    ap.add_argument("--context", default="oecalc", help="project context segment of the naming convention")
    ap.add_argument("--retries", type=int, default=1, help="passes while something is left (deletes can lag)")
    ap.add_argument("--wait", type=int, default=60, help="seconds between passes")
    a = ap.parse_args(argv)
    try:
        for attempt in range(1, max(a.retries, 1) + 1):
            left, expected, stale, skipped = collect(a)
            if not left or attempt == a.retries:
                break
            print(f"pass {attempt}: {len(left)} left, waiting {a.wait}s", flush=True)
            time.sleep(a.wait)
    except (RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError, KeyError) as exc:
        print(f"cannot complete the check: {exc}")
        return 2
    print_report(a, left, expected, stale, skipped)
    return 1 if left else 0


if __name__ == "__main__":
    sys.exit(main())

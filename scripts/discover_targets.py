#!/usr/bin/env python3
"""Find the alarm dimensions that only exist after the Helm step (task 5.5, requirement 13.3).

The Auto Scaling group of the managed node group, the application load balancer and the target groups are
created by EKS and by the AWS Load Balancer Controller, so their generated names are unknown to the templates.
This script finds them by tag and prints them as parameters of templates/alarms.yaml, so every deployment
passes the dimensions of the cluster that exists now (a recreated cluster needs no manual edit):

* Auto Scaling group: tag `eks:cluster-name` (autoscaling describe-auto-scaling-groups, read-only);
* load balancer and target groups: tag `elbv2.k8s.aws/cluster` (resourcegroupstaggingapi get-resources,
  read-only); a target group belongs to a workload when its `ingress.k8s.aws/resource` tag names the service
  (`<namespace>/<ingress>-<service>:<port>`).

It fails with a clear message (exit 1) when something is absent: no group, no application load balancer, or no
target group for a workload. With --enable-asg-metrics it also turns on the group metric `GroupInServiceInstances`
(the managed node group does not collect it and CloudFormation cannot enable it); that is the only write and it is
idempotent.

Usage:
    python scripts/discover_targets.py --cluster-name NAME [--github-env FILE] [--enable-asg-metrics]
        prints Name=value lines; --github-env appends CFN_PARAM_Name=value lines to that file
        (GITHUB_ENV), which scripts/cfn_deploy.py reads as parameter overrides
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from typing import Any

WORKLOADS = ("add", "sub", "mul", "div", "history")
CLUSTER_TAG = "elbv2.k8s.aws/cluster"
RESOURCE_TAG = "ingress.k8s.aws/resource"
ASG_TAG = "eks:cluster-name"
GROUP_METRIC = "GroupInServiceInstances"


class DiscoveryError(Exception):
    """A dimension could not be found; the message says which one and where it was looked up."""


def aws_json(args: list[str]) -> Any:
    command = ["aws", *args, "--output", "json"]
    completed = subprocess.run(command, capture_output=True, text=True, check=False)  # noqa: S603
    if completed.returncode != 0:
        raise DiscoveryError(f"aws {' '.join(args[:2])} failed: {completed.stderr.strip()[:500]}")
    return json.loads(completed.stdout) if completed.stdout.strip() else {}


def find_auto_scaling_group(cluster: str, groups: dict[str, Any]) -> str:
    names = sorted(g["AutoScalingGroupName"] for g in groups.get("AutoScalingGroups", []))
    if not names:
        raise DiscoveryError(f"no Auto Scaling group with tag {ASG_TAG}={cluster}: is the node group created?")
    if len(names) > 1:
        raise DiscoveryError(f"{len(names)} Auto Scaling groups carry tag {ASG_TAG}={cluster}; expected one: {names}")
    return names[0]


def tag_map(mapping: dict[str, Any]) -> dict[str, str]:
    return {tag["Key"]: tag["Value"] for tag in mapping.get("Tags", [])}


def dimension(arn: str, kind: str) -> str:
    """'...:loadbalancer/app/k8s-x/abc' -> 'app/k8s-x/abc'; '...:targetgroup/k8s-x/abc' -> 'targetgroup/k8s-x/abc'."""
    _, _, resource = arn.partition(":" + kind + "/")
    if not resource:
        raise DiscoveryError(f"unexpected ARN for a {kind}: {arn.split(':')[-1]}")
    return resource if kind == "loadbalancer" else f"{kind}/{resource}"


def owns_workload(resource_tag: str, workload: str) -> bool:
    """The LBC tag is `<namespace>/<ingress>-<service>:<port>`; match the service at the end, not a substring."""
    service = resource_tag.rpartition(":")[0] if ":" in resource_tag else resource_tag
    return service.endswith(f"-{workload}")


def find_load_balancer_and_target_groups(cluster: str, resources: dict[str, Any]) -> dict[str, str]:
    balancers: list[str] = []
    target_groups: dict[str, list[str]] = {name: [] for name in WORKLOADS}
    for mapping in resources.get("ResourceTagMappingList", []):
        arn = mapping["ResourceARN"]
        if ":loadbalancer/app/" in arn:
            balancers.append(dimension(arn, "loadbalancer"))
        elif ":targetgroup/" in arn:
            resource_tag = tag_map(mapping).get(RESOURCE_TAG, "")
            for name in WORKLOADS:
                if owns_workload(resource_tag, name):
                    target_groups[name].append(dimension(arn, "targetgroup"))
    if not balancers:
        raise DiscoveryError(
            f"no application load balancer with tag {CLUSTER_TAG}={cluster}: is the release deployed (Helm step)?"
        )
    if len(balancers) > 1:
        raise DiscoveryError(f"{len(balancers)} application load balancers carry tag {CLUSTER_TAG}={cluster}")
    found: dict[str, str] = {"LoadBalancerDimension": balancers[0]}
    for name in WORKLOADS:
        matches = sorted(set(target_groups[name]))
        if len(matches) != 1:
            raise DiscoveryError(
                f"{len(matches)} target groups for the {name} service (tag {RESOURCE_TAG} ending in -{name});"
                " expected one"
            )
        found[f"TargetGroup{name.capitalize()}"] = matches[0]
    return found


def discover(cluster: str) -> dict[str, str]:
    groups = aws_json(
        ["autoscaling", "describe-auto-scaling-groups", "--filters", f"Name=tag:{ASG_TAG},Values={cluster}"]
    )
    resources = aws_json(
        [
            "resourcegroupstaggingapi",
            "get-resources",
            "--tag-filters",
            f"Key={CLUSTER_TAG},Values={cluster}",
            "--resource-type-filters",
            "elasticloadbalancing:loadbalancer",
            "elasticloadbalancing:targetgroup",
        ]
    )
    values = {"AutoScalingGroupName": find_auto_scaling_group(cluster, groups)}
    values.update(find_load_balancer_and_target_groups(cluster, resources))
    return values


def enable_group_metrics(group: str) -> None:
    aws_json(
        [
            "autoscaling",
            "enable-metrics-collection",
            "--auto-scaling-group-name",
            group,
            "--granularity",
            "1Minute",
            "--metrics",
            GROUP_METRIC,
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--cluster-name", required=True)
    parser.add_argument("--github-env", metavar="FILE", help="append CFN_PARAM_<Name>=<value> lines to this file")
    parser.add_argument("--enable-asg-metrics", action="store_true", help="turn on the in-service group metric")
    args = parser.parse_args(argv)
    try:
        values = discover(args.cluster_name)
        if args.enable_asg_metrics:
            enable_group_metrics(values["AutoScalingGroupName"])
    except DiscoveryError as error:
        print(f"::error::{error}")
        return 1
    for name, value in values.items():
        print(f"{name}={value}")
    if args.github_env:
        with open(args.github_env, "a", encoding="utf-8") as handle:
            handle.writelines(f"CFN_PARAM_{name}={value}\n" for name, value in values.items())
    return 0


if __name__ == "__main__":
    sys.exit(main())

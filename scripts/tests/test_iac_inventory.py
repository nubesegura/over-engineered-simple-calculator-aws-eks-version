"""Inventory and tag/name check with faked AWS responses (task 7.3)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import iac_inventory as inv
import pytest
from conftest import FakeCli, Reply

ACCOUNT = "123456789012"
REPO = inv.REPO_NAME
VPC = "vpc-0123456789abcdef0"
ELB = f"arn:aws:elasticloadbalancing:us-east-2:{ACCOUNT}"
LB_ARN = f"{ELB}:loadbalancer/app/k8s-calculat-0123456789/0123456789abcdef"
TG_ARN = f"{ELB}:targetgroup/k8s-calculat-add-0123456789/0123456789abcdef"
LS_ARN = f"{ELB}:listener/app/k8s-calculat-0123456789/0123456789abcdef/0123"
GOOD = {"team-owner": "t", "project-name": "p", "app-name": "a", "repo-name": REPO, "env-type": "dev"}


def pairs(tags: dict[str, str]) -> list[dict[str, str]]:
    return [{"Key": k, "Value": v} for k, v in tags.items()]


def mapping(arn: str, tags: dict[str, str]) -> dict[str, Any]:
    return {"ResourceARN": arn, "Tags": pairs(tags)}


VPC_ITEM = mapping(f"arn:aws:ec2:us-east-2:{ACCOUNT}:vpc/{VPC}", GOOD)
ROLE_ITEM = mapping(f"arn:aws:iam::{ACCOUNT}:role/iamr-useast2-oecalc-eks-node-dev", GOOD)
GROUP = {"GroupId": "sg-1", "GroupName": "sgrp-useast2-oecalc-eks-alb-dev", "Tags": pairs(GOOD)}
RULE = {"SecurityGroupRuleId": "sgr-0123456789abcdef0", "GroupId": "sg-1", "IsEgress": False}


def wire(
    cli: FakeCli,
    by_repo: list[dict[str, Any]],
    by_env: list[dict[str, Any]] | None = None,
    groups: list[dict[str, Any]] | None = None,
    elb_tag_set: dict[str, dict[str, str]] | None = None,
) -> None:
    def tagging(command: list[str]) -> dict[str, Any]:
        filter_key = command[command.index("--tag-filters") + 1]
        return {"ResourceTagMappingList": by_repo if "repo-name" in filter_key else by_env or []}

    cli.on("resourcegroupstaggingapi get-resources", tagging)
    cli.on("ec2 describe-security-groups", {"SecurityGroups": groups or []})
    cli.on("ec2 describe-security-group-rules", {"SecurityGroupRules": [RULE]})
    balancers = [
        {"LoadBalancerArn": LB_ARN, "LoadBalancerName": "k8s-calculat-0123456789", "VpcId": VPC},
        {"LoadBalancerArn": "other-arn", "LoadBalancerName": "other", "VpcId": "vpc-other"},
    ]
    cli.on("elbv2 describe-load-balancers", {"LoadBalancers": balancers})
    target_group = {"TargetGroupArn": TG_ARN, "TargetGroupName": "k8s-calculat-add-0123456789", "VpcId": VPC}
    cli.on("elbv2 describe-target-groups", {"TargetGroups": [target_group]})
    cli.on("elbv2 describe-listeners", {"Listeners": [{"ListenerArn": LS_ARN, "Port": 443, "Protocol": "HTTPS"}]})
    tag_set = elb_tag_set if elb_tag_set is not None else {LB_ARN: GOOD, TG_ARN: GOOD, LS_ARN: GOOD}
    descriptions = [{"ResourceArn": arn, "Tags": pairs(tags)} for arn, tags in tag_set.items()]
    cli.on("elbv2 describe-tags", {"TagDescriptions": descriptions})


def test_clean_environment_has_no_findings_and_lists_every_kind(cli: FakeCli, tmp_path: Path) -> None:
    wire(cli, [VPC_ITEM, ROLE_ITEM], groups=[GROUP])
    out = tmp_path / "report.json"
    assert inv.main(["--env", "dev", "--output", str(out)]) == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["findings"] == [] and report["vpc"] == VPC
    for kind in (
        "ec2:security-group",
        "ec2:security-group-rule",
        "elasticloadbalancing:loadbalancer",
        "elasticloadbalancing:targetgroup",
        "elasticloadbalancing:listener",
        "iam:role",
    ):
        assert report["counts"][kind] == 1
    assert not any("other" in r["arn"] for r in report["resources"])  # a load balancer of another VPC is ignored
    rule = next(r for r in report["resources"] if r["type"] == "ec2:security-group-rule")
    assert rule["detail"]["direction"] == "ingress"  # listed, never tag-checked


def test_controller_resources_without_tags_are_reported(cli: FakeCli, capsys: pytest.CaptureFixture[str]) -> None:
    partial = {k: v for k, v in GOOD.items() if k not in ("app-name", "env-type")}
    wire(cli, [VPC_ITEM], groups=[GROUP], elb_tag_set={LB_ARN: partial, TG_ARN: GOOD})
    assert inv.main(["--env", "dev"]) == 1
    text = capsys.readouterr().out
    assert f"{LB_ARN}: missing tag app-name" in text and f"{LB_ARN}: missing tag env-type" in text
    assert f"{LS_ARN}: missing tag team-owner" in text  # the listener came back with no tags at all
    assert f"{TG_ARN}:" not in text


@pytest.mark.parametrize(
    ("arn", "tags", "name"),
    [
        (
            f"arn:aws:iam::{ACCOUNT}:role/service/iamr-useast2-oecalc-eks-node-dev",
            {},
            "iamr-useast2-oecalc-eks-node-dev",
        ),
        (
            f"arn:aws:secretsmanager:us-east-2:{ACCOUNT}:secret:sm-useast2-oecalc-eks-app-dev-AbC123",
            {},
            "sm-useast2-oecalc-eks-app-dev",
        ),
        (
            f"arn:aws:logs:us-east-2:{ACCOUNT}:log-group:/aws/eks/eksc-useast2-oecalc-dev/cluster:*",
            {},
            "/aws/eks/eksc-useast2-oecalc-dev/cluster",
        ),
        (f"arn:aws:sqs:us-east-2:{ACCOUNT}:sqs-useast2-oecalc-eks-ingest-dev", {}, "sqs-useast2-oecalc-eks-ingest-dev"),
        ("arn:aws:s3:::s3-useast2-oecalc-eks-ingest-dev", {}, "s3-useast2-oecalc-eks-ingest-dev"),
        (LB_ARN, {}, "k8s-calculat-0123456789"),
        (LS_ARN, {}, None),
        (f"arn:aws:ec2:us-east-2:{ACCOUNT}:volume/vol-0123456789abcdef0", {}, None),
        (f"arn:aws:ec2:us-east-2:{ACCOUNT}:subnet/subnet-0123456789abcdef0", {"Name": "sn-a"}, "sn-a"),
        (f"arn:aws:kms:us-east-2:{ACCOUNT}:key/00000000-0000-0000-0000-000000000000", {}, None),
        (f"arn:aws:rds:us-east-2:{ACCOUNT}:proxy:prx-0123456789abcdef0", {}, None),
        ("not-an-arn", {}, None),
    ],
)
def test_candidate_name_by_kind(arn: str, tags: dict[str, str], name: str | None) -> None:
    assert inv.candidate_name(arn, tags) == name


@pytest.mark.parametrize(
    ("name", "valid"),
    [
        ("vpc-useast2-oecalc-eks-dev", True),
        ("iamr-useast2-oecalc-dev", True),
        ("/aws/lambda/lmb-useast2-oecalc-eks-nat-dev", True),
        ("alias/kms-useast2-oecalc-eks-dev", True),
        ("k8s-traffic-eksc-useast2-oecalc-dev-0123", True),  # generated by the controller
        ("eks-cluster-sg-eksc-useast2-oecalc-dev-123", True),  # generated by EKS
        ("sqs-useast2-oecalc-eks-ingest-prod", False),  # wrong environment
        ("sqs-useast1-oecalc-eks-ingest-dev", False),  # wrong region code
        ("my-queue", False),
        ("sqs-useast2-oecalc-eks-ingest", False),  # no env suffix
    ],
)
def test_name_pattern(name: str, valid: bool) -> None:
    assert inv.name_is_valid(name, inv.name_pattern("useast2", "oecalc", "dev")) is valid


def test_bad_name_wrong_tag_value_and_other_environments(cli: FakeCli, capsys: pytest.CaptureFixture[str]) -> None:
    sns = f"arn:aws:sns:us-east-2:{ACCOUNT}:sns-useast2-oecalc-eks-alerts"
    items = [
        VPC_ITEM,
        mapping(f"arn:aws:sqs:us-east-2:{ACCOUNT}:my-queue", GOOD),
        mapping(f"{sns}-dev", {**GOOD, "repo-name": "x"}),
        mapping(f"{sns}-prod", {**GOOD, "env-type": "prod"}),
        mapping(f"arn:aws:rds:us-east-2:{ACCOUNT}:snapshot:rds:x", {}),
    ]
    wire(cli, items, groups=[GROUP])
    assert inv.main(["--env", "dev"]) == 1
    text = capsys.readouterr().out
    assert "my-queue breaks the pattern" in text and "repo-name is x" in text
    assert "alerts-prod" not in text and "snapshot" not in text  # other environment and runtime artifacts


def test_partially_tagged_resource_is_found_by_the_environment_tag(
    cli: FakeCli, capsys: pytest.CaptureFixture[str]
) -> None:
    sns = f"arn:aws:sns:us-east-2:{ACCOUNT}:sns-useast2-oecalc"
    ours = mapping(f"{sns}-eks-alerts-dev", {"env-type": "dev"})
    foreign = mapping(f"{sns}-ecs-alerts-dev", {"env-type": "dev"})
    wire(cli, [VPC_ITEM], by_env=[ours, foreign], groups=[GROUP])
    assert inv.main(["--env", "dev"]) == 1
    text = capsys.readouterr().out
    assert "eks-alerts-dev: missing tag team-owner" in text and "missing tag repo-name" in text
    assert "ecs-alerts" not in text


def test_default_security_group_is_ignored(cli: FakeCli) -> None:
    wire(cli, [VPC_ITEM], groups=[GROUP, {"GroupId": "sg-2", "GroupName": "default", "Tags": []}])
    assert inv.main(["--env", "dev"]) == 0


def test_no_vpc_means_no_vpc_scan(cli: FakeCli, capsys: pytest.CaptureFixture[str]) -> None:
    wire(cli, [ROLE_ITEM])
    assert inv.main(["--env", "dev"]) == 0
    assert "VPC not found" in capsys.readouterr().out
    assert not cli.called("ec2 describe-security-groups")


def test_elb_tags_are_requested_in_batches_of_twenty(cli: FakeCli) -> None:
    cli.on("elbv2 describe-tags", {"TagDescriptions": []})
    inv.elb_tags([f"arn:x:{n}" for n in range(45)], "us-east-2", None)
    assert len(cli.called("elbv2 describe-tags")) == 3


def test_failed_read_exits_with_two_and_hides_account_ids(cli: FakeCli, capsys: pytest.CaptureFixture[str]) -> None:
    cli.on("resourcegroupstaggingapi get-resources", Reply(returncode=255, stderr=f"AccessDenied for {ACCOUNT}"))
    assert inv.main(["--env", "dev", "--profile", "p"]) == 2
    err = capsys.readouterr().err
    assert ACCOUNT not in err and "AccessDenied" in err
    assert "--profile" in cli.calls[0]

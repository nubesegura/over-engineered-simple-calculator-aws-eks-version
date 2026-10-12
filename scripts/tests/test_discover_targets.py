"""Discovery of the alarm dimensions by tag, with faked answers of the AWS CLI (no AWS call)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import discover_targets as script
import pytest
from conftest import FakeCli, Reply

ASG = "autoscaling describe-auto-scaling-groups"
TAGGING = "resourcegroupstaggingapi get-resources"
ENABLE = "autoscaling enable-metrics-collection"
ARN = "arn:aws:elasticloadbalancing:us-east-2:111122223333:"


def load_balancer(name: str = "k8s-calc-abc", suffix: str = "0123456789abcdef") -> dict[str, Any]:
    return {"ResourceARN": f"{ARN}loadbalancer/app/{name}/{suffix}", "Tags": []}


def target_group(workload: str, ingress: str = "calculator-api", suffix: str | None = None) -> dict[str, Any]:
    suffix = suffix or workload.encode().hex().ljust(16, "0")
    return {
        "ResourceARN": f"{ARN}targetgroup/k8s-calc-{workload}/{suffix}",
        "Tags": [{"Key": "ingress.k8s.aws/resource", "Value": f"calculator/{ingress}-{workload}:8443"}],
    }


def everything() -> dict[str, Any]:
    return {"ResourceTagMappingList": [load_balancer(), *(target_group(name) for name in script.WORKLOADS)]}


def groups(*names: str) -> dict[str, Any]:
    return {"AutoScalingGroups": [{"AutoScalingGroupName": name} for name in names]}


def test_dimensions_are_found_by_tag_and_filtered_by_the_cluster(cli: FakeCli) -> None:
    cli.on(ASG, groups("eks-eksng-useast2-oecalc-dev-1234"))
    cli.on(TAGGING, everything())
    values = script.discover("cluster-a")
    assert values["AutoScalingGroupName"] == "eks-eksng-useast2-oecalc-dev-1234"
    assert values["LoadBalancerDimension"] == "app/k8s-calc-abc/0123456789abcdef"
    assert values["TargetGroupHistory"].startswith("targetgroup/k8s-calc-history/")
    assert set(values) == {
        "AutoScalingGroupName",
        "LoadBalancerDimension",
        *(f"TargetGroup{name.capitalize()}" for name in script.WORKLOADS),
    }
    assert "Name=tag:eks:cluster-name,Values=cluster-a" in cli.called(ASG)[0]
    assert "Key=elbv2.k8s.aws/cluster,Values=cluster-a" in cli.called(TAGGING)[0]


def test_the_preflight_target_groups_of_the_second_ingress_are_ignored(cli: FakeCli) -> None:
    resources = everything()
    resources["ResourceTagMappingList"] += [
        target_group(name, ingress="calculator-preflight", suffix=f"{i:016x}")
        for i, name in enumerate(script.WORKLOADS)
    ]
    cli.on(ASG, groups("eks-eksng-useast2-oecalc-dev-1234"))
    cli.on(TAGGING, resources)
    values = script.discover("cluster-a")
    assert values["TargetGroupAdd"] == target_group("add")["ResourceARN"].split(":")[-1]


def test_a_workload_is_matched_by_the_end_of_the_service_name_not_a_substring() -> None:
    assert script.owns_workload("calculator/calculator-api-add:8443", "add")
    assert not script.owns_workload("calculator/calculator-api-address:8443", "add")
    assert not script.owns_workload("", "add")


def test_no_auto_scaling_group_fails_and_names_the_tag(cli: FakeCli) -> None:
    cli.on(ASG, groups())
    cli.on(TAGGING, everything())
    with pytest.raises(script.DiscoveryError, match=r"no Auto Scaling group with tag eks:cluster-name=cluster-a"):
        script.discover("cluster-a")


def test_two_groups_are_refused_instead_of_guessing(cli: FakeCli) -> None:
    cli.on(ASG, groups("one", "two"))
    cli.on(TAGGING, everything())
    with pytest.raises(script.DiscoveryError, match="2 Auto Scaling groups"):
        script.discover("cluster-a")


def test_missing_load_balancer_says_the_helm_step_may_not_have_run(cli: FakeCli) -> None:
    cli.on(ASG, groups("g"))
    cli.on(TAGGING, {"ResourceTagMappingList": []})
    with pytest.raises(script.DiscoveryError, match="no application load balancer.*Helm"):
        script.discover("cluster-a")


def test_a_missing_target_group_names_the_service(cli: FakeCli) -> None:
    listing = everything()
    listing["ResourceTagMappingList"] = [m for m in listing["ResourceTagMappingList"] if "-div" not in m["ResourceARN"]]
    cli.on(ASG, groups("g"))
    cli.on(TAGGING, listing)
    with pytest.raises(script.DiscoveryError, match="0 target groups for the div service"):
        script.discover("cluster-a")


def test_main_exits_1_with_an_error_annotation_when_something_is_absent(
    cli: FakeCli, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.on(ASG, groups("g"))
    cli.on(TAGGING, {"ResourceTagMappingList": []})
    assert script.main(["--cluster-name", "cluster-a"]) == 1
    assert capsys.readouterr().out.startswith("::error::no application load balancer")


def test_main_writes_cfn_param_lines_and_turns_on_the_group_metric(cli: FakeCli, tmp_path: Path) -> None:
    cli.on(ASG, groups("g-1"))
    cli.on(TAGGING, everything())
    cli.on(ENABLE, Reply())
    github_env = tmp_path / "env"
    github_env.write_text("EXISTING=1\n", encoding="utf-8")
    code = script.main(["--cluster-name", "cluster-a", "--github-env", str(github_env), "--enable-asg-metrics"])
    assert code == 0
    lines = github_env.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "EXISTING=1" and "CFN_PARAM_AutoScalingGroupName=g-1" in lines
    assert len([line for line in lines if line.startswith("CFN_PARAM_TargetGroup")]) == 5
    enable = cli.called(ENABLE)[0]
    assert enable[enable.index("--auto-scaling-group-name") + 1] == "g-1"
    assert enable[enable.index("--metrics") + 1] == "GroupInServiceInstances"


def test_the_group_metric_is_not_touched_without_the_flag(cli: FakeCli) -> None:
    cli.on(ASG, groups("g-1"))
    cli.on(TAGGING, everything())
    assert script.main(["--cluster-name", "cluster-a"]) == 0
    assert not cli.called(ENABLE)


def test_a_failing_aws_call_is_reported_without_a_traceback(cli: FakeCli, capsys: pytest.CaptureFixture[str]) -> None:
    cli.on(ASG, Reply(returncode=255, stderr="AccessDenied: not allowed"))
    assert script.main(["--cluster-name", "cluster-a"]) == 1
    assert "autoscaling describe-auto-scaling-groups failed: AccessDenied" in capsys.readouterr().out

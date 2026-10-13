"""Tests of the destroy flow of scripts/cfn_deploy.py (task 7.1): uninstall build, waits, registration, order.

AWS and the build project are faked; nothing reaches AWS.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import cfn_deploy as deploy
import pytest
from conftest import FakeCli
from test_cfn_deploy import World, deployed_world, repo  # noqa: F401  (repo is a fixture)

CLUSTER = "eksc-useast2-oecalc-dev"
VPC = "vpc-0abc"
LB_ARN = "arn:aws:elasticloadbalancing:us-east-2:111122223333:loadbalancer/app/k8s-alb/1"
TG_ARN = "arn:aws:elasticloadbalancing:us-east-2:111122223333:targetgroup/k8s-tg/1"
OTHER_ARN = "arn:aws:elasticloadbalancing:us-east-2:111122223333:loadbalancer/app/other/2"
STACK = "stk-useast2-oecalc-eks-{}-dev"


class Controller:
    """Scripted elbv2 and ec2: resources of the controller that disappear after `polls` listings."""

    def __init__(self, cli: FakeCli, polls: int = 0) -> None:
        self.polls = polls
        self.listings = 0
        cli.on("elbv2 describe-load-balancers", self.load_balancers)
        cli.on("elbv2 describe-target-groups", self.target_groups)
        cli.on("elbv2 describe-tags", self.tags)
        cli.on("ec2 describe-security-groups", self.security_groups)

    def present(self) -> bool:
        return self.listings <= self.polls

    def load_balancers(self, _args: list[str]) -> dict[str, Any]:
        self.listings += 1
        found = [{"LoadBalancerArn": OTHER_ARN, "LoadBalancerName": "other", "VpcId": "vpc-other"}]
        if self.present():
            found.append({"LoadBalancerArn": LB_ARN, "LoadBalancerName": "k8s-alb", "VpcId": VPC})
        return {"LoadBalancers": found}

    def target_groups(self, _args: list[str]) -> dict[str, Any]:
        found = [{"TargetGroupArn": TG_ARN, "TargetGroupName": "k8s-tg", "VpcId": VPC}] if self.present() else []
        return {"TargetGroups": found}

    def tags(self, args: list[str]) -> dict[str, Any]:
        arns = [a for a in args[args.index("--resource-arns") + 1 :] if a.startswith("arn:")]
        mine = {"Key": "elbv2.k8s.aws/cluster", "Value": CLUSTER}
        return {"TagDescriptions": [{"ResourceArn": a, "Tags": [mine]} for a in arns if "other" not in a]}

    def security_groups(self, _args: list[str]) -> dict[str, Any]:
        groups = [
            {"GroupId": "sg-1", "GroupName": "k8s-sg", "Tags": [{"Key": "elbv2.k8s.aws/cluster", "Value": CLUSTER}]}
        ]
        groups.append(
            {"GroupId": "sg-2", "GroupName": "x", "Tags": [{"Key": "elbv2.k8s.aws/cluster", "Value": "other"}]}
        )
        return {"SecurityGroups": groups if self.present() else []}


@pytest.fixture
def full(repo: Path, cli: FakeCli, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:  # noqa: F811
    """deployed_world plus the cluster and deploy stacks; the build is a recorded fake."""
    stacks_file = repo / "templates" / "stacks.json"
    data = json.loads(stacks_file.read_text(encoding="utf-8"))
    for env in ("dev", "prod"):
        data["environments"][env] += [
            {"unit": unit, "stack": f"stk-useast2-oecalc-eks-{unit}-{env}", "template": f"templates/{unit}.yaml"}
            for unit in ("cluster", "deploy")
        ]
    stacks_file.write_text(json.dumps(data), encoding="utf-8")
    world = deployed_world(cli)
    world.statuses[STACK.format("cluster")] = "CREATE_COMPLETE"
    world.statuses[STACK.format("deploy")] = "CREATE_COMPLETE"
    world.outputs[STACK.format("cluster")] = {"ClusterName": CLUSTER}
    world.outputs[STACK.format("network")] = {"VpcId": VPC}
    controller = Controller(cli)
    builds: list[str] = []
    monkeypatch.setattr(deploy, "start_uninstall_build", lambda env: builds.append(env))
    return {"world": world, "controller": controller, "builds": builds}


def index_of(cli: FakeCli, *words: str) -> int:
    return next(
        i for i, c in enumerate(cli.calls) if c[: len(words)] == list(words) or c[1 : 1 + len(words)] == list(words)
    )


def test_destroy_order_is_nat_build_wait_registration_then_stacks(
    cli: FakeCli, full: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(deploy, "start_uninstall_build", lambda env: cli.calls.append(["build", env]))
    assert deploy.main(["destroy", "--env", "dev", "--mode", "destroy"]) == 0
    up = next(c for c in cli.called("lambda invoke") if '"action":"up"' in c[c.index("--payload") + 1])
    build = index_of(cli, "build")
    wait = max(i for i, c in enumerate(cli.calls) if c[1:3] == ["elbv2", "describe-load-balancers"])
    positions = [
        cli.calls.index(up),
        build,
        wait,
        index_of(cli, "bash", "scripts/destroy_registry.sh"),
        index_of(cli, "cloudformation", "delete-stack"),
    ]
    assert positions == sorted(positions)
    deleted = [c[c.index("--stack-name") + 1] for c in cli.called("cloudformation delete-stack")]
    assert deleted == [STACK.format(n) for n in ("deploy", "cluster", "scheduler", "network", "security")]
    assert full["world"].statuses == {}


def test_the_wait_polls_until_the_resources_are_gone(
    cli: FakeCli, full: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    full["controller"].polls = 2
    assert deploy.main(["destroy", "--env", "dev", "--mode", "destroy"]) == 0
    out = capsys.readouterr().out
    assert (
        "Waiting for the controller to remove: load balancer k8s-alb; target group k8s-tg; security group sg-1 (k8s-sg)"
        in out
    )
    assert "No load balancer, target group or controller security group is left" in out
    assert "sg-2" not in out and "other" not in out  # another VPC or cluster is never listed


def test_the_wait_timeout_names_what_is_left_and_stops_before_the_registry_and_stacks(
    cli: FakeCli, full: dict[str, Any], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    full["controller"].polls = 10**6
    monkeypatch.setattr(deploy, "CONTROLLER_WAIT_SECONDS", 0)
    assert deploy.main(["destroy", "--env", "dev", "--mode", "destroy"]) == 1
    out = capsys.readouterr().out
    assert "timed out after 0 minutes" in out and "load balancer k8s-alb" in out
    assert not cli.called("bash scripts/destroy_registry.sh")
    assert not cli.called("cloudformation delete-stack")


def test_a_failed_build_stops_everything(cli: FakeCli, full: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(_env: str) -> None:
        raise deploy.DeployError("the uninstall build failed")

    monkeypatch.setattr(deploy, "start_uninstall_build", failing)
    assert deploy.main(["destroy", "--env", "dev", "--mode", "destroy"]) == 1
    assert not cli.called("bash scripts/destroy_registry.sh")
    assert not cli.called("cloudformation delete-stack")


def test_without_a_scheduler_stack_the_nat_gateway_is_not_recreated(cli: FakeCli, full: dict[str, Any]) -> None:
    del full["world"].statuses[STACK.format("scheduler")]
    assert deploy.main(["destroy", "--env", "dev", "--mode", "destroy"]) == 0
    assert full["builds"] == ["dev"]
    assert not cli.called("lambda invoke")


def test_without_cluster_or_deploy_no_build_runs_and_the_nat_gateway_is_only_removed(
    cli: FakeCli, full: dict[str, Any]
) -> None:
    del full["world"].statuses[STACK.format("deploy")]
    assert deploy.main(["destroy", "--env", "dev", "--mode", "destroy"]) == 0
    assert full["builds"] == []
    actions = [json.loads(c[c.index("--payload") + 1])["action"] for c in cli.called("lambda invoke")]
    assert actions == ["down"]


def test_plan_lists_the_uninstall_work_and_changes_nothing(
    cli: FakeCli, full: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    full["controller"].polls = 10**6
    assert deploy.main(["destroy", "--env", "dev", "--mode", "plan"]) == 0
    out = capsys.readouterr().out
    assert "Build in uninstall mode will run" in out and "recreated first" in out
    assert "calculator, cluster-autoscaler, aws-load-balancer-controller" in out
    assert (
        "Controller resources in the VPC now: load balancer k8s-alb; target group k8s-tg; security group sg-1 (k8s-sg)"
        in out
    )
    assert "dns-name and hosted-zone-id" in out
    assert full["builds"] == []
    for write in ("lambda invoke", "bash scripts/destroy_registry.sh", "cloudformation delete-stack"):
        assert not cli.called(write)


def test_the_empty_environment_ends_green_and_only_removes_the_registration(cli: FakeCli, repo: Path) -> None:  # noqa: F811
    World(cli)
    assert deploy.main(["destroy", "--env", "dev", "--mode", "destroy"]) == 0
    assert [c[0] for c in cli.calls if c[0] != "aws"] == ["bash"]
    assert not cli.called("elbv2 describe-load-balancers")


def test_the_uninstall_build_is_started_in_uninstall_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake(command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", fake)
    deploy.start_uninstall_build("dev")
    assert seen[0][1].endswith("run_deploy_build.py") and seen[0][2:] == [
        "run",
        "--env",
        "dev",
        "--command",
        "uninstall",
    ]
    monkeypatch.setattr(subprocess, "run", lambda c, **_k: subprocess.CompletedProcess(c, 1))
    with pytest.raises(deploy.DeployError, match="uninstall build failed"):
        deploy.start_uninstall_build("dev")


def test_remove_registration_passes_the_environment_and_reports_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs, command=command)
        return subprocess.CompletedProcess(command, 0, "ok\n", "")

    monkeypatch.setattr(subprocess, "run", fake)
    deploy.remove_registration("oecalc", "dev")
    assert captured["command"] == ["bash", "scripts/destroy_registry.sh"]
    assert captured["env"]["ENV_TYPE"] == "dev" and captured["env"]["CONTEXT"] == "oecalc"
    monkeypatch.setattr(subprocess, "run", lambda c, **_k: subprocess.CompletedProcess(c, 1, "", "denied"))
    with pytest.raises(deploy.DeployError, match="denied"):
        deploy.remove_registration("oecalc", "dev")


def test_a_leftover_without_a_known_cluster_is_found_by_the_controller_tag(cli: FakeCli) -> None:
    Controller(cli, polls=10**6)
    found = deploy.controller_resources(VPC, None)
    assert "load balancer k8s-alb" in found and "security group sg-2 (x)" in found


def test_delete_failed_after_the_uninstall_keeps_the_remaining_stacks(cli: FakeCli, full: dict[str, Any]) -> None:
    full["world"].delete_fails.add(STACK.format("network"))
    assert deploy.main(["destroy", "--env", "dev", "--mode", "destroy"]) == 1
    deleted = [c[c.index("--stack-name") + 1] for c in cli.called("cloudformation delete-stack")]
    assert STACK.format("security") not in deleted

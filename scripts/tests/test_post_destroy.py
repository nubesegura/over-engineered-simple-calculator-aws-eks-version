"""Tests of scripts/post_destroy_check.py with a faked AWS CLI (read-only calls only)."""

from __future__ import annotations

import argparse
import re
from typing import Any

import post_destroy_check as check
import pytest
import yaml
from conftest import REPO, FakeCli, Reply

REPO_NAME = "over-engineered-simple-calculator-aws-eks-version"
ACCT = "123456789012"
NAT_ARN = f"arn:aws:ec2:us-east-2:{ACCT}:natgateway/nat-1"
CLUSTER_ARN = f"arn:aws:eks:us-east-2:{ACCT}:cluster/eksc-useast2-oecalc-dev"
ALB_ARN = f"arn:aws:elasticloadbalancing:us-east-2:{ACCT}:loadbalancer/app/k8s-x/abc"
KEY_ARN = f"arn:aws:kms:us-east-2:{ACCT}:key/1111"
TOPIC_ARN = f"arn:aws:sns:us-east-2:{ACCT}:sns-useast2-oecalc-eks-alerts-dev"


class Account:
    """What the faked account holds; each field is the answer of one read-only call."""

    def __init__(self) -> None:
        self.tagged: list[str] = []
        self.responses: dict[str, Any] = {
            "ec2 describe-nat-gateways": {"NatGateways": [{"State": "deleted"}]},
            "eks describe-cluster": Reply(returncode=254, stderr="ResourceNotFoundException"),
            "elbv2 describe-load-balancers": Reply(returncode=254, stderr="LoadBalancerNotFound"),
            "kms describe-key": {"KeyMetadata": {"KeyState": "PendingDeletion"}},
            "scheduler list-schedules": {"Schedules": []},
            "iam list-roles": {"Roles": []},
            "iam list-role-tags": {"Tags": []},
            "secretsmanager list-secrets": {"SecretList": []},
            "logs describe-log-groups": {"logGroups": []},
            "logs list-tags-for-resource": {"tags": {}},
            "ssm get-parameters-by-path": {"Parameters": []},
        }

    def __call__(self, args: list[str]) -> Reply | Any:
        key = " ".join(args[1:3])
        if key == "resourcegroupstaggingapi get-resources":
            return {"ResourceTagMappingList": [{"ResourceARN": arn} for arn in self.tagged]}
        answer = self.responses[key]
        return answer(args) if callable(answer) else answer


@pytest.fixture
def account(cli: FakeCli) -> Account:
    acc = Account()
    for key in ["resourcegroupstaggingapi get-resources", *acc.responses]:
        cli.on(key, acc)
    return acc


def args(**extra: Any) -> argparse.Namespace:
    base = {"repo_name": REPO_NAME, "env": "dev", "profile": None, "region": "us-east-2", "context": "oecalc"}
    return argparse.Namespace(**{**base, **extra})


def run_main(*extra: str) -> int:
    return int(check.main(["--repo-name", REPO_NAME, "--env", "dev", *extra]))


def test_an_empty_environment_is_clean(account: Account, capsys: pytest.CaptureFixture[str]) -> None:
    assert run_main() == 0
    assert "LEFT OVER: 0" in capsys.readouterr().out


def test_tombstones_and_pending_key_do_not_count(account: Account, capsys: pytest.CaptureFixture[str]) -> None:
    account.tagged = [NAT_ARN, CLUSTER_ARN, ALB_ARN, KEY_ARN]
    assert run_main() == 0
    out = capsys.readouterr().out
    assert "Tombstones already gone (not an error; the tag index keeps them for a while): 3" in out
    assert "Expected residues (not an error): 1" in out
    assert "PendingDeletion" in out


def test_a_live_nat_gateway_and_an_enabled_key_are_left_over(
    account: Account, capsys: pytest.CaptureFixture[str]
) -> None:
    account.tagged = [NAT_ARN, KEY_ARN]
    account.responses["ec2 describe-nat-gateways"] = {"NatGateways": [{"State": "available"}]}
    account.responses["kms describe-key"] = {"KeyMetadata": {"KeyState": "Enabled"}}
    assert run_main() == 1
    out = capsys.readouterr().out
    assert "tagged resource: ec2:natgateway/nat-1" in out
    assert "kms key: kms:key/1111 (Enabled)" in out


def test_a_cluster_that_is_still_deleting_is_a_tombstone_but_an_active_one_is_left(account: Account) -> None:
    account.tagged = [CLUSTER_ARN]
    account.responses["eks describe-cluster"] = {"cluster": {"status": "DELETING"}}
    assert run_main() == 0
    account.responses["eks describe-cluster"] = {"cluster": {"status": "ACTIVE"}}
    assert run_main() == 1


def test_a_live_load_balancer_is_left_over(account: Account) -> None:
    account.tagged = [ALB_ARN]
    account.responses["elbv2 describe-load-balancers"] = {"LoadBalancers": [{"State": {"Code": "active"}}]}
    assert run_main() == 1


def test_unknown_resource_types_are_never_stale(account: Account, capsys: pytest.CaptureFixture[str]) -> None:
    account.tagged = [TOPIC_ARN]
    assert run_main() == 1
    assert "tagged resource: sns:sns-useast2-oecalc-eks-alerts-dev" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("rest", "key", "empty", "live"),
    [
        ("subnet/subnet-1", "ec2 describe-subnets", {"Subnets": []}, {"Subnets": [{}]}),
        (
            "security-group/sg-1",
            "ec2 describe-security-groups",
            {"SecurityGroups": []},
            {"SecurityGroups": [{}]},
        ),
        ("elastic-ip/eipalloc-1", "ec2 describe-addresses", {"Addresses": []}, {"Addresses": [{}]}),
        ("security-group-rule/sgr-1", "ec2 describe-security-group-rules", {}, {"SecurityGroupRules": [{}]}),
        (
            "vpc-endpoint/vpce-1",
            "ec2 describe-vpc-endpoints",
            {"VpcEndpoints": [{"State": "deleted"}]},
            {"VpcEndpoints": [{"State": "available"}]},
        ),
    ],
)
def test_ec2_kinds_are_stale_only_when_gone(cli: FakeCli, rest: str, key: str, empty: Any, live: Any) -> None:
    arn = f"arn:aws:ec2:us-east-2:{ACCT}:{rest}"
    cli.on(key, empty)
    assert check.is_stale(arn, None, "us-east-2") is True
    cli.on(key, live)
    assert check.is_stale(arn, None, "us-east-2") is False


def test_unlisted_ec2_kinds_and_plain_names_are_never_stale() -> None:
    assert check.is_stale(f"arn:aws:ec2:us-east-2:{ACCT}:volume/vol-1", None, "us-east-2") is False
    assert check.short("not-an-arn") == "not-an-arn"


def test_nodegroups_are_stale_when_missing(cli: FakeCli) -> None:
    cli.on("eks describe-nodegroup", {})
    arn = f"arn:aws:eks:us-east-2:{ACCT}:nodegroup/eksc-x/eksng-x/uuid"
    assert check.is_stale(arn, None, "us-east-2") is True
    cli.on("eks describe-nodegroup", {"nodegroup": {"status": "ACTIVE"}})
    assert check.is_stale(arn, None, "us-east-2") is False
    assert check.is_stale(f"arn:aws:eks:us-east-2:{ACCT}:addon/c/vpc-cni/uuid", None, "us-east-2") is False


def test_target_groups_follow_the_load_balancer_rule(cli: FakeCli) -> None:
    arn = f"arn:aws:elasticloadbalancing:us-east-2:{ACCT}:targetgroup/k8s-x/abc"
    cli.on("elbv2 describe-target-groups", {"TargetGroups": []})
    assert check.is_stale(arn, None, "us-east-2") is True
    cli.on("elbv2 describe-target-groups", {"TargetGroups": [{}]})
    assert check.is_stale(arn, None, "us-east-2") is False
    assert (
        check.is_stale(f"arn:aws:elasticloadbalancing:us-east-2:{ACCT}:listener/app/x/y/z", None, "us-east-2") is False
    )


def test_untagged_kinds_of_this_project_are_found_by_the_naming_convention(
    account: Account, capsys: pytest.CaptureFixture[str]
) -> None:
    account.responses["scheduler list-schedules"] = {
        "Schedules": [
            {"Name": "sch-useast2-oecalc-eks-nat-down-dev"},
            {"Name": "sch-useast2-oecalc-ecs-nat-dev"},
        ]
    }
    account.responses["iam list-roles"] = {
        "Roles": [
            {"RoleName": "iamr-useast2-oecalc-eks-nat-fnc-dev"},
            {"RoleName": "iamr-useast2-oecalc-eks-other-dev"},
            {"RoleName": "iamr-useast2-oecalc-ecs-task-dev"},
        ]
    }
    account.responses["iam list-role-tags"] = lambda command: (
        {"Tags": [{"Key": "repo-name", "Value": "someone-else"}]}
        if "other" in command[command.index("--role-name") + 1]
        else {"Tags": []}
    )
    account.responses["secretsmanager list-secrets"] = {
        "SecretList": [
            {"Name": "sm-useast2-oecalc-eks-app-dev"},
            {"Name": "sm-useast2-oecalc-eks-old-dev", "DeletedDate": 1},
            {"Name": "sm-useast2-oecalc-eks-foreign-dev", "Tags": [{"Key": "repo-name", "Value": "x"}]},
        ]
    }
    account.responses["logs describe-log-groups"] = {
        "logGroups": [
            {
                "logGroupName": "/aws/lambda/fnc-useast2-oecalc-eks-nat-dev",
                "arn": "arn:aws:logs:r:a:log-group:/aws/lambda/x:*",
            },
            {
                "logGroupName": "/aws/eks/eksc-useast2-oecalc-dev/cluster",
                "arn": "arn:aws:logs:r:a:log-group:/aws/eks/x:*",
            },
            {
                "logGroupName": "/aws/lambda/fnc-useast2-oecalc-ecs-x-dev",
                "arn": "arn:aws:logs:r:a:log-group:/y:*",
            },
        ]
    }
    account.responses["ssm get-parameters-by-path"] = {
        "Parameters": [{"Name": "/oecalc/dev/api-backends/eks/dns-name"}]
    }
    assert run_main() == 1
    out = capsys.readouterr().out
    assert "scheduler schedule: sch-useast2-oecalc-eks-nat-down-dev" in out
    assert "ecs" not in out
    assert "iam role: iamr-useast2-oecalc-eks-nat-fnc-dev" in out
    assert "other-dev" not in out
    assert "secret: sm-useast2-oecalc-eks-app-dev" in out
    assert "sm-useast2-oecalc-eks-old-dev" in out
    assert "(scheduled for deletion)" in out
    assert "foreign" not in out
    assert "log group: /aws/lambda/fnc-useast2-oecalc-eks-nat-dev" in out
    assert "log group: /aws/eks/eksc-useast2-oecalc-dev/cluster" in out
    assert "ssm parameter: /oecalc/dev/api-backends/eks/dns-name" in out


def test_a_check_without_permission_is_reported_as_skipped(
    account: Account, capsys: pytest.CaptureFixture[str]
) -> None:
    account.responses["scheduler list-schedules"] = Reply(returncode=254, stderr="AccessDeniedException")
    assert run_main() == 0
    assert "SKIPPED (no permission, not verified): scheduler" in capsys.readouterr().out


def test_a_failing_read_gives_exit_code_2(account: Account, capsys: pytest.CaptureFixture[str]) -> None:
    account.responses["secretsmanager list-secrets"] = Reply(returncode=1, stderr=f"Throttled for account {ACCT}")
    assert run_main() == 2
    out = capsys.readouterr().out
    assert "cannot complete the check" in out
    assert ACCT not in out


def test_retries_wait_and_check_again(account: Account, monkeypatch: pytest.MonkeyPatch) -> None:
    account.tagged = [TOPIC_ARN]
    sleeps: list[int] = []
    monkeypatch.setattr("time.sleep", sleeps.append)
    assert run_main("--retries", "3", "--wait", "7") == 1
    assert sleeps == [7, 7]


def test_a_profile_is_passed_to_the_cli(account: Account, cli: FakeCli) -> None:
    run_main("--profile", "p")
    assert all(c[-2:] == ["--profile", "p"] for c in cli.calls)


def test_the_check_only_makes_read_calls(account: Account, cli: FakeCli) -> None:
    account.tagged = [NAT_ARN]
    run_main()
    verbs = {c[2].split("-")[0] for c in cli.calls}
    assert verbs <= {"get", "describe", "list"}


def test_collect_reports_json_errors_as_exit_2(account: Account, cli: FakeCli) -> None:
    cli.on("resourcegroupstaggingapi get-resources", Reply("not json"))
    assert run_main() == 2


def test_no_aws_stdout_is_an_empty_answer(cli: FakeCli) -> None:
    cli.on("sts get-caller-identity", Reply(""))
    assert check.aws(["sts", "get-caller-identity"], None, "us-east-2") == {}


def test_project_name_rule() -> None:
    assert check.is_project_name("sm-useast2-oecalc-eks-app-dev", "oecalc", "-dev")
    assert check.is_project_name("/aws/eks/eksc-useast2-oecalc-dev/cluster", "oecalc", "-dev")
    assert not check.is_project_name("sm-useast2-oecalc-ecs-app-dev", "oecalc", "-dev")
    assert not check.is_project_name("sm-useast2-oecalc-eks-app-prod", "oecalc", "-dev")


# --------------------------------------------------------------------------- Destroy DEV workflow
WORKFLOW = REPO / ".github" / "workflows" / "destroy-dev.yml"


@pytest.fixture(scope="module")
def workflow() -> dict[str, Any]:
    loaded: dict[Any, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return loaded


def steps(workflow: dict[Any, Any]) -> dict[str, dict[str, Any]]:
    return {step["name"]: step for step in workflow["jobs"]["destroy"]["steps"]}


def test_workflow_is_manual_with_plan_as_default_and_a_typed_confirmation(workflow: dict[Any, Any]) -> None:
    inputs = workflow[True]["workflow_dispatch"]["inputs"]  # PyYAML reads the key `on` as True
    assert inputs["mode"]["default"] == "plan"
    assert inputs["mode"]["options"] == ["plan", "destroy"]
    assert set(workflow[True]) == {"workflow_dispatch"}
    validate = steps(workflow)["Validate the request"]
    assert '"$CONFIRM" != "$REPO_NAME"' in validate["run"]
    assert validate["env"]["REPO_NAME"] == "${{ github.event.repository.name }}"
    assert "refs/heads/develop" in validate["run"]


def test_destroy_step_requires_mode_destroy_and_plan_always_runs_first(workflow: dict[Any, Any]) -> None:
    named = steps(workflow)
    order = list(named)
    destroy = named["Destroy (CloudFormation)"]
    assert "inputs.mode == 'destroy'" in destroy["if"]
    assert "--mode destroy" in destroy["run"]
    plan = named["Plan the destruction (CloudFormation)"]
    assert "if" not in plan or "inputs.mode" not in plan["if"]
    assert "--mode plan" in plan["run"]
    assert order.index(plan["name"]) < order.index(destroy["name"])


def test_owner_guard_detect_runs_before_any_destruction(workflow: dict[Any, Any]) -> None:
    named = steps(workflow)
    assert "iac_owner_guard.py detect --env dev" in named["Detect the deployed owner"]["run"]
    order = list(named)
    assert order.index("Detect the deployed owner") < order.index("Plan the destruction (CloudFormation)")
    assert "== 'cloudformation'" in named["Destroy (CloudFormation)"]["if"]


def test_final_step_runs_the_leftover_check_with_the_repository_name(workflow: dict[Any, Any]) -> None:
    final = list(steps(workflow).values())[-1]
    assert final["name"] == "Verify nothing is left behind"
    assert final["if"] == "${{ inputs.mode == 'destroy' }}"
    assert 'scripts/post_destroy_check.py --repo-name "$REPO_NAME" --env dev' in final["run"]
    assert final["env"]["REPO_NAME"] == "${{ github.event.repository.name }}"


def test_workflow_shares_the_deploy_concurrency_group_and_pins_actions_by_sha(workflow: dict[Any, Any]) -> None:
    job = workflow["jobs"]["destroy"]
    assert job["concurrency"] == {"group": "deploy-dev", "cancel-in-progress": False}
    assert job["environment"] == "dev"
    uses = [step["uses"] for step in job["steps"] if "uses" in step]
    assert uses
    for action in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", action), action


def test_workflow_depends_on_nothing_from_the_ecs_repository() -> None:
    text = WORKFLOW.read_text(encoding="utf-8").lower()
    assert "ecs" not in text.replace("secrets", "")
    assert "terragrunt" not in text

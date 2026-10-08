"""Tests of scripts/cfn_deploy.py with a faked AWS and SAM command line (nothing reaches AWS)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import cfn_deploy as deploy
import pytest
from conftest import REPO, FakeCli, Reply

TAGS = {
    "team-owner": "nube-segura",
    "project-name": "over-engineered-calculator",
    "app-name": "calc-api",
    "repo-name": "over-engineered-simple-calculator-aws-eks-version",
    "env-type": "dev",
}
SCHEDULER_TEMPLATE = """\
Parameters:
  EnvType:
    Type: String
  Context:
    Type: String
    Default: oecalc
  SupportEmail:
    Type: String
  TagRepoName:
    Type: String
  FunctionTimeoutSeconds:
    Type: Number
    Default: 300
Resources:
  X:
    Type: AWS::SNS::Topic
"""


def change(action: str, resource_type: str, replacement: str | None = None) -> dict[str, Any]:
    detail: dict[str, Any] = {"Action": action, "ResourceType": resource_type, "LogicalResourceId": "X"}
    if replacement is not None:
        detail["Replacement"] = replacement
    return {"ResourceChange": detail}


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A small repository: security, network, scheduler and bastion (the last two only when enabled)."""
    (tmp_path / "templates").mkdir()
    (tmp_path / "parameters").mkdir()
    for name in ("security", "network", "scheduler", "bastion"):
        (tmp_path / "templates" / f"{name}.yaml").write_text(SCHEDULER_TEMPLATE, encoding="utf-8")

    def entry(unit: str, env: str, **extra: Any) -> dict[str, Any]:
        return {
            "unit": unit,
            "stack": f"stk-useast2-oecalc-eks-{unit}-{env}",
            "template": f"templates/{unit}.yaml",
            "sam": unit == "scheduler",
            **extra,
        }

    environments = {
        env: [
            entry("security", env),
            entry("network", env),
            entry("scheduler", env, onlyIf={"parameter": "NatSchedulerEnabled", "equals": "true"}),
            entry("bastion", env, onlyIf={"parameter": "EnableBastion", "equals": "true"}),
        ]
        for env in ("dev", "prod")
    }
    (tmp_path / "templates" / "stacks.json").write_text(json.dumps({"environments": environments}), encoding="utf-8")
    for env, enabled in (("dev", "true"), ("prod", "false")):
        config = {
            "Parameters": {
                "EnvType": env,
                "Context": "oecalc",
                "RegionCode": "useast2",
                "NatSchedulerEnabled": enabled,
                "EnableBastion": enabled,
            },
            "Tags": {**TAGS, "env-type": env},
        }
        (tmp_path / "parameters" / f"{env}.json").write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(deploy, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(deploy, "STACKS_FILE", tmp_path / "templates" / "stacks.json")
    monkeypatch.setattr(deploy, "BUILD_DIR", tmp_path / ".aws-sam")
    monkeypatch.setenv("SUPPORT_EMAIL", "owner@example.invalid")
    for variable in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_STEP_SUMMARY", "CFN_ALLOW_REPLACEMENT"):
        monkeypatch.delenv(variable, raising=False)
    return tmp_path


class World:
    """Scripted CloudFormation: stack statuses, change sets and what each stack holds."""

    def __init__(self, cli: FakeCli, statuses: dict[str, str] | None = None) -> None:
        self.cli = cli
        self.statuses: dict[str, str] = dict(statuses or {})
        self.change_set: dict[str, Any] = {
            "Status": "CREATE_COMPLETE",
            "Changes": [change("Add", "AWS::SNS::Topic")],
        }
        self.outputs: dict[str, dict[str, str]] = {}
        self.resources: dict[str, list[dict[str, Any]]] = {}
        self.templates: dict[str, dict[str, Any]] = {}
        self.protected: set[str] = set()
        self.delete_fails: set[str] = set()
        cli.on("cloudformation describe-stacks", self.describe_stacks)
        cli.on("cloudformation create-change-set", self.create_change_set)
        cli.on("cloudformation describe-change-set", lambda _a: self.change_set)
        cli.on("cloudformation execute-change-set", self.execute)
        cli.on("cloudformation delete-change-set", Reply())
        cli.on("cloudformation update-termination-protection", self.set_protection)
        cli.on("cloudformation list-stack-resources", self.list_resources)
        cli.on("cloudformation get-template", self.get_template)
        cli.on("cloudformation delete-stack", self.delete_stack)
        cli.on("cloudformation describe-stack-events", self.events)
        cli.on("sam build", Reply())
        cli.on("sam package", Reply())

    @staticmethod
    def stack_of(args: list[str]) -> str:
        return args[args.index("--stack-name") + 1]

    def describe_stacks(self, args: list[str]) -> Reply:
        stack = self.stack_of(args)
        if stack not in self.statuses:
            return Reply(returncode=254, stderr=f"Stack with id {stack} does not exist")
        outputs = [{"OutputKey": k, "OutputValue": v} for k, v in self.outputs.get(stack, {}).items()]
        body = {
            "Stacks": [
                {
                    "StackStatus": self.statuses[stack],
                    "Outputs": outputs,
                    "EnableTerminationProtection": stack in self.protected,
                }
            ]
        }
        return Reply(body)

    def create_change_set(self, args: list[str]) -> Reply:
        self.last_type = args[args.index("--change-set-type") + 1]
        self.last_stack = self.stack_of(args)
        return Reply({"Id": "cs"})

    def execute(self, args: list[str]) -> Reply:
        self.statuses[self.stack_of(args)] = "CREATE_COMPLETE" if self.last_type == "CREATE" else "UPDATE_COMPLETE"
        return Reply()

    def set_protection(self, args: list[str]) -> Reply:
        stack = self.stack_of(args)
        if "--enable-termination-protection" in args:
            self.protected.add(stack)
        else:
            self.protected.discard(stack)
        return Reply()

    def list_resources(self, args: list[str]) -> Reply:
        return Reply({"StackResourceSummaries": self.resources.get(self.stack_of(args), [])})

    def get_template(self, args: list[str]) -> Reply:
        return Reply({"TemplateBody": self.templates.get(self.stack_of(args), {})})

    def delete_stack(self, args: list[str]) -> Reply:
        stack = self.stack_of(args)
        self.statuses[stack] = "DELETE_FAILED" if stack in self.delete_fails else "DELETE_COMPLETE"
        if self.statuses[stack] == "DELETE_COMPLETE":
            del self.statuses[stack]
        return Reply()

    def events(self, _args: list[str]) -> Reply:
        return Reply(
            {
                "StackEvents": [
                    {
                        "LogicalResourceId": "Vpc",
                        "ResourceStatus": "DELETE_FAILED",
                        "ResourceStatusReason": "has dependencies",
                    },
                    {"LogicalResourceId": "Ok", "ResourceStatus": "DELETE_COMPLETE"},
                ]
            }
        )


# --------------------------------------------------------------------------- change set guard
def test_add_and_in_place_modify_pass() -> None:
    changes = [change("Add", "AWS::S3::Bucket"), change("Modify", "AWS::Lambda::Function", "False")]
    assert deploy.guard_changes(changes) == []


def test_remove_is_refused() -> None:
    assert deploy.guard_changes([change("Remove", "AWS::S3::Bucket")]) == ["Remove AWS::S3::Bucket X"]


@pytest.mark.parametrize("replacement", ["True", "Conditional"])
@pytest.mark.parametrize(
    "resource_type",
    [
        "AWS::RDS::DBInstance",
        "AWS::EC2::SecurityGroup",
        "AWS::EKS::Cluster",
        "AWS::KMS::Key",
        "AWS::S3::Bucket",
    ],
)
def test_replacement_is_refused_for_every_type_by_default(resource_type: str, replacement: str) -> None:
    violations = deploy.guard_changes([change("Modify", resource_type, replacement)])
    assert violations == [f"Replace ({replacement}) {resource_type} X"]


def test_allowlist_is_empty_by_default_and_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    assert not deploy.REPLACEMENT_ALLOWED_TYPES
    assert deploy.allowed_replacements(None) == frozenset()
    monkeypatch.setenv("CFN_ALLOW_REPLACEMENT", "AWS::Lambda::Function, AWS::SNS::Topic")
    allowed = deploy.allowed_replacements(["AWS::Logs::LogGroup"])
    assert allowed == {"AWS::Lambda::Function", "AWS::SNS::Topic", "AWS::Logs::LogGroup"}
    assert deploy.guard_changes([change("Modify", "AWS::SNS::Topic", "True")], allowed) == []


def test_change_set_name_is_unique_per_attempt_and_valid() -> None:
    first = deploy.change_set_name("123", "1", "network")
    assert first != deploy.change_set_name("123", "2", "network")
    assert deploy.change_set_name("1_2", "x y", "edge").replace("-", "").isalnum()


# --------------------------------------------------------------------------- deploy
def scheduler_outputs(world: World) -> None:
    world.outputs["stk-useast2-oecalc-eks-scheduler-dev"] = {"FunctionName": "fnc-useast2-oecalc-eks-nat-dev"}


def test_deploy_invokes_up_after_the_scheduler_stack_and_says_so(
    repo: Path, cli: FakeCli, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(cli)
    scheduler_outputs(world)
    world.outputs["stk-useast2-oecalc-eks-network-dev"] = {"DefaultSecurityGroupId": "sg-0abc"}
    cli.on("ec2 describe-security-group-rules", {"SecurityGroupRules": []})
    cli.on("ec2 create-tags", Reply())
    cli.on("lambda invoke", Reply("None\n"))
    assert deploy.main(["deploy", "--env", "dev"]) == 0
    (invoke,) = cli.called("lambda invoke")
    assert '"action":"up"' in invoke[invoke.index("--payload") + 1]
    assert '"wait":true' in invoke[invoke.index("--payload") + 1]
    created = cli.called("cloudformation create-change-set")
    scheduler_index = next(i for i, c in enumerate(cli.calls) if c in created and "scheduler" in str(c))
    assert cli.calls.index(invoke) > scheduler_index
    out = capsys.readouterr().out
    assert "the 22:00 schedule will still remove it" in out
    assert "outside 08:00-22:00" in out


def test_up_runs_again_when_the_scheduler_change_set_has_no_changes(repo: Path, cli: FakeCli) -> None:
    world = World(
        cli,
        {f"stk-useast2-oecalc-eks-{u}-dev": "CREATE_COMPLETE" for u in ("security", "network", "scheduler", "bastion")},
    )
    scheduler_outputs(world)
    world.outputs["stk-useast2-oecalc-eks-network-dev"] = {"DefaultSecurityGroupId": "sg-0abc"}
    world.change_set = {"Status": "FAILED", "StatusReason": "The submitted information didn't contain changes."}
    cli.on("ec2 describe-security-group-rules", {"SecurityGroupRules": []})
    cli.on("ec2 create-tags", Reply())
    cli.on("lambda invoke", Reply("None\n"))
    assert deploy.main(["deploy", "--env", "dev"]) == 0
    assert not cli.called("cloudformation execute-change-set")
    assert deploy.main(["deploy", "--env", "dev"]) == 0  # twice: the function is idempotent, the script just repeats
    assert len(cli.called("lambda invoke")) == 2


def test_prod_deploy_does_not_invoke_the_scheduler(repo: Path, cli: FakeCli) -> None:
    world = World(cli)
    world.outputs["stk-useast2-oecalc-eks-network-prod"] = {"DefaultSecurityGroupId": "sg-0abc"}
    cli.on("ec2 describe-security-group-rules", {"SecurityGroupRules": []})
    cli.on("ec2 create-tags", Reply())
    assert deploy.main(["deploy", "--env", "prod"]) == 0
    assert not cli.called("lambda invoke")


def test_a_failing_up_stops_the_deployment(repo: Path, cli: FakeCli, capsys: pytest.CaptureFixture[str]) -> None:
    world = World(cli)
    scheduler_outputs(world)
    world.outputs["stk-useast2-oecalc-eks-network-dev"] = {"DefaultSecurityGroupId": "sg-0abc"}
    cli.on("ec2 describe-security-group-rules", {"SecurityGroupRules": []})
    cli.on("ec2 create-tags", Reply())
    cli.on("lambda invoke", Reply("Unhandled\n"))
    assert deploy.main(["deploy", "--env", "dev"]) == 1
    assert "failed with action up" in capsys.readouterr().out


def test_deploy_creates_stacks_in_order_and_skips_disabled_units(
    repo: Path, cli: FakeCli, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(cli)
    scheduler_outputs(world)
    cli.on("lambda invoke", Reply("None\n"))
    world.outputs["stk-useast2-oecalc-eks-network-dev"] = {"DefaultSecurityGroupId": "sg-0abc"}
    cli.on(
        "ec2 describe-security-group-rules",
        {"SecurityGroupRules": [{"SecurityGroupRuleId": "sgr-1", "IsEgress": True}]},
    )
    cli.on("ec2 revoke-security-group-egress", Reply())
    cli.on("ec2 create-tags", Reply())
    assert deploy.main(["deploy", "--env", "dev"]) == 0
    created = [c[c.index("--stack-name") + 1] for c in cli.called("cloudformation create-change-set")]
    assert created == [
        "stk-useast2-oecalc-eks-security-dev",
        "stk-useast2-oecalc-eks-network-dev",
        "stk-useast2-oecalc-eks-scheduler-dev",
        "stk-useast2-oecalc-eks-bastion-dev",
    ]
    assert len(cli.called("cloudformation execute-change-set")) == 4
    assert [c[1] for c in cli.called("sam")] == ["build", "package"]
    assert cli.called("ec2 revoke-security-group-egress")
    out = capsys.readouterr().out
    assert "Next step in shared-resources" in out
    assert "owner@example.invalid" not in out


def test_deploy_through_stops_after_the_named_unit(repo: Path, cli: FakeCli) -> None:
    world = World(cli)
    scheduler_outputs(world)
    cli.on("lambda invoke", Reply("None\n"))
    world.outputs["stk-useast2-oecalc-eks-network-dev"] = {"DefaultSecurityGroupId": "sg-0abc"}
    cli.on("ec2 describe-security-group-rules", {"SecurityGroupRules": []})
    cli.on("ec2 create-tags", Reply())
    assert deploy.main(["deploy", "--env", "dev", "--through", "scheduler"]) == 0
    created = [c[c.index("--stack-name") + 1] for c in cli.called("cloudformation create-change-set")]
    assert created == [
        "stk-useast2-oecalc-eks-security-dev",
        "stk-useast2-oecalc-eks-network-dev",
        "stk-useast2-oecalc-eks-scheduler-dev",
    ]


def test_deploy_through_an_unknown_unit_is_an_error(repo: Path, cli: FakeCli) -> None:
    World(cli)
    assert deploy.main(["deploy", "--env", "dev", "--through", "nope"]) == 1
    assert not cli.called("cloudformation create-change-set")


def test_limit_stacks_keeps_everything_by_default() -> None:
    entries = [{"unit": "a"}, {"unit": "b"}]
    assert deploy.limit_stacks(entries, None) == entries
    assert deploy.limit_stacks(entries, "a") == [{"unit": "a"}]


def test_deploy_prod_skips_the_scheduler_and_protects_stacks(repo: Path, cli: FakeCli) -> None:
    world = World(cli)
    world.outputs["stk-useast2-oecalc-eks-network-prod"] = {"DefaultSecurityGroupId": "sg-0abc"}
    cli.on("ec2 describe-security-group-rules", {"SecurityGroupRules": []})
    cli.on("ec2 create-tags", Reply())
    assert deploy.main(["deploy", "--env", "prod"]) == 0
    created = [c[c.index("--stack-name") + 1] for c in cli.called("cloudformation create-change-set")]
    assert created == ["stk-useast2-oecalc-eks-security-prod", "stk-useast2-oecalc-eks-network-prod"]
    assert world.protected == set(created)


def test_deploy_updates_existing_stack_and_passes_parameters_and_tags(repo: Path, cli: FakeCli) -> None:
    world = World(cli, {"stk-useast2-oecalc-eks-security-dev": "CREATE_COMPLETE"})
    seen: dict[str, Any] = {}

    def create(args: list[str]) -> Reply:
        if "security" in args[args.index("--stack-name") + 1]:
            seen["type"] = args[args.index("--change-set-type") + 1]
            seen["params"] = json.loads(
                Path(args[args.index("--parameters") + 1].removeprefix("file://")).read_text("utf-8")
            )
            seen["tags"] = json.loads(Path(args[args.index("--tags") + 1].removeprefix("file://")).read_text("utf-8"))
        return world.create_change_set(args)

    cli.on("cloudformation create-change-set", create)
    entry = deploy.find_entry("dev", "security")
    deploy.deploy_stack("dev", entry, "1", "1", frozenset())
    assert seen["type"] == "UPDATE"
    assert {p["ParameterKey"] for p in seen["params"]} == {
        "EnvType",
        "Context",
        "SupportEmail",
        "TagRepoName",
    }
    assert {t["Key"] for t in seen["tags"]} == set(TAGS)


def test_deploy_with_no_changes_executes_nothing(repo: Path, cli: FakeCli, capsys: pytest.CaptureFixture[str]) -> None:
    world = World(cli, {"stk-useast2-oecalc-eks-security-dev": "CREATE_COMPLETE"})
    world.change_set = {
        "Status": "FAILED",
        "StatusReason": "The submitted information didn't contain changes.",
    }
    deploy.deploy_stack("dev", deploy.find_entry("dev", "security"), "1", "1", frozenset())
    assert "no changes" in capsys.readouterr().out
    assert cli.called("cloudformation delete-change-set")
    assert not cli.called("cloudformation execute-change-set")


def test_deploy_stops_on_a_failed_change_set(repo: Path, cli: FakeCli) -> None:
    world = World(cli)
    world.change_set = {"Status": "FAILED", "StatusReason": "Template format error"}
    with pytest.raises(deploy.DeployError, match="Template format error"):
        deploy.deploy_stack("dev", deploy.find_entry("dev", "security"), "1", "1", frozenset())


@pytest.mark.parametrize(
    ("bad", "label"),
    [
        (change("Remove", "AWS::S3::Bucket"), "Remove AWS::S3::Bucket"),
        (change("Modify", "AWS::EC2::SecurityGroup", "True"), "Replace (True) AWS::EC2::SecurityGroup"),
        (
            change("Modify", "AWS::RDS::DBInstance", "Conditional"),
            "Replace (Conditional) AWS::RDS::DBInstance",
        ),
    ],
)
def test_deploy_stops_before_executing_a_remove_or_replacement(
    repo: Path, cli: FakeCli, capsys: pytest.CaptureFixture[str], bad: dict[str, Any], label: str
) -> None:
    world = World(cli)
    world.change_set = {"Status": "CREATE_COMPLETE", "Changes": [change("Add", "AWS::SNS::Topic"), bad]}
    assert deploy.main(["deploy", "--env", "dev"]) == 1
    assert label in capsys.readouterr().out
    assert not cli.called("cloudformation execute-change-set")


def test_deploy_runs_an_allowlisted_replacement(repo: Path, cli: FakeCli) -> None:
    world = World(cli)
    scheduler_outputs(world)
    cli.on("lambda invoke", Reply("None\n"))
    world.outputs["stk-useast2-oecalc-eks-network-dev"] = {"DefaultSecurityGroupId": "sg-0abc"}
    world.change_set = {"Status": "CREATE_COMPLETE", "Changes": [change("Modify", "AWS::SNS::Topic", "True")]}
    cli.on(
        "ec2 describe-security-group-rules",
        {"SecurityGroupRules": [{"SecurityGroupRuleId": "sgr-1", "IsEgress": False}]},
    )
    cli.on("ec2 revoke-security-group-ingress", Reply())
    cli.on("ec2 create-tags", Reply())
    assert deploy.main(["deploy", "--env", "dev", "--allow-replacement", "AWS::SNS::Topic"]) == 0


@pytest.mark.parametrize("status", ["ROLLBACK_COMPLETE", "DELETE_FAILED", "UPDATE_IN_PROGRESS"])
def test_deploy_refuses_a_stack_in_a_bad_state(repo: Path, cli: FakeCli, status: str) -> None:
    World(cli, {"stk-useast2-oecalc-eks-security-dev": status})
    with pytest.raises(deploy.DeployError, match=status):
        deploy.deploy_stack("dev", deploy.find_entry("dev", "security"), "1", "1", frozenset())


def test_wait_stack_reports_failed_events(repo: Path, cli: FakeCli, capsys: pytest.CaptureFixture[str]) -> None:
    World(cli, {"s": "UPDATE_ROLLBACK_COMPLETE"})
    with pytest.raises(deploy.DeployError, match="UPDATE_ROLLBACK_COMPLETE"):
        deploy.wait_stack("s", ("UPDATE_COMPLETE",))
    assert "Vpc DELETE_FAILED: has dependencies" in capsys.readouterr().out


def test_wait_stack_polls_while_in_progress(repo: Path, cli: FakeCli) -> None:
    statuses = ["UPDATE_IN_PROGRESS", "UPDATE_IN_PROGRESS", "UPDATE_COMPLETE"]
    cli.on("cloudformation describe-stacks", lambda _a: {"Stacks": [{"StackStatus": statuses.pop(0)}]})
    deploy.wait_stack("s", ("UPDATE_COMPLETE",))
    assert statuses == []


def test_wait_stack_times_out(repo: Path, cli: FakeCli, monkeypatch: pytest.MonkeyPatch) -> None:
    cli.on("cloudformation describe-stacks", {"Stacks": [{"StackStatus": "UPDATE_IN_PROGRESS"}]})
    monkeypatch.setattr(deploy, "STACK_TIMEOUT_SECONDS", -1)
    with pytest.raises(deploy.DeployError, match="timed out"):
        deploy.wait_stack("s", ("UPDATE_COMPLETE",))


def test_describe_failure_other_than_missing_stack_is_an_error(repo: Path, cli: FakeCli) -> None:
    cli.on("cloudformation describe-stacks", Reply(returncode=255, stderr="AccessDenied"))
    with pytest.raises(deploy.DeployError, match="AccessDenied"):
        deploy.stack_status("s")


def test_change_set_pages_are_joined(repo: Path, cli: FakeCli) -> None:
    pages = [
        {"Status": "CREATE_COMPLETE", "Changes": [change("Add", "A")], "NextToken": "t"},
        {"Status": "CREATE_COMPLETE", "Changes": [change("Add", "B")]},
    ]
    cli.on("cloudformation describe-change-set", pages)
    assert len(deploy.describe_change_set("s", "c")["Changes"]) == 2


def test_wait_change_set_polls_until_complete(repo: Path, cli: FakeCli) -> None:
    cli.on(
        "cloudformation describe-change-set",
        [{"Status": "CREATE_PENDING"}, {"Status": "CREATE_COMPLETE", "Changes": []}],
    )
    page = deploy.wait_change_set("s", "c")
    assert page is not None
    assert page["Status"] == "CREATE_COMPLETE"


# --------------------------------------------------------------------------- parameters
def test_parameters_come_from_the_file_tags_and_environment(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CFN_PARAM_Context", "other")
    monkeypatch.setenv("CFN_PARAM_", "ignored")
    values = {
        p["ParameterKey"]: p["ParameterValue"]
        for p in deploy.stack_parameters("dev", repo / "templates" / "security.yaml")
    }
    assert values == {
        "EnvType": "dev",
        "Context": "other",
        "SupportEmail": "owner@example.invalid",
        "TagRepoName": TAGS["repo-name"],
    }


def test_a_parameter_without_value_or_default_is_an_error(repo: Path) -> None:
    (repo / "templates" / "extra.yaml").write_text("Parameters:\n  Nope:\n    Type: String\n", encoding="utf-8")
    with pytest.raises(deploy.DeployError, match="Nope"):
        deploy.stack_parameters("dev", repo / "templates" / "extra.yaml")


def test_parameter_file_of_another_environment_is_refused(repo: Path) -> None:
    path = repo / "parameters" / "dev.json"
    config = json.loads(path.read_text("utf-8"))
    config["Parameters"]["EnvType"] = "prod"
    path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(deploy.DeployError, match="must be 'dev'"):
        deploy.load_parameters("dev")


def test_params_command_hides_the_email(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert deploy.main(["params", "--env", "dev", "--unit", "security"]) == 0
    out = capsys.readouterr().out
    assert "SupportEmail=***" in out
    assert "owner@example.invalid" not in out


def test_unknown_unit_and_output_command(repo: Path, cli: FakeCli, capsys: pytest.CaptureFixture[str]) -> None:
    assert deploy.main(["output", "--env", "dev", "--unit", "nope", "--key", "K"]) == 1
    world = World(cli, {"stk-useast2-oecalc-eks-security-dev": "CREATE_COMPLETE"})
    world.outputs["stk-useast2-oecalc-eks-security-dev"] = {"K": "value"}
    assert deploy.main(["output", "--env", "dev", "--unit", "security", "--key", "K"]) == 0
    assert capsys.readouterr().out.strip().endswith("value")


def test_the_real_templates_resolve_with_the_real_parameter_files(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every template written so far gets a value for each parameter it declares, in both environments."""
    monkeypatch.setenv("SUPPORT_EMAIL", "owner@example.invalid")
    for env in ("dev", "prod"):
        for name in ("security", "network", "registry", "scheduler"):
            template = REPO / "templates" / f"{name}.yaml"
            names = {p["ParameterKey"] for p in deploy.stack_parameters(env, template)}
            assert {"EnvType", "RegionCode"} <= names, (env, name)


def test_scheduler_template_parameters_match_the_parameter_files() -> None:
    declared = deploy.template_parameters(REPO / "templates" / "scheduler.yaml")
    config = deploy.load_parameters("dev")["Parameters"]
    for name in ("NatSchedulerEnabled", "NatOffSchedule", "NatOnSchedule", "NatTimeZone", "LogRetentionDays"):
        assert name in declared
        assert name in config


# --------------------------------------------------------------------------- destroy
def bucket(physical: str) -> dict[str, Any]:
    return {"ResourceType": "AWS::S3::Bucket", "LogicalResourceId": "Ingest", "PhysicalResourceId": physical}


def repository(physical: str) -> dict[str, Any]:
    return {
        "ResourceType": "AWS::ECR::Repository",
        "LogicalResourceId": "Repo",
        "PhysicalResourceId": physical,
    }


def deployed_world(cli: FakeCli) -> World:
    names = ["security", "network", "scheduler"]
    world = World(cli, {f"stk-useast2-oecalc-eks-{n}-dev": "CREATE_COMPLETE" for n in names})
    world.outputs["stk-useast2-oecalc-eks-scheduler-dev"] = {"FunctionName": "fnc-useast2-oecalc-eks-nat-dev"}
    world.resources["stk-useast2-oecalc-eks-security-dev"] = [
        bucket("bckt-useast2-oecalc-eks-ingest-123-dev"),
        repository("ecr-useast2-oecalc-eks-dev"),
    ]
    world.templates["stk-useast2-oecalc-eks-security-dev"] = {"Resources": {"Repo": {"DeletionPolicy": "Retain"}}}
    world.protected.add("stk-useast2-oecalc-eks-network-dev")
    emptied = {"bucket": False, "repository": False}

    def list_versions(_args: list[str]) -> dict[str, Any]:
        if emptied["bucket"]:
            return {}
        return {
            "Versions": [{"Key": "a", "VersionId": "1"}],
            "DeleteMarkers": [{"Key": "b", "VersionId": "2"}],
        }

    def delete_objects(_args: list[str]) -> dict[str, Any]:
        emptied["bucket"] = True
        return {}

    def list_images(_args: list[str]) -> dict[str, Any]:
        return {"imageIds": [] if emptied["repository"] else [{"imageTag": "x"}]}

    def delete_images(_args: list[str]) -> Reply:
        emptied["repository"] = True
        return Reply()

    cli.on("s3api list-object-versions", list_versions)
    cli.on("s3api delete-objects", delete_objects)
    cli.on("ecr list-images", list_images)
    cli.on("ecr batch-delete-image", delete_images)
    cli.on("lambda invoke", Reply("None\n"))
    return world


def test_destroy_plan_changes_nothing_and_lists_blockers(
    repo: Path, cli: FakeCli, capsys: pytest.CaptureFixture[str]
) -> None:
    deployed_world(cli)
    assert deploy.main(["destroy", "--env", "dev", "--mode", "plan"]) == 0
    out = capsys.readouterr().out
    assert "Stacks present (reverse deploy order): 3" in out
    assert "termination protection" in out
    assert "bucket bckt-useast2-oecalc-eks-ingest-123-dev is not empty" in out
    assert "repository ecr-useast2-oecalc-eks-dev holds images (emptied by destroy)" in out
    assert "Retained on delete: Repo (Retain)" in out
    assert not cli.called("cloudformation delete-stack")
    assert not cli.called("lambda invoke")


def test_destroy_deletes_in_reverse_order_and_removes_the_nat_first(
    repo: Path, cli: FakeCli, capsys: pytest.CaptureFixture[str]
) -> None:
    world = deployed_world(cli)
    assert deploy.main(["destroy", "--env", "dev", "--mode", "destroy"]) == 0
    deleted = [c[c.index("--stack-name") + 1] for c in cli.called("cloudformation delete-stack")]
    assert deleted == [f"stk-useast2-oecalc-eks-{n}-dev" for n in ("scheduler", "network", "security")]
    invoke = cli.called("lambda invoke")[0]
    assert '"action":"down"' in invoke[invoke.index("--payload") + 1]
    assert cli.calls.index(invoke) < cli.calls.index(cli.called("cloudformation delete-stack")[0])
    assert world.protected == set()
    assert cli.called("s3api delete-objects")
    assert cli.called("ecr batch-delete-image")
    assert world.statuses == {}
    assert "Helm uninstall for dev: not implemented yet (task 7.1)" in capsys.readouterr().out


def test_destroy_skips_emptying_repositories_with_empty_on_delete(repo: Path, cli: FakeCli) -> None:
    world = deployed_world(cli)
    world.templates["stk-useast2-oecalc-eks-security-dev"] = {
        "Resources": {"Repo": {"Properties": {"EmptyOnDelete": True}}}
    }
    assert deploy.main(["destroy", "--env", "dev", "--mode", "destroy"]) == 0
    assert not cli.called("ecr batch-delete-image")


def test_destroy_with_nothing_deployed_ends_green(repo: Path, cli: FakeCli, capsys: pytest.CaptureFixture[str]) -> None:
    World(cli)
    assert deploy.main(["destroy", "--env", "dev", "--mode", "destroy"]) == 0
    assert "Nothing is deployed" in capsys.readouterr().out
    assert not cli.called("cloudformation delete-stack")
    assert not cli.called("lambda invoke")


def test_destroy_without_the_scheduler_stack_does_not_need_the_nat_function(repo: Path, cli: FakeCli) -> None:
    World(cli, {"stk-useast2-oecalc-eks-security-dev": "CREATE_COMPLETE"})
    assert deploy.main(["destroy", "--env", "dev", "--mode", "destroy"]) == 0
    assert not cli.called("lambda invoke")


def test_destroy_stops_on_delete_failed_and_shows_the_events(
    repo: Path, cli: FakeCli, capsys: pytest.CaptureFixture[str]
) -> None:
    world = deployed_world(cli)
    world.delete_fails.add("stk-useast2-oecalc-eks-network-dev")
    assert deploy.main(["destroy", "--env", "dev", "--mode", "destroy"]) == 1
    out = capsys.readouterr().out
    assert "Vpc DELETE_FAILED: has dependencies" in out
    assert "ended in DELETE_FAILED" in out
    deleted = [c[c.index("--stack-name") + 1] for c in cli.called("cloudformation delete-stack")]
    assert "stk-useast2-oecalc-eks-security-dev" not in deleted


def test_destroy_refuses_to_empty_a_bucket_of_another_project(repo: Path, cli: FakeCli) -> None:
    world = deployed_world(cli)
    world.resources["stk-useast2-oecalc-eks-security-dev"] = [bucket("bckt-useast2-oecalc-ecs-ingest-123-dev")]
    assert deploy.main(["destroy", "--env", "dev", "--mode", "destroy"]) == 1
    assert not cli.called("s3api delete-objects")


def test_destroy_is_for_dev_only(repo: Path) -> None:
    with pytest.raises(deploy.DeployError, match="only supported for dev"):
        deploy.command_destroy(type("A", (), {"env": "prod", "mode": "destroy"})())


def test_the_nat_function_failure_stops_the_destroy(repo: Path, cli: FakeCli) -> None:
    deployed_world(cli)
    cli.on("lambda invoke", Reply("Unhandled\n"))
    assert deploy.main(["destroy", "--env", "dev", "--mode", "destroy"]) == 1
    assert not cli.called("cloudformation delete-stack")


def test_empty_bucket_stops_on_delete_errors(cli: FakeCli) -> None:
    cli.on("s3api list-object-versions", {"Versions": [{"Key": "a.csv", "VersionId": "v1"}]})
    cli.on("s3api delete-objects", {"Errors": [{"Key": "a.csv", "Code": "AccessDenied", "Message": "denied"}]})
    with pytest.raises(deploy.DeployError, match="AccessDenied"):
        deploy.empty_bucket("b")


def test_empty_bucket_returns_zero_when_the_bucket_is_gone(cli: FakeCli) -> None:
    cli.on("s3api list-object-versions", Reply(returncode=255, stderr="NoSuchBucket"))
    assert deploy.empty_bucket("b") == 0


def test_summary_lists_outputs_and_the_shared_resources_step() -> None:
    lines = deploy.summary_lines("dev", {"stk-a": {"B": "2", "A": "1"}})
    assert lines.index("  - A: 1") < lines.index("  - B: 2")
    assert any("shared-resources" in line for line in lines)


def test_step_summary_file_is_appended(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = repo / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(target))
    deploy.write_step_summary(["one", "two"])
    assert target.read_text("utf-8") == "one\ntwo\n"


def test_command_failure_message_is_short(cli: FakeCli) -> None:
    cli.on("boom", Reply(returncode=1, stderr="x" * 5000))
    with pytest.raises(deploy.DeployError) as raised:
        deploy.run(["aws", "boom"])
    assert len(str(raised.value)) < 2100


def test_step_summary_redacts_account_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(target))
    deploy.write_step_summary(["key arn:aws:kms:us-east-2:123456789012:key/abc", "bucket bckt-x-123456789012-dev"])
    text = target.read_text(encoding="utf-8")
    assert "123456789012" not in text
    assert "arn:aws:kms:us-east-2:***:key/abc" in text

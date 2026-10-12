"""Template tests for templates/scheduler.yaml (CloudFormation tags are kept as data)."""

from pathlib import Path
from typing import Any

import pytest
import yaml

TEMPLATE = Path(__file__).resolve().parents[3] / "templates" / "scheduler.yaml"
READ_ONLY_EC2 = {"ec2:DescribeNatGateways", "ec2:DescribeRouteTables"}


def _tag(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.ScalarNode):
        value: Any = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        assert isinstance(node, yaml.MappingNode)
        value = loader.construct_mapping(node, deep=True)
    return {suffix if suffix == "Condition" else f"Fn::{suffix}": value}


class _Loader(yaml.SafeLoader):  # type: ignore[misc]
    pass


_Loader.add_multi_constructor("!", _tag)


@pytest.fixture(scope="module")
def template() -> dict[str, Any]:
    loaded: dict[str, Any] = yaml.load(TEMPLATE.read_text(encoding="utf-8"), Loader=_Loader)  # noqa: S506
    return loaded


def _statements(template: dict[str, Any]) -> list[dict[str, Any]]:
    policy = template["Resources"]["FunctionRolePolicy"]["Properties"]["PolicyDocument"]
    statements: list[dict[str, Any]] = policy["Statement"]
    return statements


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def test_function_role_has_no_ec2_wildcard_action_or_resource(template: dict[str, Any]) -> None:
    for statement in _statements(template):
        for action in _as_list(statement["Action"]):
            assert action != "*"
            assert action not in ("ec2:*", "ec2:Describe*")
        ec2_actions = {a for a in _as_list(statement["Action"]) if a.startswith("ec2:")}
        if ec2_actions and statement["Resource"] == "*":
            assert ec2_actions <= READ_ONLY_EC2, statement["Sid"]


def test_mutating_ec2_actions_are_scoped_by_arn_and_tags(template: dict[str, Any]) -> None:
    by_sid = {s["Sid"]: s for s in _statements(template)}
    delete = by_sid["DeleteTheTaggedNatGateway"]
    assert set(delete["Condition"]["StringEquals"]) == {
        "aws:ResourceTag/Name",
        "aws:ResourceTag/env-type",
        "aws:ResourceTag/repo-name",
    }
    create = by_sid["CreateTheTaggedNatGateway"]
    assert "aws:RequestTag/repo-name" in create["Condition"]["StringEquals"]
    route = by_sid["ManageThePrivateDefaultRoute"]
    assert "route-table/" in str(route["Resource"])
    assert "PrivateRouteTableId" in str(route["Resource"])
    assert set(_as_list(route["Action"])) == {"ec2:CreateRoute", "ec2:ReplaceRoute", "ec2:DeleteRoute"}


def test_kms_is_granted_by_alias_and_service_only(template: dict[str, Any]) -> None:
    statement = next(s for s in _statements(template) if s["Sid"] == "UseTheDataKeyThroughSns")
    assert "kms:ResourceAliases" in str(statement["Condition"])
    assert "kms:ViaService" in str(statement["Condition"])
    assert not {a for a in _as_list(statement["Action"]) if "Alias" in a}


def test_schedules_exist_only_when_enabled_and_target_this_function(template: dict[str, Any]) -> None:
    resources = template["Resources"]
    for name, action in (("DownSchedule", "down"), ("UpSchedule", "up")):
        assert resources[name]["Condition"] == "SchedulesEnabled"
        assert resources[name]["Properties"]["Target"]["Input"] == f'{{"action":"{action}"}}'
        assert resources[name]["Properties"]["ScheduleExpressionTimezone"] == {"Fn::Ref": "NatTimeZone"}
    assert resources["SchedulerRole"]["Condition"] == "SchedulesEnabled"
    assert template["Conditions"]["SchedulesEnabled"] == {"Fn::Equals": [{"Fn::Ref": "NatSchedulerEnabled"}, "true"]}


def test_failure_notification_and_alarm_use_the_alerts_topic(template: dict[str, Any]) -> None:
    alarm = template["Resources"]["ErrorsAlarm"]["Properties"]
    assert alarm["MetricName"] == "Errors"
    assert "AlertsTopicArn" in str(alarm["AlarmActions"])
    function = template["Resources"]["NatSchedulerFunction"]["Properties"]
    assert "AlertsTopicArn" in str(function["EventInvokeConfig"]["DestinationConfig"]["OnFailure"])
    assert function["Runtime"] == "python3.14"
    assert function["Architectures"] == ["arm64"]


def test_names_say_eks_and_stacks_come_from_the_eks_network(template: dict[str, Any]) -> None:
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "-eks-network-" in text
    assert "-ecs-" not in text
    for name in (
        "FunctionRole",
        "SchedulerRole",
        "NatSchedulerFunction",
        "DownSchedule",
        "UpSchedule",
        "ErrorsAlarm",
    ):
        properties = template["Resources"][name]["Properties"]
        label = (
            properties.get("RoleName")
            or properties.get("FunctionName")
            or properties.get("Name")
            or properties.get("AlarmName")
        )
        assert "-eks-" in str(label)


def test_keep_nat_parameter_is_created_false_and_read_by_the_function(template: dict[str, Any]) -> None:
    resource = template["Resources"]["KeepNatParameter"]
    assert resource["Type"] == "AWS::SSM::Parameter"
    properties = resource["Properties"]
    assert properties["Value"] == "false"  # literal: changing it would reset the owner's value
    assert properties["Type"] == "String"
    assert properties["Tier"] == "Standard"
    assert "eks/nat-keep-up" in str(properties["Name"])
    statement = next(s for s in _statements(template) if s["Sid"] == "ReadTheKeepNatParameter")
    assert statement["Action"] == "ssm:GetParameter"
    assert "KeepNatParameter" in str(statement["Resource"])
    function = template["Resources"]["NatSchedulerFunction"]["Properties"]
    assert function["Environment"]["Variables"]["KEEP_UP_PARAMETER"] == {"Fn::Ref": "KeepNatParameter"}
    assert not any("ssm:Put" in str(s["Action"]) for s in _statements(template))

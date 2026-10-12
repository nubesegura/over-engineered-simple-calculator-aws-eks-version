"""Template tests of the alarms stack (tasks 5.5 and 5.6): YAML load, no AWS."""

from __future__ import annotations

import json
from typing import Any

import cfn_deploy
import pytest
from test_templates_m2 import REPO, as_list, env_parameters, load_cfn, of_type

ENVS = ["dev", "prod"]
TOPIC_IMPORT = "AlertsTopicArn"


@pytest.fixture(scope="module")
def alarms() -> dict[str, Any]:
    return load_cfn("alarms.yaml")


def alarm_resources(template: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {name: r["Properties"] for name, r in of_type(template, "AWS::CloudWatch::Alarm").items()}


def test_every_alarm_notifies_the_alerts_topic_and_follows_the_naming(alarms: dict[str, Any]) -> None:
    found = alarm_resources(alarms)
    assert len(found) >= 17  # the rotation and NAT scheduler alarms (2) live in their own stacks
    names = set()
    for logical, props in found.items():
        (action,) = props["AlarmActions"]
        assert TOPIC_IMPORT in json.dumps(action) and "-security-" in json.dumps(action), logical
        name = props["AlarmName"]["Fn::Sub"]
        assert name.startswith("alm-${RegionCode}-${Context}-eks-") and name.endswith("-${EnvType}"), logical
        assert props["Dimensions"] and props["TreatMissingData"], logical
        names.add(name)
    assert len(names) == len(found)  # no two alarms share a name


def test_the_requested_alarms_exist_with_the_agreed_metrics(alarms: dict[str, Any]) -> None:
    found = alarm_resources(alarms)
    metrics = {(p["Namespace"], p["MetricName"]) for p in found.values()}
    assert {
        ("AWS/AutoScaling", "GroupInServiceInstances"),
        ("AWS/ApplicationELB", "HealthyHostCount"),
        ("AWS/ApplicationELB", "HTTPCode_ELB_5XX_Count"),
        ("AWS/RDS", "FreeStorageSpace"),
        ("AWS/RDS", "DatabaseConnections"),
        ("AWS/RDS", "CPUUtilization"),
        ("AWS/RDS", "ClientConnectionsSetupFailedAuth"),
        ("AWS/SQS", "ApproximateNumberOfMessagesVisible"),
        ("AWS/CodeBuild", "FailedBuilds"),
    } <= metrics
    nodes = found["NodesInServiceAlarm"]
    assert nodes["Threshold"] == {"Ref": "NodeMinSize"} and nodes["ComparisonOperator"] == "LessThanThreshold"
    dlq = found["DeadLetterQueueAlarm"]
    assert dlq["Threshold"] == 0 and dlq["ComparisonOperator"] == "GreaterThanThreshold"
    assert "DeadLetterQueueName" in json.dumps(dlq["Dimensions"])
    alb = found["LoadBalancer5xxAlarm"]
    assert alb["Period"] == 300 and alb["EvaluationPeriods"] == 1
    assert alb["ComparisonOperator"] == "GreaterThanOrEqualToThreshold"


def test_each_workload_has_its_own_healthy_host_alarm_that_fires_below_one(alarms: dict[str, Any]) -> None:
    found = alarm_resources(alarms)
    for workload in ("Add", "Sub", "Mul", "Div", "History"):
        props = found[f"HealthyHosts{workload}Alarm"]
        assert props["Threshold"] == 1 and props["ComparisonOperator"] == "LessThanThreshold"
        assert props["Statistic"] == "Minimum" and props["TreatMissingData"] == "breaching"
        dims = {d["Name"]: d["Value"] for d in props["Dimensions"]}
        assert dims == {
            "TargetGroup": {"Ref": f"TargetGroup{workload}"},
            "LoadBalancer": {"Ref": "LoadBalancerDimension"},
        }


def test_dimensions_and_thresholds_are_parameters_and_the_values_are_in_both_environment_files(
    alarms: dict[str, Any],
) -> None:
    parameters = alarms["Parameters"]
    discovered = {
        "AutoScalingGroupName",
        "LoadBalancerDimension",
        "TargetGroupAdd",
        "TargetGroupSub",
        "TargetGroupMul",
        "TargetGroupDiv",
        "TargetGroupHistory",
        "ReminderMessageJson",
    }
    assert discovered <= set(parameters)
    assert all("Default" not in parameters[name] for name in discovered)  # a missing value stops the deployment
    for env in ENVS:
        values = env_parameters(env)
        thresholds = set(parameters) - discovered - {"EnvType", "Context", "RegionCode"}
        assert thresholds <= set(values), (env, thresholds - set(values))


def test_deploy_script_refuses_the_stack_when_a_discovered_dimension_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("AutoScalingGroupName", "ReminderMessageJson", "TargetGroupAdd"):
        monkeypatch.delenv(f"CFN_PARAM_{name}", raising=False)
    with pytest.raises(cfn_deploy.DeployError, match="no value for parameters"):
        cfn_deploy.stack_parameters("dev", REPO / "templates" / "alarms.yaml")


def test_health_rule_matches_only_eks_health_events_and_targets_the_topic(alarms: dict[str, Any]) -> None:
    (rule,) = of_type(alarms, "AWS::Events::Rule").values()
    props = rule["Properties"]
    assert props["EventPattern"] == {
        "source": ["aws.health"],
        "detail-type": ["AWS Health Event"],
        "detail": {"service": ["EKS"]},
    }
    assert props["State"] == "ENABLED"
    assert props["Name"]["Fn::Sub"] == "evr-${RegionCode}-${Context}-eks-health-${EnvType}"
    (target,) = props["Targets"]
    assert TOPIC_IMPORT in json.dumps(target["Arn"])
    assert "RoleArn" not in target  # the topic policy lets EventBridge rules of this account publish
    assert target["RetryPolicy"]["MaximumRetryAttempts"] == 8
    assert target["DeadLetterConfig"]["Arn"] == {"Fn::GetAtt": ["HealthEventsDeadLetterQueue", "Arn"]}
    queue = alarms["Resources"]["HealthEventsDeadLetterQueue"]["Properties"]
    assert "DataKeyAlias" in json.dumps(queue["KmsMasterKeyId"])
    policy = alarms["Resources"]["HealthEventsDeadLetterQueuePolicy"]["Properties"]["PolicyDocument"]["Statement"]
    only_rule = next(s for s in policy if s["Sid"] == "OnlyThisRuleSendsFailedEvents")
    assert only_rule["Condition"]["ArnEquals"]["aws:SourceArn"] == {"Fn::GetAtt": ["HealthRule", "Arn"]}
    assert any(s["Effect"] == "Deny" and s["Condition"]["Bool"]["aws:SecureTransport"] == "false" for s in policy)


def test_reminder_runs_every_seven_days_and_publishes_with_a_role_limited_to_the_topic(alarms: dict[str, Any]) -> None:
    (schedule,) = of_type(alarms, "AWS::Scheduler::Schedule").values()
    props = schedule["Properties"]
    assert props["ScheduleExpression"] == "rate(7 days)"
    assert props["Target"]["Arn"]["Fn::Sub"].endswith(":scheduler:::aws-sdk:sns:publish")
    text = props["Target"]["Input"]["Fn::Sub"][0]
    assert all(key in text for key in ('"TopicArn"', '"Subject"', '"Message":${ReminderMessageJson}'))
    assert TOPIC_IMPORT in json.dumps(props["Target"]["Input"]["Fn::Sub"][1])
    assert props["Target"]["RoleArn"] == {"Fn::GetAtt": ["ReminderRole", "Arn"]}
    assert schedule["DependsOn"] == "ReminderRolePolicy"


def test_reminder_role_trusts_only_the_scheduler_and_publishes_only_to_the_topic(alarms: dict[str, Any]) -> None:
    role = alarms["Resources"]["ReminderRole"]["Properties"]
    (trust,) = role["AssumeRolePolicyDocument"]["Statement"]
    assert trust["Principal"] == {"Service": "scheduler.amazonaws.com"}
    assert "aws:SourceAccount" in trust["Condition"]["StringEquals"]
    statements = alarms["Resources"]["ReminderRolePolicy"]["Properties"]["PolicyDocument"]["Statement"]
    publish = next(s for s in statements if s["Sid"] == "PublishOnlyToTheAlertsTopic")
    assert publish["Action"] == "sns:Publish" and TOPIC_IMPORT in json.dumps(publish["Resource"])
    key = next(s for s in statements if s["Sid"] == "UseTheDataKeyThroughSns")
    assert set(as_list(key["Action"])) == {"kms:GenerateDataKey", "kms:Decrypt"}
    assert "kms:ResourceAliases" in json.dumps(key["Condition"]) and "sns." in json.dumps(key["Condition"])


def test_alarms_is_the_last_unit_and_runs_after_the_release_in_the_workflow() -> None:
    for env in ENVS:
        units = [e["unit"] for e in cfn_deploy.load_stacks(env)]
        assert units[-1] == "alarms"
    assert cfn_deploy.limit_stacks(cfn_deploy.load_stacks("dev"), None, "alarms") == [
        e for e in cfn_deploy.load_stacks("dev") if e["unit"] == "alarms"
    ]

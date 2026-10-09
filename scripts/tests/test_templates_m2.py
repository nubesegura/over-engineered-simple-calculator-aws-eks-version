"""Template tests of milestone 2: database and rotation stacks (YAML load, no AWS)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
TEMPLATES = REPO / "templates"
NAT_ON_START_UTC = 13  # 08:00 Panama (UTC-5)


def load_cfn(name: str) -> dict[str, Any]:
    """Load a CloudFormation YAML file; intrinsic short forms become {"Fn::X": value} like in JSON."""

    class Loader(yaml.SafeLoader):
        pass

    def construct(loader: yaml.SafeLoader, tag_suffix: str, node: yaml.Node) -> Any:
        if isinstance(node, yaml.ScalarNode):
            value: Any = loader.construct_scalar(node)
            if tag_suffix == "GetAtt":
                value = value.split(".", 1)
        elif isinstance(node, yaml.SequenceNode):
            value = loader.construct_sequence(node, deep=True)
        elif isinstance(node, yaml.MappingNode):
            value = loader.construct_mapping(node, deep=True)
        else:
            raise TypeError(f"unsupported YAML node for !{tag_suffix}")
        key = "Ref" if tag_suffix == "Ref" else f"Fn::{tag_suffix}"
        return {key: value}

    Loader.add_multi_constructor("!", construct)
    template: dict[str, Any] = yaml.load((TEMPLATES / name).read_text(encoding="utf-8"), Loader=Loader)  # noqa: S506
    return template


def of_type(template: dict[str, Any], resource_type: str) -> dict[str, dict[str, Any]]:
    return {k: v for k, v in template["Resources"].items() if v["Type"] == resource_type}


def statements(role_policy: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = role_policy["Properties"]["PolicyDocument"]["Statement"]
    return result


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


def env_parameters(env: str) -> dict[str, str]:
    data = json.loads((REPO / "parameters" / f"{env}.json").read_text(encoding="utf-8"))
    parameters: dict[str, str] = data["Parameters"]
    return parameters


@pytest.fixture(scope="module")
def database() -> dict[str, Any]:
    return load_cfn("database.yaml")


@pytest.fixture(scope="module")
def rotation() -> dict[str, Any]:
    return load_cfn("rotation.yaml")


# ---------------------------------------------------------------- database (task 2.2)


def test_parameter_group_forces_ssl(database: dict[str, Any]) -> None:
    groups = of_type(database, "AWS::RDS::DBParameterGroup")
    assert [g["Properties"]["Parameters"]["rds.force_ssl"] for g in groups.values()] == ["1"]


def test_two_instances_with_opposite_conditions(database: dict[str, Any]) -> None:
    instances = of_type(database, "AWS::RDS::DBInstance")
    assert {k: v["Condition"] for k, v in instances.items()} == {
        "DbInstanceProd": "IsProd",
        "DbInstanceDev": "IsDev",
    }


@pytest.mark.parametrize("name", ["DbInstanceProd", "DbInstanceDev"])
def test_instance_controls_in_both_branches(database: dict[str, Any], name: str) -> None:
    props = database["Resources"][name]["Properties"]
    assert props["StorageEncrypted"] is True
    assert props["PubliclyAccessible"] is False
    assert props["StorageType"] == "gp3"
    assert props["EnableIAMDatabaseAuthentication"] is False
    assert props["ManageMasterUserPassword"] is True
    assert "MasterUserPassword" not in props
    assert props["MasterUsername"] not in {"postgres", "admin", "root", "sa", "master", "mysql"}
    assert props["AutoMinorVersionUpgrade"] is True
    assert props["CopyTagsToSnapshot"] is True
    assert props["Port"] == "5432"
    assert props["DBInstanceClass"] == {"Ref": "InstanceClass"}
    assert props["DBParameterGroupName"] == {"Ref": "ParameterGroup"}
    assert props["VPCSecurityGroups"]
    assert database["Parameters"]["InstanceClass"]["Default"] == "db.t4g.micro"


def test_snapshot_and_protection_only_in_the_prod_branch(database: dict[str, Any]) -> None:
    prod = database["Resources"]["DbInstanceProd"]
    dev = database["Resources"]["DbInstanceDev"]
    assert prod["DeletionPolicy"] == "Snapshot" and prod["UpdateReplacePolicy"] == "Snapshot"
    assert dev["DeletionPolicy"] == "Delete" and dev["UpdateReplacePolicy"] == "Delete"
    assert prod["Properties"]["DeletionProtection"] == {"Ref": "DbDeletionProtection"}
    assert prod["Properties"]["MultiAZ"] == {"Ref": "DbMultiAz"}
    assert dev["Properties"]["DeletionProtection"] is False
    assert dev["Properties"]["MultiAZ"] is False


def test_prod_safeguards_rule_and_parameter_values(database: dict[str, Any]) -> None:
    rule = database["Rules"]["ProdSafeguards"]
    assert rule["RuleCondition"] == {"Fn::Equals": [{"Ref": "EnvType"}, "prod"]}
    assert len(rule["Assertions"]) == 4
    prod, dev = env_parameters("prod"), env_parameters("dev")
    assert (prod["DbMultiAz"], prod["DbDeletionProtection"], prod["EnableDbLogExports"]) == ("true",) * 3
    assert int(prod["DbBackupRetentionDays"]) == 15
    assert int(dev["DbBackupRetentionDays"]) == 1


def test_application_secret_has_no_host_and_a_generated_password(database: dict[str, Any]) -> None:
    secret = database["Resources"]["AppSecret"]["Properties"]
    assert "SecretString" not in secret
    generated = secret["GenerateSecretString"]
    assert generated["GenerateStringKey"] == "password"
    template = generated["SecretStringTemplate"]["Fn::Sub"]
    keys = set(json.loads(template.replace("${DbName}", "x").replace("${AppUsername}", "y")))
    assert keys == {"dbname", "username"}
    assert secret["Name"] == {"Fn::Sub": "sm-${RegionCode}-${Context}-eks-app-${EnvType}"}
    assert "KmsKeyId" in secret


def test_secret_policies_deny_insecure_transport(database: dict[str, Any]) -> None:
    policies = of_type(database, "AWS::SecretsManager::ResourcePolicy")
    assert set(policies) == {"AppSecretPolicy", "MasterSecretPolicy"}
    for policy in policies.values():
        deny = policy["Properties"]["ResourcePolicy"]["Statement"][0]
        assert deny["Effect"] == "Deny"
        assert deny["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}


def test_proxy_requires_tls_and_registers_the_application_secret(database: dict[str, Any]) -> None:
    proxy = database["Resources"]["Proxy"]["Properties"]
    assert proxy["RequireTLS"] is True
    assert proxy["EngineFamily"] == "POSTGRESQL"
    (auth,) = proxy["Auth"]
    assert auth["SecretArn"] == {"Ref": "AppSecret"}
    assert auth["IAMAuth"] == "DISABLED"
    assert auth["AuthScheme"] == "SECRETS"
    assert database["Resources"]["ProxyTargetGroup"]["Properties"]["TargetGroupName"] == "default"


def test_proxy_role_is_scoped_to_the_application_secret(database: dict[str, Any]) -> None:
    trust = database["Resources"]["ProxyRole"]["Properties"]["AssumeRolePolicyDocument"]["Statement"][0]
    assert trust["Principal"] == {"Service": "rds.amazonaws.com"}
    by_sid = {s["Sid"]: s for s in statements(database["Resources"]["ProxyRolePolicy"])}
    assert by_sid["ReadTheApplicationSecret"]["Resource"] == {"Ref": "AppSecret"}
    kms = by_sid["DecryptTheSecretWithTheDataKey"]
    assert kms["Action"] == "kms:Decrypt"
    assert "kms:ResourceAliases" in kms["Condition"]["ForAnyValue:StringEquals"]
    assert "kms:ViaService" in kms["Condition"]["StringEquals"]
    wildcards = [sid for sid, s in by_sid.items() if s["Resource"] == "*"]
    assert wildcards == ["DecryptTheSecretWithTheDataKey"]


def test_database_exports_follow_the_pattern(database: dict[str, Any]) -> None:
    outputs = database["Outputs"]
    assert {"ProxyEndpoint", "EndpointAddress", "AppSecretArn", "MasterSecretArn"} <= set(outputs)
    for name, output in outputs.items():
        assert output["Export"]["Name"] == {"Fn::Sub": f"${{AWS::StackName}}:{name}"}


def test_database_names_follow_the_convention(database: dict[str, Any]) -> None:
    resources = database["Resources"]
    proxy_name = resources["Proxy"]["Properties"]["DBProxyName"]["Fn::Sub"]
    assert proxy_name == "rdsp-${RegionCode}-${Context}-eks-${EnvType}"
    instance_name = resources["DbInstanceDev"]["Properties"]["DBInstanceIdentifier"]["Fn::Sub"]
    assert instance_name == "rds-${RegionCode}-${Context}-eks-${EnvType}"
    role_name = resources["ProxyRole"]["Properties"]["RoleName"]["Fn::Sub"]
    assert role_name == "iamr-${RegionCode}-${Context}-eks-proxy-${EnvType}"


# ---------------------------------------------------------------- rotation (task 2.3)


def test_function_runs_in_private_subnets_with_the_rotation_group(rotation: dict[str, Any]) -> None:
    function = rotation["Resources"]["RotationFunction"]["Properties"]
    assert function["Handler"] == "app.lambda_handler"
    assert function["Runtime"] == "python3.14"
    assert function["Architectures"] == ["arm64"]
    assert function["CodeUri"] == "../src/rotation"
    assert rotation["Resources"]["RotationFunction"]["Metadata"] == {"BuildMethod": "makefile"}
    assert "RotationSecurityGroupId" in json.dumps(function["VpcConfig"]["SecurityGroupIds"])
    assert "PrivateSubnetA" in json.dumps(function["VpcConfig"]["SubnetIds"])
    assert "ReservedConcurrentExecutions" not in function
    variables = function["Environment"]["Variables"]
    assert set(variables) == {
        "DB_HOST",
        "DB_PORT",
        "DB_NAME",
        "MASTER_SECRET_ARN",
        "SSL_ROOT_CERT",
        "LOG_LEVEL",
    }
    assert "EndpointAddress" in json.dumps(variables["DB_HOST"])
    assert "ProxyEndpoint" not in json.dumps(variables)


def test_permission_is_for_secrets_manager_with_the_account_as_source(rotation: dict[str, Any]) -> None:
    permissions = of_type(rotation, "AWS::Lambda::Permission")
    assert len(permissions) == 1
    props = next(iter(permissions.values()))["Properties"]
    assert props["Principal"] == "secretsmanager.amazonaws.com"
    assert props["SourceAccount"] == {"Ref": "AWS::AccountId"}
    assert props["Action"] == "lambda:InvokeFunction"


def test_no_wildcard_secret_resources(rotation: dict[str, Any]) -> None:
    checked = 0
    for statement in statements(rotation["Resources"]["FunctionRolePolicy"]):
        actions = as_list(statement["Action"])
        if not any(a.startswith("secretsmanager:") for a in actions):
            continue
        checked += 1
        assert not any(a.endswith("*") for a in actions)
        if actions == ["secretsmanager:GetRandomPassword"]:
            # This action has no resource-level permission: "*" is the only possible value.
            assert statement["Resource"] == "*"
        else:
            assert "*" not in json.dumps(statement["Resource"])
    assert checked == 3


def test_role_reaches_only_the_two_secrets_and_the_key_by_alias(rotation: dict[str, Any]) -> None:
    by_sid = {s["Sid"]: s for s in statements(rotation["Resources"]["FunctionRolePolicy"])}
    assert "AppSecretArn" in json.dumps(by_sid["RotateTheApplicationSecret"]["Resource"])
    assert "MasterSecretArn" in json.dumps(by_sid["ReadTheMasterSecret"]["Resource"])
    assert by_sid["ReadTheMasterSecret"]["Action"] == "secretsmanager:GetSecretValue"
    kms = by_sid["UseTheDataKeyThroughSecretsManager"]
    assert "kms:ResourceAliases" in kms["Condition"]["ForAnyValue:StringEquals"]
    assert "kms:ViaService" in kms["Condition"]["StringEquals"]
    forbidden = ("kms:CreateAlias", "kms:UpdateAlias", "kms:DeleteAlias", "iam:", "kms:*")
    assert not any(a.startswith(forbidden) for s in by_sid.values() for a in as_list(s["Action"]))


def test_both_secrets_rotate_every_three_days_inside_the_window(rotation: dict[str, Any]) -> None:
    schedules = of_type(rotation, "AWS::SecretsManager::RotationSchedule")
    assert set(schedules) == {"AppSecretRotation", "MasterSecretRotation"}
    for schedule in schedules.values():
        rules = schedule["Properties"]["RotationRules"]
        assert rules["Duration"] == {"Ref": "RotationWindowDuration"}
        days = [int(d) for d in rules["ScheduleExpression"]["Fn::Sub"].split()[2].split(",")]
        assert {b - a for a, b in zip(days, days[1:], strict=False)} == {3}
        assert schedule["Properties"]["RotateImmediatelyOnUpdate"] is False
    assert "RotationLambdaARN" in schedules["AppSecretRotation"]["Properties"]
    assert "RotationLambdaARN" not in schedules["MasterSecretRotation"]["Properties"]


@pytest.mark.parametrize("env", ["dev", "prod"])
def test_rotation_window_sits_inside_the_nat_on_hours(env: str) -> None:
    # The NAT gateway is up 08:00-22:00 Panama, that is 13:00-03:00 UTC; the window cannot cross midnight UTC.
    params = env_parameters(env)
    start = int(params["RotationWindowStartHourUtc"])
    end = start + int(params["RotationWindowDuration"].rstrip("h"))
    assert start >= NAT_ON_START_UTC
    assert end <= 24


def test_error_alarm_goes_to_the_alerts_topic(rotation: dict[str, Any]) -> None:
    alarm = rotation["Resources"]["ErrorsAlarm"]["Properties"]
    assert alarm["MetricName"] == "Errors"
    assert alarm["Namespace"] == "AWS/Lambda"
    assert "AlertsTopicArn" in json.dumps(alarm["AlarmActions"])


def test_proxy_log_group_is_owned_encrypted_and_created_before_the_proxy(database: dict[str, Any]) -> None:
    group = database["Resources"]["ProxyLogGroup"]
    assert group["DeletionPolicy"] == "Delete"
    assert group["UpdateReplacePolicy"] == "Delete"
    props = group["Properties"]
    assert props["LogGroupName"]["Fn::Sub"] == "/aws/rds/proxy/rdsp-${RegionCode}-${Context}-eks-${EnvType}"
    assert props["RetentionInDays"] == {"Ref": "LogRetentionDays"}
    assert "DataKeyArn" in json.dumps(props["KmsKeyId"])
    proxy = database["Resources"]["Proxy"]
    assert "ProxyLogGroup" in proxy["DependsOn"]
    assert proxy["Properties"]["DBProxyName"]["Fn::Sub"].endswith(props["LogGroupName"]["Fn::Sub"].split("/")[-1])

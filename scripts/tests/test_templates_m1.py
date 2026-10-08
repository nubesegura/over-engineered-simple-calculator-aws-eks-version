"""Template tests of milestone 1: security, network and registry stacks (YAML load, no AWS)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
TEMPLATES = REPO / "templates"

# Frozen on purpose: a security group description is replacement-forcing. If this test fails, the edit
# would replace the group (and everything attached to it). Do not "fix" the test; revert the template.
FROZEN_SG_DESCRIPTIONS = {
    "AlbSecurityGroup": "Load balancer: HTTPS and HTTP from the internet, egress to the pods and to the token issuer keys",  # noqa: E501
    "NodeSecurityGroup": "Nodes and pods: pod port from the load balancer, egress 443, DNS and the database path",
    "DatabaseSecurityGroup": "PostgreSQL: database port only from the proxy, the nodes and the rotation function",
    "ProxySecurityGroup": "RDS Proxy: database port from the nodes, egress to the database only",
    "CodeBuildSecurityGroup": "Deployment build: no ingress, egress 443 (cluster endpoint, ECR, SSM, S3 through the NAT)",  # noqa: E501
    "BastionSecurityGroup": "Bastion: no ingress (Session Manager only), egress 443",
    "RotationSecurityGroup": "Rotation function: no ingress, egress 443 and the database",
}


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
        else:
            value = loader.construct_mapping(node, deep=True)
        key = "Ref" if tag_suffix == "Ref" else f"Fn::{tag_suffix}"
        return {key: value}

    Loader.add_multi_constructor("!", construct)
    template: dict[str, Any] = yaml.load((TEMPLATES / name).read_text(encoding="utf-8"), Loader=Loader)  # noqa: S506
    return template


def resources_of(template: dict[str, Any], resource_type: str) -> dict[str, dict[str, Any]]:
    return {k: v for k, v in template["Resources"].items() if v["Type"] == resource_type}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else [value]


# ---------------------------------------------------------------- security (task 1.2)


@pytest.fixture(scope="module")
def security() -> dict[str, Any]:
    return load_cfn("security.yaml")


def test_keys_rotate_and_are_enabled(security: dict[str, Any]) -> None:
    keys = resources_of(security, "AWS::KMS::Key")
    assert len(keys) == 2
    for key in keys.values():
        assert key["Properties"]["EnableKeyRotation"] is True
        assert key["Properties"]["Enabled"] is True
        assert key["Properties"]["PendingWindowInDays"] == {"Ref": "KmsDeletionWindowDays"}


def test_aliases_follow_the_naming_convention(security: dict[str, Any]) -> None:
    aliases = {
        k: v["Properties"]["AliasName"]["Fn::Sub"]
        for k, v in resources_of(security, "AWS::KMS::Alias").items()
    }
    assert aliases == {
        "DataKeyAlias": "alias/kms-${RegionCode}-${Context}-eks-${EnvType}",
        "SecretsKeyAlias": "alias/kms-${RegionCode}-${Context}-eks-secrets-${EnvType}",
    }


def test_key_policy_has_the_kms06_administration_statement(security: dict[str, Any]) -> None:
    required = {
        "kms:Describe*", "kms:PutKeyPolicy", "kms:Create*", "kms:Update*", "kms:Enable*", "kms:RevokeGrant",
        "kms:List*", "kms:Disable*", "kms:Get*", "kms:Delete*", "kms:ScheduleKeyDeletion",
        "kms:CancelKeyDeletion", "kms:TagResource", "kms:UntagResource", "kms:RetireGrant",
    }  # fmt: skip
    for key in resources_of(security, "AWS::KMS::Key").values():
        statements = {s["Sid"]: s for s in key["Properties"]["KeyPolicy"]["Statement"]}
        admin = statements["KeyAdministration"]
        assert required <= set(admin["Action"])
        assert not any(
            a.startswith(("kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey")) for a in admin["Action"]
        )
        assert "role/${KeyAdminRoleName}" in admin["Principal"]["AWS"]["Fn::Sub"]
        assert statements["EnableIamPolicies"]["Principal"]["AWS"]["Fn::Sub"].endswith(":root")


def documents(template: dict[str, Any]) -> list[dict[str, Any]]:
    docs = [k["Properties"]["KeyPolicy"] for k in resources_of(template, "AWS::KMS::Key").values()]
    docs += [
        t["Properties"]["PolicyDocument"] for t in resources_of(template, "AWS::SNS::TopicPolicy").values()
    ]
    return docs


def test_no_wildcard_principal_without_conditions(security: dict[str, Any]) -> None:
    for doc in documents(security):
        for statement in doc["Statement"]:
            principal = statement["Principal"]
            wildcard = principal == "*" or principal.get("AWS") == "*"
            assert not wildcard or "Condition" in statement, statement.get("Sid")
            assert not (wildcard and statement["Effect"] == "Allow"), statement.get("Sid")


def test_alerts_topic_is_encrypted_with_the_data_key_and_has_tls_deny(security: dict[str, Any]) -> None:
    topic = resources_of(security, "AWS::SNS::Topic")["AlertsTopic"]["Properties"]
    assert topic["KmsMasterKeyId"] == {"Ref": "DataKeyAlias"}
    statements = resources_of(security, "AWS::SNS::TopicPolicy")["AlertsTopicPolicy"]["Properties"][
        "PolicyDocument"
    ]
    assert any(
        s["Effect"] == "Deny" and "aws:SecureTransport" in json.dumps(s) for s in statements["Statement"]
    )
    subscription = resources_of(security, "AWS::SNS::Subscription")["AlertsEmailSubscription"]["Properties"]
    assert subscription["Protocol"] == "email"
    assert subscription["Endpoint"] == {"Ref": "SupportEmail"}


def test_security_outputs_are_the_contract(security: dict[str, Any]) -> None:
    assert {"DataKeyAlias", "SecretsKeyAlias", "AlertsTopicArn"} <= set(security["Outputs"])
    for name in ("DataKeyAlias", "SecretsKeyAlias", "AlertsTopicArn"):
        assert security["Outputs"][name]["Export"]["Name"] == {"Fn::Sub": f"${{AWS::StackName}}:{name}"}


# ---------------------------------------------------------------- network (task 1.3)


@pytest.fixture(scope="module")
def network() -> dict[str, Any]:
    return load_cfn("network.yaml")


def test_security_group_descriptions_are_unchanged(network: dict[str, Any]) -> None:
    groups = resources_of(network, "AWS::EC2::SecurityGroup")
    assert {k: v["Properties"]["GroupDescription"] for k, v in groups.items()} == FROZEN_SG_DESCRIPTIONS


def test_security_group_names_use_the_sgp_prefix(network: dict[str, Any]) -> None:
    for group in resources_of(network, "AWS::EC2::SecurityGroup").values():
        assert group["Properties"]["GroupName"]["Fn::Sub"].startswith("sgp-${RegionCode}-${Context}-eks-")


def ingress_rules(network: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    rules: list[tuple[str, dict[str, Any]]] = []
    for name, group in resources_of(network, "AWS::EC2::SecurityGroup").items():
        rules += [(name, r) for r in group["Properties"].get("SecurityGroupIngress", [])]
    rules += [
        (n, r["Properties"]) for n, r in resources_of(network, "AWS::EC2::SecurityGroupIngress").items()
    ]
    return rules


def test_only_the_load_balancer_listens_to_the_internet(network: dict[str, Any]) -> None:
    open_rules = [
        (n, r) for n, r in ingress_rules(network) if r.get("CidrIp") == "0.0.0.0/0" or "CidrIpv6" in r
    ]
    assert {(n, r["FromPort"], r["ToPort"]) for n, r in open_rules} == {
        ("AlbSecurityGroup", 443, 443),
        ("AlbSecurityGroup", 80, 80),
    }


def test_no_ssh_or_wide_ingress(network: dict[str, Any]) -> None:
    for name, rule in ingress_rules(network):
        assert rule["IpProtocol"] not in ("-1", "all"), name
        low, high = rule["FromPort"], rule["ToPort"]
        if isinstance(low, int) and isinstance(high, int):
            assert not (low <= 22 <= high), name
            assert high - low < 100, name


def test_every_rule_has_a_description(network: dict[str, Any]) -> None:
    for name, group in resources_of(network, "AWS::EC2::SecurityGroup").items():
        for rule in (
            group["Properties"].get("SecurityGroupIngress", []) + group["Properties"]["SecurityGroupEgress"]
        ):
            assert rule["Description"], name
    for kind in ("AWS::EC2::SecurityGroupIngress", "AWS::EC2::SecurityGroupEgress"):
        for name, rule in resources_of(network, kind).items():
            assert rule["Properties"]["Description"], name


def test_alb_can_reach_the_token_issuer_keys(network: dict[str, Any]) -> None:
    egress = resources_of(network, "AWS::EC2::SecurityGroup")["AlbSecurityGroup"]["Properties"][
        "SecurityGroupEgress"
    ]
    assert any(r["FromPort"] == 443 and r["CidrIp"] == "0.0.0.0/0" for r in egress)


def test_database_is_reachable_only_from_proxy_nodes_and_rotation(network: dict[str, Any]) -> None:
    sources = {
        r["Properties"]["SourceSecurityGroupId"]["Ref"]
        for r in resources_of(network, "AWS::EC2::SecurityGroupIngress").values()
        if r["Properties"]["GroupId"] == {"Ref": "DatabaseSecurityGroup"}
    }
    assert sources == {"ProxySecurityGroup", "NodeSecurityGroup", "RotationSecurityGroup"}
    assert (
        "SecurityGroupIngress"
        not in resources_of(network, "AWS::EC2::SecurityGroup")["DatabaseSecurityGroup"]["Properties"]
    )


def test_subnets_are_tagged_for_load_balancer_discovery_and_not_public(network: dict[str, Any]) -> None:
    expected = {
        "PublicSubnetA": "kubernetes.io/role/elb", "PublicSubnetB": "kubernetes.io/role/elb",
        "PrivateSubnetA": "kubernetes.io/role/internal-elb", "PrivateSubnetB": "kubernetes.io/role/internal-elb",
    }  # fmt: skip
    for name, role_tag in expected.items():
        props = network["Resources"][name]["Properties"]
        tags = {t["Key"] if isinstance(t["Key"], str) else "cluster": t["Value"] for t in props["Tags"]}
        assert tags[role_tag] == "1"
        assert any(
            isinstance(t["Key"], dict) and "kubernetes.io/cluster/eksc-" in t["Key"]["Fn::Sub"]
            for t in props["Tags"]
        )
        assert props["MapPublicIpOnLaunch"] is False
        assert "Fn::Cidr" in props["CidrBlock"]["Fn::Select"][1]


def test_private_default_route_only_exists_under_the_prod_condition(network: dict[str, Any]) -> None:
    for name, route in resources_of(network, "AWS::EC2::Route").items():
        if (
            route["Properties"].get("DestinationCidrBlock") == "0.0.0.0/0"
            and "NatGatewayId" in route["Properties"]
        ):
            assert route.get("Condition") == "NatPerAz", name
    for kind in ("AWS::EC2::NatGateway", "AWS::EC2::EIP"):
        for name, res in resources_of(network, kind).items():
            assert name == "NatEip" or res.get("Condition") == "NatPerAz", name


def test_flow_logs_are_prod_only_and_no_interface_endpoints(network: dict[str, Any]) -> None:
    for kind in ("AWS::EC2::FlowLog", "AWS::Logs::LogGroup", "AWS::IAM::Role"):
        for res in resources_of(network, kind).values():
            assert res.get("Condition") == "CreateFlowLogs"
    endpoints = resources_of(network, "AWS::EC2::VPCEndpoint")
    assert [e["Properties"]["VpcEndpointType"] for e in endpoints.values()] == ["Gateway"]
    assert network["Resources"]["Vpc"]["Properties"]["EnableDnsSupport"] is True
    assert network["Resources"]["Vpc"]["Properties"]["EnableDnsHostnames"] is True


def test_network_outputs_are_the_contract(network: dict[str, Any]) -> None:
    contract = [
        "VpcId", "PublicSubnetA", "PublicSubnetB", "PrivateSubnetA", "PrivateSubnetB", "PublicRouteTableId",
        "PrivateRouteTableId", "NatEipAllocationId", "AlbSecurityGroupId", "NodeSecurityGroupId",
        "DatabaseSecurityGroupId", "ProxySecurityGroupId", "CodeBuildSecurityGroupId", "BastionSecurityGroupId",
        "RotationSecurityGroupId",
    ]  # fmt: skip
    for name in contract:
        assert network["Outputs"][name]["Export"]["Name"] == {"Fn::Sub": f"${{AWS::StackName}}:{name}"}


# ---------------------------------------------------------------- registry (task 1.4)


@pytest.fixture(scope="module")
def registry() -> dict[str, Any]:
    return load_cfn("registry.yaml")


def test_repository_has_immutable_tags_scan_on_push_and_cmk(registry: dict[str, Any]) -> None:
    repositories = resources_of(registry, "AWS::ECR::Repository")
    assert list(repositories) == ["Repository"]
    props = repositories["Repository"]["Properties"]
    assert props["ImageTagMutability"] == "IMMUTABLE"
    assert props["ImageScanningConfiguration"]["ScanOnPush"] is True
    assert props["EncryptionConfiguration"]["EncryptionType"] == "KMS"
    assert props["RepositoryName"] == {"Fn::Sub": "ecr-${RegionCode}-${Context}-eks-${EnvType}"}
    assert props["EmptyOnDelete"] == {"Fn::If": ["EmptyOnDelete", True, False]}


def test_lifecycle_keeps_the_last_ten_images(registry: dict[str, Any]) -> None:
    text = resources_of(registry, "AWS::ECR::Repository")["Repository"]["Properties"]["LifecyclePolicy"][
        "LifecyclePolicyText"
    ]
    assert registry["Parameters"]["ImagesToKeep"]["Default"] == 10
    assert re.search(r'"countNumber":\$\{ImagesToKeep\}', text["Fn::Sub"])
    assert '"imageCountMoreThan"' in text["Fn::Sub"]


def test_repository_policy_is_pull_only_for_two_roles(registry: dict[str, Any]) -> None:
    policy = resources_of(registry, "AWS::ECR::Repository")["Repository"]["Properties"][
        "RepositoryPolicyText"
    ]
    (statement,) = policy["Statement"]
    assert statement["Effect"] == "Allow" and statement["Principal"] != "*"
    assert all(
        a.startswith("ecr:") and not a.startswith(("ecr:Put", "ecr:Init", "ecr:Upload"))
        for a in statement["Action"]
    )
    roles = [r["Fn::Sub"] for r in as_list(statement["Condition"]["ArnEquals"]["aws:PrincipalArn"])]
    assert len(roles) == 2
    assert any(r.endswith("eks-node-${EnvType}") for r in roles)
    assert any(r.endswith("eks-deploy-${EnvType}") for r in roles)

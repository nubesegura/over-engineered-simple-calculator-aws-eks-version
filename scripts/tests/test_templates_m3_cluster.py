"""Template tests of milestone 3, cluster stack (tasks 3.1 to 3.4): YAML load, no AWS."""

from __future__ import annotations

import ipaddress
import json
import re
from typing import Any

import cfn_deploy
import pytest
from test_templates_m2 import TEMPLATES, as_list, env_parameters, load_cfn, of_type

ENVS = ["dev", "prod"]
POD_ROLES = {
    "LbcRole": ("lbc", "kube-system", "aws-load-balancer-controller"),
    "AutoscalerRole": ("autoscaler", "kube-system", "cluster-autoscaler"),
    "CalcRole": ("calc", "calculator", "calc"),
    "HistoryRole": ("history", "calculator", "history"),
    "MigrateRole": ("migrate", "calculator", "migrate"),
}
ALIAS_ACTIONS = {"kms:createalias", "kms:updatealias", "kms:deletealias"}


@pytest.fixture(scope="module")
def cluster() -> dict[str, Any]:
    return load_cfn("cluster.yaml")


def props(template: dict[str, Any], name: str) -> dict[str, Any]:
    result: dict[str, Any] = template["Resources"][name]["Properties"]
    return result


def policy_documents(template: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Every role policy of the stack by logical id, with the autoscaler JSON string parsed."""
    documents: dict[str, dict[str, Any]] = {}
    for name, resource in of_type(template, "AWS::IAM::RolePolicy").items():
        document = resource["Properties"]["PolicyDocument"]
        if isinstance(document, dict) and "Fn::Sub" in document:
            text = document["Fn::Sub"][0]
            document = json.loads(re.sub(r"\$\{[^}]+\}", "x", text))
        documents[name] = document
    return documents


def statements_of(document: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = document["Statement"]
    return result


def role_of(policy: dict[str, Any]) -> str:
    ref: str = policy["Properties"]["RoleName"]["Ref"]
    return ref


def actions_of(statement: dict[str, Any]) -> list[str]:
    return [str(a) for a in as_list(statement["Action"])]


# ---------------------------------------------------------------- cluster (task 3.1)


def test_cluster_is_private_only_with_encryption_logs_and_api_mode(cluster: dict[str, Any]) -> None:
    p = props(cluster, "Cluster")
    vpc = p["ResourcesVpcConfig"]
    assert vpc["EndpointPrivateAccess"] is True and vpc["EndpointPublicAccess"] is False
    assert "PublicAccessCidrs" not in vpc
    assert len(vpc["SubnetIds"]) == 2  # the two private subnets
    assert all("PrivateSubnet" in s["Fn::ImportValue"]["Fn::Sub"] for s in vpc["SubnetIds"])
    assert p["AccessConfig"] == {"AuthenticationMode": "API", "BootstrapClusterCreatorAdminPermissions": False}
    assert p["UpgradePolicy"] == {"SupportType": "STANDARD"}
    assert p["BootstrapSelfManagedAddons"] is False
    assert p["Version"] == {"Ref": "KubernetesVersion"}
    assert p["KubernetesNetworkConfig"] == {"IpFamily": "ipv4", "ServiceIpv4Cidr": {"Ref": "ServiceIpv4Cidr"}}
    (encryption,) = p["EncryptionConfig"]
    assert encryption["Resources"] == ["secrets"]
    # The property takes the key ARN (an alias ARN is not documented and Provider.KeyArn is immutable)
    key_arn = encryption["Provider"]["KeyArn"]["Fn::ImportValue"]["Fn::Sub"]
    assert key_arn.endswith(":SecretsKeyArn")


def test_control_plane_logs_and_log_group(cluster: dict[str, Any]) -> None:
    enabled = props(cluster, "Cluster")["Logging"]["ClusterLogging"]["EnabledTypes"]
    assert sorted(t["Type"] for t in enabled) == ["api", "audit", "authenticator", "controllerManager", "scheduler"]
    group = props(cluster, "ClusterLogGroup")
    assert group["LogGroupName"]["Fn::Sub"] == "/aws/eks/eksc-${RegionCode}-${Context}-${EnvType}/cluster"
    assert group["RetentionInDays"] == {"Ref": "LogRetentionDays"}
    assert group["KmsKeyId"]["Fn::ImportValue"]["Fn::Sub"].endswith(":DataKeyArn")
    assert "ClusterLogGroup" in cluster["Resources"]["Cluster"]["DependsOn"]  # EKS must not create its own


def test_cluster_name_and_deletion_protection_in_prod_only(cluster: dict[str, Any]) -> None:
    p = props(cluster, "Cluster")
    assert p["Name"]["Fn::Sub"] == "eksc-${RegionCode}-${Context}-${EnvType}"
    assert p["DeletionProtection"] == {"Fn::If": ["IsProd", True, False]}
    assert cluster["Conditions"]["IsProd"] == {"Fn::Equals": [{"Ref": "EnvType"}, "prod"]}


def test_cluster_role_has_managed_policy_and_only_the_secrets_key_inline(cluster: dict[str, Any]) -> None:
    role = props(cluster, "ClusterRole")
    assert role["RoleName"]["Fn::Sub"] == "iamr-${RegionCode}-${Context}-eks-cluster-${EnvType}"
    assert role["AssumeRolePolicyDocument"]["Statement"][0]["Principal"] == {"Service": "eks.amazonaws.com"}
    assert [m["Fn::Sub"].rsplit("/", 1)[1] for m in role["ManagedPolicyArns"]] == ["AmazonEKSClusterPolicy"]
    assert "Policies" not in role
    (statement,) = statements_of(policy_documents(cluster)["ClusterRoleSecretsKeyPolicy"])
    assert sorted(actions_of(statement)) == ["kms:Decrypt", "kms:DescribeKey", "kms:Encrypt", "kms:ListGrants"]
    assert statement["Condition"]["ForAnyValue:StringEquals"]["kms:ResourceAliases"] == [
        {"Fn::ImportValue": {"Fn::Sub": "stk-${RegionCode}-${Context}-eks-security-${EnvType}:SecretsKeyAlias"}}
    ]


@pytest.mark.parametrize("env", ENVS)
def test_service_range_does_not_overlap_the_vpc_and_is_a_parameter(cluster: dict[str, Any], env: str) -> None:
    values = env_parameters(env)
    service = ipaddress.ip_network(values["ServiceIpv4Cidr"])
    assert str(service) == "172.20.0.0/16"
    assert not service.overlaps(ipaddress.ip_network(values["VpcCidr"]))
    pattern = cluster["Parameters"]["ServiceIpv4Cidr"]["AllowedPattern"]
    assert re.fullmatch(pattern, values["ServiceIpv4Cidr"])
    assert not re.fullmatch(pattern, "192.168.0.0/16")  # EKS accepts only 10.x and 172.16-31.x ranges


@pytest.mark.parametrize("env", ENVS)
def test_every_parameter_without_default_has_a_value(env: str) -> None:
    """The deploy script refuses a template with a parameter that nothing fills."""
    given = {
        p["ParameterKey"]: p["ParameterValue"] for p in cfn_deploy.stack_parameters(env, TEMPLATES / "cluster.yaml")
    }
    template = load_cfn("cluster.yaml")
    assert set(given) == set(template["Parameters"]) - {
        k for k, v in template["Parameters"].items() if "Default" in v and k not in env_parameters(env)
    }
    assert given["KubernetesVersion"] == "1.37"


# ---------------------------------------------------------------- node group (task 3.2)


def test_node_role_name_is_the_one_the_registry_policy_expects(cluster: dict[str, Any]) -> None:
    role = props(cluster, "NodeRole")
    assert role["RoleName"]["Fn::Sub"] == "iamr-${RegionCode}-${Context}-eks-node-${EnvType}"
    assert role["AssumeRolePolicyDocument"]["Statement"][0]["Principal"] == {"Service": "ec2.amazonaws.com"}
    assert sorted(m["Fn::Sub"].rsplit("/", 1)[1] for m in role["ManagedPolicyArns"]) == [
        "AmazonEC2ContainerRegistryPullOnly",
        "AmazonEKSWorkerNodePolicy",
        "AmazonEKS_CNI_Policy",
    ]
    registry = (TEMPLATES / "registry.yaml").read_text(encoding="utf-8")
    assert "role/iamr-${RegionCode}-${Context}-eks-node-${EnvType}" in registry


@pytest.mark.parametrize("env", ENVS)
def test_node_group_shape(cluster: dict[str, Any], env: str) -> None:
    group = props(cluster, "NodeGroup")
    assert group["NodegroupName"]["Fn::Sub"] == "eksng-${RegionCode}-${Context}-${EnvType}"
    assert group["AmiType"] == "AL2023_ARM_64_STANDARD"
    assert group["CapacityType"] == "ON_DEMAND"
    assert group["InstanceTypes"] == [{"Ref": "NodeInstanceType"}]
    assert cluster["Parameters"]["NodeInstanceType"]["Default"] == "t4g.medium"
    assert group["ScalingConfig"] == {
        "MinSize": {"Ref": "NodeMinSize"},
        "DesiredSize": {"Ref": "NodeDesiredSize"},
        "MaxSize": {"Ref": "NodeMaxSize"},
    }
    values = env_parameters(env)
    assert (values["NodeMinSize"], values["NodeDesiredSize"], values["NodeMaxSize"]) == ("2", "2", "4")
    assert group["UpdateConfig"] == {"MaxUnavailable": 1}
    assert "RemoteAccess" not in group and "DiskSize" not in group  # no key pair, disk comes from the template
    assert len(group["Subnets"]) == 2
    assert all("PrivateSubnet" in s["Fn::ImportValue"]["Fn::Sub"] for s in group["Subnets"])
    assert group["LaunchTemplate"]["Id"] == {"Ref": "NodeLaunchTemplate"}


def test_launch_template_imds_volume_and_security_groups(cluster: dict[str, Any]) -> None:
    data = props(cluster, "NodeLaunchTemplate")["LaunchTemplateData"]
    assert data["MetadataOptions"] == {
        "HttpEndpoint": "enabled",
        "HttpTokens": "required",
        "HttpPutResponseHopLimit": 1,
    }
    assert "KeyName" not in data
    (mapping,) = data["BlockDeviceMappings"]
    ebs = mapping["Ebs"]
    assert ebs["VolumeType"] == "gp3" and ebs["Encrypted"] is True
    assert (
        ebs["VolumeSize"] == {"Ref": "NodeVolumeSizeGb"} and cluster["Parameters"]["NodeVolumeSizeGb"]["Default"] == 20
    )
    assert ebs["KmsKeyId"]["Fn::ImportValue"]["Fn::Sub"].endswith(":DataKeyArn")  # CloudFormation documents an ARN
    groups = data["SecurityGroupIds"]
    assert groups[0]["Fn::ImportValue"]["Fn::Sub"].endswith(":NodeSecurityGroupId")
    assert groups[1] == {"Fn::GetAtt": ["Cluster", "ClusterSecurityGroupId"]}
    assert "NetworkInterfaces" not in data  # no public IP is possible: the subnets do not assign one


def test_autoscaler_tags_and_mandatory_tags_on_nodes(cluster: dict[str, Any]) -> None:
    group_tags = props(cluster, "NodeGroup")["Tags"]
    assert group_tags["k8s.io/cluster-autoscaler/enabled"] == "true"
    mandatory = {"team-owner", "project-name", "app-name", "repo-name", "env-type"}
    assert mandatory <= set(group_tags)
    specs = {
        s["ResourceType"]: [t["Key"] if isinstance(t["Key"], str) else json.dumps(t["Key"]) for t in s["Tags"]]
        for s in props(cluster, "NodeLaunchTemplate")["LaunchTemplateData"]["TagSpecifications"]
    }
    assert set(specs) == {"instance", "volume", "network-interface"}
    for keys in specs.values():
        assert mandatory <= set(keys)
    instance_keys = specs["instance"]
    assert "k8s.io/cluster-autoscaler/enabled" in instance_keys
    assert any("k8s.io/cluster-autoscaler/eksc-" in k for k in instance_keys)


# ---------------------------------------------------------------- add-ons (task 3.3)


def test_five_addons_with_pinned_versions_and_network_policy(cluster: dict[str, Any]) -> None:
    addons = {p["AddonName"]: p for p in (r["Properties"] for r in of_type(cluster, "AWS::EKS::Addon").values())}
    assert set(addons) == {"vpc-cni", "coredns", "kube-proxy", "eks-pod-identity-agent", "metrics-server"}
    assert json.loads(addons["vpc-cni"]["ConfigurationValues"]) == {"enableNetworkPolicy": "true"}
    version_parameters = {a: p["AddonVersion"]["Ref"] for a, p in addons.items()}
    assert len(set(version_parameters.values())) == 5
    for env in ENVS:
        for parameter in version_parameters.values():
            assert re.fullmatch(cluster["Parameters"][parameter]["AllowedPattern"], env_parameters(env)[parameter])
    assert all(p["ClusterName"] == {"Ref": "Cluster"} for p in addons.values())


def test_addons_are_ordered_around_the_node_group(cluster: dict[str, Any]) -> None:
    resources = cluster["Resources"]
    before = {"VpcCniAddon", "KubeProxyAddon", "PodIdentityAgentAddon"}
    assert before <= set(resources["NodeGroup"]["DependsOn"])  # daemon sets that nodes need to become ready
    assert resources["CoreDnsAddon"]["DependsOn"] == "NodeGroup"  # these two need nodes to schedule on
    assert resources["MetricsServerAddon"]["DependsOn"] == "NodeGroup"


# ---------------------------------------------------------------- pod identity (task 3.4)


def test_roles_and_associations(cluster: dict[str, Any]) -> None:
    assert "IngestRole" not in cluster["Resources"]  # the storage stack owns it
    associations = {
        (a["Properties"]["Namespace"], a["Properties"]["ServiceAccount"]): a["Properties"]["RoleArn"]["Fn::GetAtt"][0]
        for a in of_type(cluster, "AWS::EKS::PodIdentityAssociation").values()
    }
    assert associations == {(ns, sa): role for role, (_, ns, sa) in POD_ROLES.items()}
    for role, (workload, _, _) in POD_ROLES.items():
        properties = props(cluster, role)
        assert properties["RoleName"]["Fn::Sub"] == f"iamr-${{RegionCode}}-${{Context}}-eks-{workload}-${{EnvType}}"
        (trust,) = properties["AssumeRolePolicyDocument"]["Statement"]
        assert trust["Principal"] == {"Service": "pods.eks.amazonaws.com"}
        assert sorted(trust["Action"]) == ["sts:AssumeRole", "sts:TagSession"]
        assert "ManagedPolicyArns" not in properties  # one scoped policy per role, nothing broad attached
    for association in of_type(cluster, "AWS::EKS::PodIdentityAssociation").values():
        assert association["Properties"]["ClusterName"] == {"Ref": "Cluster"}


def test_no_role_reads_secrets_from_star_and_only_migrate_reads_the_master_secret(cluster: dict[str, Any]) -> None:
    reads: dict[str, list[str]] = {}
    for name, document in policy_documents(cluster).items():
        for statement in statements_of(document):
            if any(a in ("secretsmanager:GetSecretValue", "secretsmanager:*", "*") for a in actions_of(statement)):
                assert statement["Resource"] != "*", name
                assert "*" not in json.dumps(statement["Resource"]), name
                reads.setdefault(role_of(cluster["Resources"][name]), []).append(
                    statement["Resource"]["Fn::ImportValue"]["Fn::Sub"].rsplit(":", 1)[1]
                )
    assert reads == {"CalcRole": ["AppSecretArn"], "HistoryRole": ["AppSecretArn"], "MigrateRole": ["MasterSecretArn"]}


def test_no_role_can_manage_aliases_and_kms_is_by_alias_and_service(cluster: dict[str, Any]) -> None:
    for name, document in policy_documents(cluster).items():
        for statement in statements_of(document):
            actions = {a.lower() for a in actions_of(statement)}
            assert not actions & ALIAS_ACTIONS, name
            assert "kms:*" not in actions and "*" not in actions, name
            kms = [a for a in actions if a.startswith("kms:")]
            if not kms:
                continue
            assert "key/*" in json.dumps(statement["Resource"]) and "Resource" in statement, name
            condition = statement["Condition"]
            assert "kms:ResourceAliases" in condition["ForAnyValue:StringEquals"], name  # never a key ARN
            if name != "ClusterRoleSecretsKeyPolicy":
                assert condition["StringEquals"]["kms:ViaService"], name
    # IAM policies never name a key ARN; the only ARNs of the data key are the log group, the launch template volume
    # (CloudFormation documents only an ARN there) and the cluster encryption provider
    policies = {
        k: v for k, v in cluster["Resources"].items() if v["Type"] in ("AWS::IAM::RolePolicy", "AWS::IAM::Role")
    }
    assert "DataKeyArn" not in json.dumps(policies) and "SecretsKeyArn" not in json.dumps(policies)


def test_calc_and_history_publish_to_the_alerts_topic_and_migrate_does_not(cluster: dict[str, Any]) -> None:
    documents = policy_documents(cluster)
    for name in ("CalcRolePolicy", "HistoryRolePolicy"):
        publish = [s for s in statements_of(documents[name]) if "sns:Publish" in actions_of(s)]
        assert len(publish) == 1 and publish[0]["Resource"]["Fn::ImportValue"]["Fn::Sub"].endswith(":AlertsTopicArn")
    assert all("sns:" not in a for s in statements_of(documents["MigrateRolePolicy"]) for a in actions_of(s))
    assert documents["CalcRolePolicy"] == documents["HistoryRolePolicy"]  # same permissions, as the design says


def test_load_balancer_controller_policy_is_the_reference_with_tag_conditions(cluster: dict[str, Any]) -> None:
    statements = statements_of(policy_documents(cluster)["LbcRolePolicy"])
    assert len(statements) == 16  # statements of the reference policy v3.6.0
    flat = json.dumps(statements)
    assert flat.count("elbv2.k8s.aws/cluster") >= 9  # tag conditions of the reference policy
    deletes = [s for s in statements if "elasticloadbalancing:DeleteLoadBalancer" in actions_of(s)]
    assert deletes and "aws:ResourceTag/elbv2.k8s.aws/cluster" in json.dumps(deletes[0]["Condition"])
    assert "iam:CreateServiceLinkedRole" in flat and "elasticloadbalancing.amazonaws.com" in flat
    assert not {a.lower() for s in statements for a in actions_of(s)} & ALIAS_ACTIONS


def test_autoscaler_writes_only_on_the_tagged_group(cluster: dict[str, Any]) -> None:
    documents = policy_documents(cluster)
    (write,) = statements_of(documents["AutoscalerWritePolicy"])
    assert sorted(actions_of(write)) == [
        "autoscaling:SetDesiredCapacity",
        "autoscaling:TerminateInstanceInAutoScalingGroup",
    ]
    resource = write["Resource"]["Fn::Sub"] if isinstance(write["Resource"], dict) else write["Resource"]
    assert resource.startswith("arn:${AWS::Partition}:autoscaling:") and "autoScalingGroup" in resource
    assert write["Condition"]["StringEquals"] == {
        "aws:ResourceTag/k8s.io/cluster-autoscaler/enabled": "true",
        "aws:ResourceTag/eks:cluster-name": {"Ref": "Cluster"},
    }
    # CloudFormation requires a JSON object for PolicyDocument, never a string (Fn::Sub of JSON text)
    assert isinstance(cluster["Resources"]["AutoscalerWritePolicy"]["Properties"]["PolicyDocument"], dict)
    assert "Fn::Sub" not in cluster["Resources"]["AutoscalerWritePolicy"]["Properties"]["PolicyDocument"]
    # Everything else the autoscaler may do is a read action.
    for statement in statements_of(documents["AutoscalerReadPolicy"]):
        assert all(a.split(":")[1].startswith(("Describe", "Get")) for a in actions_of(statement))
    write_actions = {a for p in documents.values() for s in statements_of(p) for a in actions_of(s)}
    assert "autoscaling:UpdateAutoScalingGroup" not in write_actions


def test_pod_roles_cannot_be_assumed_from_another_account(cluster: dict[str, Any]) -> None:
    for role in POD_ROLES:
        (trust,) = props(cluster, role)["AssumeRolePolicyDocument"]["Statement"]
        assert trust["Condition"] == {"StringEquals": {"aws:SourceAccount": {"Ref": "AWS::AccountId"}}}


# ---------------------------------------------------------------- outputs (contract with the deploy and bastion stacks)


def test_outputs_are_the_agreed_exports(cluster: dict[str, Any]) -> None:
    expected = {
        "ClusterName",
        "ClusterArn",
        "ClusterSecurityGroupId",
        "ClusterEndpoint",
        "NodeRoleArn",
        "NodeGroupName",
        "KubernetesVersion",
        "LbcRoleArn",
        "AutoscalerRoleArn",
    }
    assert set(cluster["Outputs"]) == expected
    for name, output in cluster["Outputs"].items():
        assert output["Export"]["Name"] == {"Fn::Sub": f"${{AWS::StackName}}:{name}"}
    assert cluster["Outputs"]["ClusterSecurityGroupId"]["Value"] == {
        "Fn::GetAtt": ["Cluster", "ClusterSecurityGroupId"]
    }
    assert cluster["Outputs"]["ClusterName"]["Value"] == {"Ref": "Cluster"}


def test_every_imported_export_exists_in_the_producing_template(cluster: dict[str, Any]) -> None:
    text = (TEMPLATES / "cluster.yaml").read_text(encoding="utf-8")
    for stack, export in sorted(set(re.findall(r"eks-(security|network|database)-\$\{EnvType\}:(\w+)", text))):
        producer = load_cfn(f"{stack}.yaml")
        assert export in producer["Outputs"], f"{stack}:{export}"
        assert "Export" in producer["Outputs"][export], f"{stack}:{export}"


# ---------------------------------------------------------------- key policy for the node volumes (security stack)


def test_data_key_names_the_autoscaling_service_linked_role_behind_a_parameter() -> None:
    security = load_cfn("security.yaml")
    assert security["Conditions"]["GrantAutoScalingRole"] == {
        "Fn::Equals": [{"Ref": "AutoScalingServiceLinkedRoleExists"}, "true"]
    }
    assert security["Parameters"]["AutoScalingServiceLinkedRoleExists"]["AllowedValues"] == ["true", "false"]
    wrapped = [s for s in props(security, "DataKey")["KeyPolicy"]["Statement"] if "Fn::If" in s]
    assert len(wrapped) == 2
    role = "role/aws-service-role/autoscaling.amazonaws.com/AWSServiceRoleForAutoScaling"
    statements = {}
    for item in wrapped:
        condition, statement, otherwise = item["Fn::If"]
        assert condition == "GrantAutoScalingRole" and otherwise == {"Ref": "AWS::NoValue"}
        statements[statement["Sid"]] = statement
    for statement in statements.values():
        assert statement["Principal"]["AWS"]["Fn::Sub"].endswith(role)  # exact role, never a wildcard
        assert statement["Condition"]["StringEquals"]["kms:ViaService"] == {
            "Fn::Sub": "ec2.${AWS::Region}.amazonaws.com"
        }
        assert statement["Condition"]["StringEquals"]["kms:CallerAccount"] == {"Ref": "AWS::AccountId"}
        assert not {a.lower() for a in actions_of(statement)} & ALIAS_ACTIONS
    assert actions_of(statements["AutoScalingNodeVolumeGrants"]) == ["kms:CreateGrant"]
    assert statements["AutoScalingNodeVolumeGrants"]["Condition"]["Bool"] == {"kms:GrantIsForAWSResource": "true"}
    assert set(actions_of(statements["AutoScalingNodeVolumes"])) == {
        "kms:Encrypt", "kms:Decrypt", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:DescribeKey",
    }  # fmt: skip


def test_parameter_values_for_the_service_linked_role() -> None:
    assert env_parameters("dev")["AutoScalingServiceLinkedRoleExists"] == "true"
    assert env_parameters("prod")["AutoScalingServiceLinkedRoleExists"] == "false"  # create the role first, see README

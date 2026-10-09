"""Template and build script tests of milestone 3: deployment build project (3.5) and bastion (3.6).

Pure file inspection plus bash runs of the buildspec commands with stubbed `aws` and `unzip` (no AWS access).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any

import pytest
import yaml
from test_templates_m2 import as_list, env_parameters, load_cfn, of_type

REPO = Path(__file__).resolve().parents[2]
BUILDSPEC = REPO / "scripts" / "buildspec.yml"
SHA256 = re.compile(r"^[0-9a-f]{64}$")


@pytest.fixture(scope="module")
def deploy() -> dict[str, Any]:
    return load_cfn("deploy.yaml")


@pytest.fixture(scope="module")
def bastion() -> dict[str, Any]:
    return load_cfn("bastion.yaml")


def role_statements(template: dict[str, Any], role: str) -> list[dict[str, Any]]:
    (policy,) = template["Resources"][role]["Properties"]["Policies"]
    result: list[dict[str, Any]] = policy["PolicyDocument"]["Statement"]
    return result


def actions_of(stmts: list[dict[str, Any]]) -> list[str]:
    return [a for s in stmts for a in as_list(s["Action"])]


# ---------------------------------------------------------------- deployment project (task 3.5)


def test_buildspec_in_the_template_is_the_file_and_cannot_be_overridden_by_the_source(
    deploy: dict[str, Any],
) -> None:
    project = deploy["Resources"]["DeployProject"]["Properties"]
    embedded: str = project["Source"]["BuildSpec"]
    assert embedded.strip() == BUILDSPEC.read_text(encoding="utf-8").strip()
    assert project["Source"]["Type"] == "S3"
    assert not embedded.strip().endswith((".yml", ".yaml"))  # inline content, never a path inside the archive
    for forbidden in ("SecondarySources", "SourceVersion", "Triggers", "SecondaryArtifacts"):
        assert forbidden not in project
    kinds = {r["Type"] for r in deploy["Resources"].values()}
    assert not kinds & {"AWS::CodeBuild::SourceCredential", "AWS::CodeBuild::ReportGroup"}


def test_project_settings(deploy: dict[str, Any]) -> None:
    project = deploy["Resources"]["DeployProject"]["Properties"]
    env = project["Environment"]
    assert env["Type"] == "ARM_CONTAINER"
    assert env["ComputeType"] == "BUILD_GENERAL1_SMALL"
    assert env["PrivilegedMode"] is False
    assert re.fullmatch(r"aws/codebuild/amazonlinux-aarch64-standard:\d+\.\d+-\d{2}\.\d{2}\.\d{2}", env["Image"])
    assert project["Artifacts"] == {"Type": "NO_ARTIFACTS"}
    assert project["TimeoutInMinutes"] == 30
    assert project["ConcurrentBuildLimit"] == 1
    assert "DataKeyAlias" in json.dumps(project["EncryptionKey"])
    assert project["Name"] == {"Fn::Sub": "cb-${RegionCode}-${Context}-eks-deploy-${EnvType}"}
    logs = project["LogsConfig"]["CloudWatchLogs"]
    assert logs["Status"] == "ENABLED" and logs["GroupName"] == {"Ref": "BuildLogGroup"}


def test_project_runs_in_the_private_subnets_with_the_codebuild_group(deploy: dict[str, Any]) -> None:
    vpc = json.dumps(deploy["Resources"]["DeployProject"]["Properties"]["VpcConfig"])
    for export in ("VpcId", "PrivateSubnetA", "PrivateSubnetB", "CodeBuildSecurityGroupId"):
        assert f"eks-network-${{EnvType}}:{export}" in vpc
    assert "Public" not in vpc


def test_environment_variables_are_plain_and_hold_no_secrets(deploy: dict[str, Any]) -> None:
    variables = deploy["Resources"]["DeployProject"]["Properties"]["Environment"]["EnvironmentVariables"]
    names = {v["Name"] for v in variables}
    assert {v["Type"] for v in variables} == {"PLAINTEXT"}
    assert {"CLUSTER_NAME", "VPC_ID", "KUBERNETES_VERSION", "ENV_TYPE", "TAG_REPO_NAME"} <= names
    assert not {n for n in names if re.search(r"SECRET|PASSWORD|TOKEN|KEY", n)}
    # what the workflow passes at StartBuild must not be fixed in the project
    assert not names & {"SOURCE_SHA256", "DEPLOY_COMMAND"}


def test_log_group_is_encrypted_and_has_the_retention_parameter(deploy: dict[str, Any]) -> None:
    group = deploy["Resources"]["BuildLogGroup"]["Properties"]
    assert group["RetentionInDays"] == {"Ref": "LogRetentionDays"}
    assert "DataKeyArn" in json.dumps(group["KmsKeyId"])
    assert env_parameters("dev")["LogRetentionDays"] == "14" and env_parameters("prod")["LogRetentionDays"] == "90"


def test_role_name_matches_the_registry_policy_and_trust_is_the_service_for_this_project(
    deploy: dict[str, Any],
) -> None:
    role = deploy["Resources"]["DeployRole"]["Properties"]
    name = role["RoleName"]["Fn::Sub"]
    assert name == "iamr-${RegionCode}-${Context}-eks-deploy-${EnvType}"
    registry = (REPO / "templates" / "registry.yaml").read_text(encoding="utf-8")
    assert f"role/{name}" in registry
    (trust,) = role["AssumeRolePolicyDocument"]["Statement"]
    assert trust["Principal"] == {"Service": "codebuild.amazonaws.com"}
    assert trust["Action"] == "sts:AssumeRole"
    assert trust["Condition"]["StringEquals"]["aws:SourceArn"]["Fn::Sub"].endswith(
        ":project/cb-${RegionCode}-${Context}-eks-deploy-${EnvType}"
    )
    assert role["MaxSessionDuration"] <= 3600


def test_role_cannot_push_images_or_use_wildcards(deploy: dict[str, Any]) -> None:
    stmts = role_statements(deploy, "DeployRole")
    actions = actions_of(stmts)
    assert not [a for a in actions if re.match(r"ecr:(Put|InitiateLayerUpload|UploadLayerPart|CompleteLayerUpload)", a)]
    assert not [a for a in actions if a.endswith("*")]
    assert not [a for a in actions if a.startswith(("iam:", "sts:", "secretsmanager:", "eks:Create", "eks:Delete"))]
    assert not [a for a in actions if "Alias" in a]
    assert not [a for a in actions if a.startswith("s3:") and re.search(r"Put|Delete|Acl|Policy", a)]


def test_wildcard_resources_are_only_for_actions_that_need_them(deploy: dict[str, Any]) -> None:
    wildcard = {s["Sid"] for s in role_statements(deploy, "DeployRole") if s["Resource"] == "*"}
    assert wildcard == {
        "EcrLogin",
        "DescribeLoadBalancers",
        "UseTheDataKeyByAlias",
        "VpcNetworkInterfaces",
    }


def test_ssm_writes_are_limited_to_the_backend_registry_path(deploy: dict[str, Any]) -> None:
    by_sid = {s["Sid"]: s for s in role_statements(deploy, "DeployRole")}
    write = by_sid["PublishBackendRegistration"]
    assert {"ssm:PutParameter", "ssm:DeleteParameter"} <= set(write["Action"])
    assert write["Resource"]["Fn::Sub"].endswith(":parameter/${Context}/${EnvType}/api-backends/eks/*")
    certificate = by_sid["ReadSharedCertificateParameter"]
    assert certificate["Action"] == "ssm:GetParameter"
    assert certificate["Resource"]["Fn::Sub"].endswith(":parameter/${Context}/${EnvType}/api-certificate-arn")
    others = [
        s for s in role_statements(deploy, "DeployRole") if any(a.startswith("ssm:") for a in as_list(s["Action"]))
    ]
    assert {s["Sid"] for s in others} == {"PublishBackendRegistration", "ReadSharedCertificateParameter"}


def test_source_read_is_limited_to_this_repository_and_environment_prefix(deploy: dict[str, Any]) -> None:
    by_sid = {s["Sid"]: s for s in role_statements(deploy, "DeployRole")}
    read = by_sid["ReadSourceArchive"]
    assert set(read["Action"]) == {"s3:GetObject", "s3:GetObjectVersion"}
    assert read["Resource"]["Fn::Sub"].endswith("/${TagRepoName}/${EnvType}/*")
    assert deploy["Parameters"]["SourceBucketNamePattern"]["Default"].startswith("aws-sam-cli-managed-default-")


def test_kms_is_granted_by_alias_with_via_service(deploy: dict[str, Any]) -> None:
    by_sid = {s["Sid"]: s for s in role_statements(deploy, "DeployRole")}
    kms = by_sid["UseTheDataKeyByAlias"]
    assert set(kms["Action"]) == {"kms:Decrypt", "kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"}
    assert "DataKeyAlias" in json.dumps(kms["Condition"]["ForAnyValue:StringEquals"]["kms:ResourceAliases"])
    assert kms["Condition"]["StringEquals"]["kms:ViaService"]


def test_no_wildcard_actions_on_sensitive_services(deploy: dict[str, Any]) -> None:
    text = json.dumps(deploy["Resources"]["DeployRole"])
    assert not re.search(r'"(kms|secretsmanager|iam|sts):[A-Za-z]*\*', text)
    assert "AdministratorAccess" not in text and "PowerUserAccess" not in text


def test_network_interface_permission_is_limited_to_the_private_subnets_and_codebuild(deploy: dict[str, Any]) -> None:
    by_sid = {s["Sid"]: s for s in role_statements(deploy, "DeployRole")}
    attach = by_sid["AttachNetworkInterfacesInThePrivateSubnets"]
    condition = attach["Condition"]["StringEquals"]
    assert condition["ec2:AuthorizedService"] == "codebuild.amazonaws.com"
    assert len(condition["ec2:Subnet"]) == 2


def test_access_entry_is_the_cluster_admin_policy_for_the_deploy_role(deploy: dict[str, Any]) -> None:
    entry = deploy["Resources"]["DeployAccessEntry"]["Properties"]
    assert entry["PrincipalArn"] == {"Fn::GetAtt": ["DeployRole", "Arn"]}
    assert entry["Type"] == "STANDARD"
    (policy,) = entry["AccessPolicies"]
    assert policy["PolicyArn"]["Fn::Sub"].endswith(":cluster-access-policy/AmazonEKSClusterAdminPolicy")
    assert policy["AccessScope"] == {"Type": "cluster"}
    assert "KubernetesGroups" not in entry


def test_cluster_security_group_accepts_443_from_the_build_group(deploy: dict[str, Any]) -> None:
    rules = of_type(deploy, "AWS::EC2::SecurityGroupIngress")
    (rule,) = rules.values()
    props = rule["Properties"]
    assert (props["IpProtocol"], props["FromPort"], props["ToPort"]) == ("tcp", 443, 443)
    assert "ClusterSecurityGroupId" in json.dumps(props["GroupId"])
    assert "CodeBuildSecurityGroupId" in json.dumps(props["SourceSecurityGroupId"])
    assert props["Description"] == "Cluster API from the deployment build"
    assert "CidrIp" not in props


def test_failed_builds_go_to_the_alerts_topic(deploy: dict[str, Any]) -> None:
    rule = deploy["Resources"]["BuildFailedRule"]["Properties"]
    pattern = rule["EventPattern"]
    assert pattern["source"] == ["aws.codebuild"]
    assert set(pattern["detail"]["build-status"]) == {"FAILED", "FAULT", "TIMED_OUT"}
    assert pattern["detail"]["project-name"] == [{"Ref": "DeployProject"}]
    (target,) = rule["Targets"]
    assert "AlertsTopicArn" in json.dumps(target["Arn"])
    assert rule["Name"] == {"Fn::Sub": "evr-${RegionCode}-${Context}-eks-deploy-failed-${EnvType}"}


def test_undelivered_alert_events_go_to_an_encrypted_queue_only_this_rule_can_write(deploy: dict[str, Any]) -> None:
    (target,) = deploy["Resources"]["BuildFailedRule"]["Properties"]["Targets"]
    assert target["RetryPolicy"]["MaximumRetryAttempts"] > 0
    assert target["DeadLetterConfig"]["Arn"] == {"Fn::GetAtt": ["AlertsDeadLetterQueue", "Arn"]}
    queue = deploy["Resources"]["AlertsDeadLetterQueue"]["Properties"]
    assert "DataKeyAlias" in json.dumps(queue["KmsMasterKeyId"])
    allow, deny = deploy["Resources"]["AlertsDeadLetterQueuePolicy"]["Properties"]["PolicyDocument"]["Statement"]
    assert allow["Principal"] == {"Service": "events.amazonaws.com"} and allow["Action"] == "sqs:SendMessage"
    assert allow["Condition"]["ArnEquals"]["aws:SourceArn"] == {"Fn::GetAtt": ["BuildFailedRule", "Arn"]}
    assert deny["Effect"] == "Deny" and deny["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}


def test_outputs_follow_the_export_pattern(deploy: dict[str, Any]) -> None:
    outputs = deploy["Outputs"]
    assert {"ProjectName", "ProjectArn", "DeployRoleArn"} <= set(outputs)
    for name, output in outputs.items():
        assert output["Export"]["Name"] == {"Fn::Sub": f"${{AWS::StackName}}:{name}"}
    assert "PowerUserAccess" in outputs["ProjectArn"]["Description"]


def test_deploy_stack_is_always_deployed_and_bastion_only_when_enabled() -> None:
    stacks = json.loads((REPO / "templates" / "stacks.json").read_text(encoding="utf-8"))
    for entries in stacks["environments"].values():
        by_unit = {e["unit"]: e for e in entries}
        assert "onlyIf" not in by_unit["deploy"]
        assert by_unit["bastion"]["onlyIf"] == {"parameter": "EnableBastion", "equals": "true"}
    assert env_parameters("dev")["EnableBastion"] == "true"
    assert env_parameters("prod")["EnableBastion"] == "false"


# ---------------------------------------------------------------- buildspec commands (task 3.5)


def find_bash() -> str | None:
    """A POSIX bash. The WSL launcher in System32 is not usable here."""
    for candidate in (shutil.which("bash"), r"C:\Program Files\Git\bin\bash.exe"):
        if candidate and "system32" not in candidate.lower() and Path(candidate).exists():
            return candidate
    return None


BASH = find_bash()
needs_bash = pytest.mark.skipif(BASH is None, reason="bash is not available")


def buildspec() -> dict[str, Any]:
    spec: dict[str, Any] = yaml.safe_load(BUILDSPEC.read_text(encoding="utf-8"))
    return spec


def posix(path: Path) -> str:
    return path.as_posix()


class Sandbox:
    """A fake bucket, stubs for `aws` and `unzip`, and a verified directory."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.bucket = root / "bucket"
        self.bin = root / "bin"
        self.verified = root / "verified"
        self.log = root / "aws.log"
        self.bucket.mkdir()
        self.bin.mkdir()
        (self.bin / "aws").write_text(
            "#!/usr/bin/env bash\n"
            'echo "$*" >> "$AWS_LOG"\n'
            'if [ "$1" = "s3" ]; then\n'
            '  src="${@: -2:1}"; dst="${@: -1}"; cp "$FAKE_BUCKET/$(basename "$src")" "$dst"\n'
            'elif [ "$1" = "eks" ]; then\n'
            '  while [ $# -gt 0 ]; do [ "$1" = "--kubeconfig" ] && touch "$2"; shift; done\n'
            "fi\n",
            encoding="utf-8",
            newline="\n",
        )
        (self.bin / "unzip").write_text(
            '#!/usr/bin/env bash\nexec "$PYTHON_FOR_STUB" -m zipfile -e "$2" "$4"\n', encoding="utf-8", newline="\n"
        )
        for stub in self.bin.iterdir():
            stub.chmod(0o755)

    def archive(self, files: dict[str, str]) -> str:
        path = self.bucket / "source.zip"
        with zipfile.ZipFile(path, "w") as zf:
            for name, content in files.items():
                zf.writestr(name, content)
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def run(self, script: str, **env: str) -> subprocess.CompletedProcess[str]:
        assert BASH is not None
        environment = {
            **os.environ,
            "PATH": f"{posix(self.bin)}{os.pathsep}{os.environ['PATH']}",
            "AWS_LOG": posix(self.log),
            "FAKE_BUCKET": posix(self.bucket),
            "PYTHON_FOR_STUB": sys.executable,
            "VERIFIED_DIR": posix(self.verified),
            "CODEBUILD_SOURCE_REPO_URL": "s3://bucket-x/repo/dev/source.zip",
            "CLUSTER_NAME": "eksc-test",
            "AWS_REGION": "us-east-2",
            **env,
        }
        return subprocess.run([BASH, "-c", script], env=environment, capture_output=True, text=True, check=False)

    def aws_calls(self) -> list[str]:
        return self.log.read_text(encoding="utf-8").splitlines() if self.log.exists() else []


@pytest.fixture
def sandbox(tmp_path: Path) -> Sandbox:
    return Sandbox(tmp_path)


def verify_script() -> str:
    script: str = buildspec()["phases"]["install"]["commands"][0]
    return script


def dispatch_script() -> str:
    script: str = buildspec()["phases"]["build"]["commands"][0]
    return script


@needs_bash
def test_matching_checksum_extracts_the_verified_copy(sandbox: Sandbox) -> None:
    digest = sandbox.archive({"scripts/helm_addons.sh": "echo ok\n"})
    result = sandbox.run(verify_script(), DEPLOY_COMMAND="addons", SOURCE_SHA256=digest)
    assert result.returncode == 0, result.stderr
    assert (sandbox.verified / "scripts" / "helm_addons.sh").read_text(encoding="utf-8") == "echo ok\n"
    assert any(call.startswith("s3 cp") and "s3://bucket-x/repo/dev/source.zip" in call for call in sandbox.aws_calls())


@needs_bash
def test_different_checksum_refuses_to_run_and_extracts_nothing(sandbox: Sandbox) -> None:
    sandbox.archive({"scripts/helm_addons.sh": "echo tampered\n"})
    result = sandbox.run(verify_script(), DEPLOY_COMMAND="addons", SOURCE_SHA256="0" * 64)
    assert result.returncode != 0
    assert "checksum mismatch" in result.stderr and "refusing to run" in result.stderr
    assert not sandbox.verified.exists()


@needs_bash
@pytest.mark.parametrize("command", ["destroy", "addons; id", "", "ADDONS", "deploy uninstall"])
def test_command_outside_the_allow_list_is_refused_before_any_download(sandbox: Sandbox, command: str) -> None:
    digest = sandbox.archive({"scripts/helm_addons.sh": "echo ok\n"})
    result = sandbox.run(verify_script(), DEPLOY_COMMAND=command, SOURCE_SHA256=digest)
    assert result.returncode != 0
    assert sandbox.aws_calls() == []
    assert not sandbox.verified.exists()


@needs_bash
@pytest.mark.parametrize("digest", ["", "abc", "A" * 64, "g" * 64, "0" * 63])
def test_malformed_checksum_is_refused_before_any_download(sandbox: Sandbox, digest: str) -> None:
    sandbox.archive({"scripts/helm_addons.sh": "echo ok\n"})
    result = sandbox.run(verify_script(), DEPLOY_COMMAND="addons", SOURCE_SHA256=digest)
    assert result.returncode != 0
    assert sandbox.aws_calls() == []


@needs_bash
@pytest.mark.parametrize(
    ("command", "script"),
    [
        ("addons", "scripts/helm_addons.sh"),
        ("deploy", "scripts/helm_deploy.sh"),
        ("uninstall", "scripts/helm_uninstall.sh"),
    ],
)
def test_each_command_runs_its_script_from_the_verified_copy(sandbox: Sandbox, command: str, script: str) -> None:
    sandbox.verified.mkdir()
    target = sandbox.verified / script
    target.parent.mkdir(parents=True)
    target.write_text('echo "ran ${0} in $(basename "$PWD") with $(basename "$KUBECONFIG")"\n', encoding="utf-8")
    result = sandbox.run(dispatch_script(), DEPLOY_COMMAND=command)
    assert result.returncode == 0, result.stderr
    assert f"ran {script} in verified" in result.stdout
    assert any(
        call.startswith("eks update-kubeconfig --name eksc-test --region us-east-2") for call in sandbox.aws_calls()
    )


@needs_bash
def test_a_script_missing_from_the_archive_and_an_unknown_command_fail(sandbox: Sandbox) -> None:
    sandbox.verified.mkdir()
    missing = sandbox.run(dispatch_script(), DEPLOY_COMMAND="deploy")
    assert missing.returncode != 0 and "not part of the verified source archive" in missing.stderr
    unknown = sandbox.run(dispatch_script(), DEPLOY_COMMAND="shell")
    assert unknown.returncode != 0
    assert sandbox.aws_calls() == []


def test_buildspec_pins_tools_with_checksums_and_skips_nothing() -> None:
    spec = buildspec()
    variables = spec["env"]["variables"]
    assert spec["version"] == 0.2
    assert re.fullmatch(r"v\d+\.\d+\.\d+", variables["KUBECTL_VERSION"])
    assert re.fullmatch(r"v\d+\.\d+\.\d+", variables["HELM_VERSION"])
    assert SHA256.match(variables["KUBECTL_SHA256"]) and SHA256.match(variables["HELM_SHA256"])
    install = "\n".join(spec["phases"]["install"]["commands"])
    assert install.count("sha256sum -c -") == 2
    assert "latest" not in install and "stable.txt" not in install


# ---------------------------------------------------------------- bastion (task 3.6)


def test_bastion_has_no_security_group_of_its_own_and_no_inbound_rule(bastion: dict[str, Any]) -> None:
    assert not of_type(bastion, "AWS::EC2::SecurityGroup")
    (rule,) = of_type(bastion, "AWS::EC2::SecurityGroupIngress").values()
    props = rule["Properties"]
    # the only ingress rule is on the CLUSTER group (imported), from the bastion group: 443
    assert "ClusterSecurityGroupId" in json.dumps(props["GroupId"])
    assert "BastionSecurityGroupId" in json.dumps(props["SourceSecurityGroupId"])
    assert (props["FromPort"], props["ToPort"], props["IpProtocol"]) == (443, 443, "tcp")
    assert props["Description"] == "Cluster API from the bastion"
    assert "CidrIp" not in props and "CidrIpv6" not in props
    instance = bastion["Resources"]["BastionInstance"]["Properties"]
    assert "BastionSecurityGroupId" in json.dumps(instance["NetworkInterfaces"][0]["GroupSet"])
    assert "SecurityGroupIngress" not in instance


def test_instance_has_no_key_no_public_ip_and_is_in_a_private_subnet(bastion: dict[str, Any]) -> None:
    instance = bastion["Resources"]["BastionInstance"]["Properties"]
    assert "KeyName" not in instance
    (nic,) = instance["NetworkInterfaces"]
    assert nic["AssociatePublicIpAddress"] is False
    assert "PrivateSubnetA" in json.dumps(nic["SubnetId"]) and "Public" not in json.dumps(nic["SubnetId"])
    assert instance["InstanceType"] == "t4g.nano"
    assert "arm64" in bastion["Parameters"]["BastionImageId"]["Default"]
    assert instance["MetadataOptions"] == {
        "HttpTokens": "required",
        "HttpEndpoint": "enabled",
        "HttpPutResponseHopLimit": 1,
    }
    assert instance["PropagateTagsToVolumeOnCreation"] is True
    (disk,) = instance["BlockDeviceMappings"]
    ebs = disk["Ebs"]
    assert (ebs["Encrypted"], ebs["VolumeType"], ebs["DeleteOnTermination"]) == (True, "gp3", True)
    assert "DataKeyAlias" in json.dumps(ebs["KmsKeyId"])
    assert instance["DisableApiTermination"] == {"Fn::If": ["IsProd", True, False]}
    assert instance["Tags"][0]["Value"] == {"Fn::Sub": "ec2-${RegionCode}-${Context}-eks-bastion-${EnvType}"}


def test_instance_role_can_only_describe_the_cluster_and_write_the_session_log(bastion: dict[str, Any]) -> None:
    role = bastion["Resources"]["BastionRole"]["Properties"]
    (managed,) = role["ManagedPolicyArns"]
    assert managed["Fn::Sub"].endswith(":policy/AmazonSSMManagedInstanceCore")
    assert role["AssumeRolePolicyDocument"]["Statement"][0]["Principal"] == {"Service": "ec2.amazonaws.com"}
    assert role["RoleName"]["Fn::Sub"] == "iamr-${RegionCode}-${Context}-eks-bastion-${EnvType}"
    stmts = role_statements(bastion, "BastionRole")
    assert set(actions_of(stmts)) == {
        "eks:DescribeCluster",
        "logs:DescribeLogGroups",
        "logs:CreateLogStream",
        "logs:DescribeLogStreams",
        "logs:PutLogEvents",
    }
    assert {s["Sid"] for s in stmts if s["Resource"] == "*"} == {"CheckSessionLogGroup"}


def test_access_entry_is_view_only_by_default_and_edit_is_a_deliberate_namespace_parameter(
    bastion: dict[str, Any],
) -> None:
    assert bastion["Parameters"]["BastionWriteAccess"]["Default"] == "false"
    assert bastion["Conditions"]["WriteAccess"] == {"Fn::Equals": [{"Ref": "BastionWriteAccess"}, "true"]}
    entry = bastion["Resources"]["BastionAccessEntry"]["Properties"]
    assert entry["PrincipalArn"] == {"Fn::GetAtt": ["BastionRole", "Arn"]}
    assert entry["KubernetesGroups"] == ["calc-operator-readonly"]
    view, optional = entry["AccessPolicies"]
    assert view["PolicyArn"]["Fn::Sub"].endswith(":cluster-access-policy/AmazonEKSViewPolicy")
    assert view["AccessScope"] == {"Type": "cluster"}
    condition, edit, nothing = optional["Fn::If"]
    assert condition == "WriteAccess" and nothing == {"Ref": "AWS::NoValue"}
    assert edit["PolicyArn"]["Fn::Sub"].endswith(":cluster-access-policy/AmazonEKSEditPolicy")
    assert edit["AccessScope"] == {"Type": "namespace", "Namespaces": ["calculator"]}
    text = json.dumps(entry)
    assert "ClusterAdmin" not in text and "AmazonEKSAdminPolicy" not in text and "SecretAdmin" not in text


def test_sessions_are_logged_to_an_encrypted_log_group(bastion: dict[str, Any]) -> None:
    group = bastion["Resources"]["SessionLogGroup"]["Properties"]
    assert group["RetentionInDays"] == {"Ref": "LogRetentionDays"}
    assert "DataKeyArn" in json.dumps(group["KmsKeyId"])
    document = bastion["Resources"]["SessionDocument"]["Properties"]
    assert document["DocumentType"] == "Session"
    assert document["UpdateMethod"] == "NewVersion"
    content = document["Content"]
    assert content["sessionType"] == "Standard_Stream"
    inputs = content["inputs"]
    assert inputs["cloudWatchLogGroupName"] == {"Ref": "SessionLogGroup"}
    assert inputs["cloudWatchEncryptionEnabled"] is True
    assert inputs["cloudWatchStreamingEnabled"] is True
    assert inputs["s3BucketName"] == ""
    assert int(inputs["idleSessionTimeout"]) <= 60
    assert document["Name"] == {"Fn::Sub": "ssmd-${RegionCode}-${Context}-eks-bastion-${EnvType}"}


def user_data(bastion: dict[str, Any]) -> str:
    sub = bastion["Resources"]["BastionInstance"]["Properties"]["UserData"]["Fn::Base64"]["Fn::Sub"]
    script: str = sub[0]
    return script.replace("${ClusterName}", "eksc-test").replace("${AWS::Region}", "us-east-2")


def test_user_data_pins_and_verifies_every_download(bastion: dict[str, Any]) -> None:
    script = user_data(bastion)
    pins = dict(re.findall(r"^([A-Z0-9_]+)=(\S+)$", script, flags=re.M))
    for tool in ("KUBECTL", "HELM", "AWSCLI"):
        assert re.fullmatch(r"v?\d+\.\d+\.\d+", pins[f"{tool}_VERSION"])
        assert SHA256.match(pins[f"{tool}_SHA256"])
    assert script.count('fetch "https://') == 3
    assert "sha256sum -c -" in script
    assert "latest" not in script and "stable.txt" not in script
    assert 'update-kubeconfig --name "eksc-test" --region "us-east-2"' in script
    # the same kubectl and helm as the deployment build
    variables = buildspec()["env"]["variables"]
    assert (
        pins["KUBECTL_VERSION"] == variables["KUBECTL_VERSION"]
        and pins["KUBECTL_SHA256"] == variables["KUBECTL_SHA256"]
    )
    assert pins["HELM_VERSION"] == variables["HELM_VERSION"] and pins["HELM_SHA256"] == variables["HELM_SHA256"]


def test_user_data_has_no_secrets_and_is_valid_bash(bastion: dict[str, Any], tmp_path: Path) -> None:
    script = user_data(bastion)
    assert not re.search(r"(?i)(password|secret|token|api_?key|aws_secret_access_key)\s*[=:]\s*\S{6,}", script)
    assert "AKIA" not in script
    if BASH is None:
        pytest.skip("bash is not available")
    path = tmp_path / "user-data.sh"
    path.write_text(script, encoding="utf-8", newline="\n")
    check = subprocess.run([BASH, "-n", posix(path)], capture_output=True, text=True, check=False)
    assert check.returncode == 0, check.stderr


def test_bastion_exports_follow_the_pattern(bastion: dict[str, Any]) -> None:
    for name, output in bastion["Outputs"].items():
        assert output["Export"]["Name"] == {"Fn::Sub": f"${{AWS::StackName}}:{name}"}

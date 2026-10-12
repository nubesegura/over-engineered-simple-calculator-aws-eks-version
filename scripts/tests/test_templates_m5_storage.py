"""Template tests of the storage stack (task 5.1): YAML load, no AWS."""

from __future__ import annotations

import json
from typing import Any

import cfn_deploy
import pytest
from test_templates_m2 import (
    REPO,
    TEMPLATES,
    as_list,
    env_parameters,
    load_cfn,
    of_type,
)

ENVS = ["dev", "prod"]


@pytest.fixture(scope="module")
def storage() -> dict[str, Any]:
    return load_cfn("storage.yaml")


def props(template: dict[str, Any], name: str) -> dict[str, Any]:
    result: dict[str, Any] = template["Resources"][name]["Properties"]
    return result


def statement(document: dict[str, Any], sid: str) -> dict[str, Any]:
    (found,) = [s for s in document["Statement"] if s["Sid"] == sid]
    return found


def test_both_buckets_block_public_access_and_encrypt(storage: dict[str, Any]) -> None:
    buckets = of_type(storage, "AWS::S3::Bucket")
    assert set(buckets) == {"IngestBucket", "AlbLogBucket"}
    for name, bucket in buckets.items():
        assert set(bucket["Properties"]["PublicAccessBlockConfiguration"].values()) == {True}, name
        assert bucket["Properties"]["OwnershipControls"]["Rules"] == [{"ObjectOwnership": "BucketOwnerEnforced"}]
    (ingest_rule,) = props(storage, "IngestBucket")["BucketEncryption"]["ServerSideEncryptionConfiguration"]
    assert ingest_rule["ServerSideEncryptionByDefault"]["SSEAlgorithm"] == "aws:kms"
    key = json.dumps(ingest_rule["ServerSideEncryptionByDefault"]["KMSMasterKeyID"])
    assert "DataKeyAlias" in key  # the data key by alias, never a key id
    (log_rule,) = props(storage, "AlbLogBucket")["BucketEncryption"]["ServerSideEncryptionConfiguration"]
    assert log_rule["ServerSideEncryptionByDefault"]["SSEAlgorithm"] == "AES256"  # log delivery has no CMK support


def test_ingest_bucket_is_versioned_and_named_by_convention(
    storage: dict[str, Any],
) -> None:
    p = props(storage, "IngestBucket")
    assert p["VersioningConfiguration"] == {"Status": "Enabled"}
    assert p["BucketName"]["Fn::Sub"] == "bckt-${RegionCode}-${Context}-eks-ingest-${AWS::AccountId}-${EnvType}"


def test_every_bucket_and_queue_policy_denies_insecure_transport(
    storage: dict[str, Any],
) -> None:
    policies = {
        **of_type(storage, "AWS::S3::BucketPolicy"),
        **of_type(storage, "AWS::SQS::QueuePolicy"),
    }
    assert len(policies) == 4
    for name, policy in policies.items():
        denies = [s for s in policy["Properties"]["PolicyDocument"]["Statement"] if s["Sid"] == "DenyInsecureTransport"]
        assert len(denies) == 1, name
        assert denies[0]["Effect"] == "Deny" and denies[0]["Condition"] == {"Bool": {"aws:SecureTransport": "false"}}


def test_ingest_bucket_policy_also_requires_tls_12(storage: dict[str, Any]) -> None:
    document = props(storage, "IngestBucketPolicy")["PolicyDocument"]
    old = statement(document, "DenyOldTlsVersions")
    assert old["Effect"] == "Deny" and old["Condition"] == {"NumericLessThan": {"s3:TlsVersion": 1.2}}


def test_log_delivery_is_limited_to_the_regional_account_and_one_prefix(
    storage: dict[str, Any],
) -> None:
    document = props(storage, "AlbLogBucketPolicy")["PolicyDocument"]
    allow = statement(document, "LoadBalancerLogDelivery")
    assert allow["Action"] == "s3:PutObject"
    assert "${ElbLogDeliveryAccountId}" in allow["Principal"]["AWS"]["Fn::Sub"]
    assert allow["Resource"]["Fn::Sub"].endswith("/alb/AWSLogs/${AWS::AccountId}/*")
    # the only Allow statement; no wildcard principal gets anything
    assert [s["Sid"] for s in document["Statement"] if s["Effect"] == "Allow"] == ["LoadBalancerLogDelivery"]


@pytest.mark.parametrize(("env", "days"), [("dev", "30"), ("prod", "90")])
def test_log_bucket_retention_comes_from_the_parameter_file(storage: dict[str, Any], env: str, days: str) -> None:
    assert env_parameters(env)["AlbLogRetentionDays"] == days
    rules = props(storage, "AlbLogBucket")["LifecycleConfiguration"]["Rules"]
    expire = next(r for r in rules if r["Id"] == "expire-logs")
    assert expire["ExpirationInDays"] == {"Ref": "AlbLogRetentionDays"}


def test_lifecycle_has_a_rule_for_each_prefix(storage: dict[str, Any]) -> None:
    rules = props(storage, "IngestBucket")["LifecycleConfiguration"]["Rules"]
    by_prefix = {r["Prefix"]: r for r in rules if "Prefix" in r}
    assert set(by_prefix) == {"incoming/", "processed/", "rejected/", "reports/"}
    assert all(r["Status"] == "Enabled" for r in rules)
    assert all("NoncurrentVersionExpiration" in r for r in by_prefix.values())  # versioning must not grow forever


def test_only_csv_files_under_incoming_notify_the_queue(
    storage: dict[str, Any],
) -> None:
    (config,) = props(storage, "IngestBucket")["NotificationConfiguration"]["QueueConfigurations"]
    assert config["Event"] == "s3:ObjectCreated:*"
    assert config["Queue"] == {"Fn::GetAtt": ["IngestQueue", "Arn"]}
    rules = {r["Name"]: r["Value"] for r in config["Filter"]["S3Key"]["Rules"]}
    assert rules == {"prefix": "incoming/", "suffix": ".csv"}
    assert storage["Resources"]["IngestBucket"]["DependsOn"] == "IngestQueuePolicy"


def test_queues_are_encrypted_and_redrive_after_the_configured_receives(
    storage: dict[str, Any],
) -> None:
    for name in ("IngestQueue", "DeadLetterQueue"):
        key = props(storage, name)["KmsMasterKeyId"]["Fn::ImportValue"]["Fn::Sub"]
        assert key.endswith(":DataKeyAlias"), name
    redrive = props(storage, "IngestQueue")["RedrivePolicy"]
    assert redrive["deadLetterTargetArn"] == {"Fn::GetAtt": ["DeadLetterQueue", "Arn"]}
    assert redrive["maxReceiveCount"] == {"Ref": "MaxReceiveCount"}
    assert storage["Parameters"]["MaxReceiveCount"]["Default"] == 3
    assert "ingest-dlq" in props(storage, "DeadLetterQueue")["QueueName"]["Fn::Sub"]


def test_dead_letter_queue_accepts_redrive_only_from_the_ingest_queue(
    storage: dict[str, Any],
) -> None:
    allow = props(storage, "DeadLetterQueue")["RedriveAllowPolicy"]
    assert allow["redrivePermission"] == "byQueue"
    (source,) = allow["sourceQueueArns"]
    assert source["Fn::Sub"].endswith(":sqs-${RegionCode}-${Context}-eks-ingest-${EnvType}")
    # the same name as the ingest queue, so the allow list cannot drift
    assert props(storage, "IngestQueue")["QueueName"]["Fn::Sub"] == "sqs-${RegionCode}-${Context}-eks-ingest-${EnvType}"


def test_queue_accepts_events_of_the_ingestion_bucket_in_this_account_only(
    storage: dict[str, Any],
) -> None:
    document = props(storage, "IngestQueuePolicy")["PolicyDocument"]
    allow = statement(document, "OnlyTheIngestionBucketSendsEvents")
    assert allow["Principal"] == {"Service": "s3.amazonaws.com"} and allow["Action"] == "sqs:SendMessage"
    condition = allow["Condition"]
    bucket_name = props(storage, "IngestBucket")["BucketName"]["Fn::Sub"]
    assert condition["ArnEquals"]["aws:SourceArn"]["Fn::Sub"] == f"arn:${{AWS::Partition}}:s3:::{bucket_name}"
    assert condition["StringEquals"] == {"aws:SourceAccount": {"Ref": "AWS::AccountId"}}


def test_key_policy_of_the_security_stack_names_the_same_bucket(
    storage: dict[str, Any],
) -> None:
    key_policy = props(load_cfn("security.yaml"), "DataKey")["KeyPolicy"]
    (grant,) = [
        s for s in key_policy["Statement"] if isinstance(s, dict) and s.get("Sid") == "IngestBucketEventsToQueue"
    ]
    bucket_name = props(storage, "IngestBucket")["BucketName"]["Fn::Sub"]
    assert grant["Condition"]["ArnEquals"]["aws:SourceArn"]["Fn::Sub"].endswith(bucket_name)


def test_ingest_role_trust_and_association(storage: dict[str, Any]) -> None:
    trust = props(storage, "IngestRole")["AssumeRolePolicyDocument"]["Statement"][0]
    assert trust["Principal"] == {"Service": "pods.eks.amazonaws.com"}
    assert sorted(trust["Action"]) == ["sts:AssumeRole", "sts:TagSession"]
    assert trust["Condition"] == {"StringEquals": {"aws:SourceAccount": {"Ref": "AWS::AccountId"}}}
    association = props(storage, "IngestAssociation")
    assert (association["Namespace"], association["ServiceAccount"]) == (
        "calculator",
        "ingest",
    )
    assert association["ClusterName"]["Fn::ImportValue"]["Fn::Sub"].endswith("-cluster-${EnvType}:ClusterName")
    assert association["RoleArn"] == {"Fn::GetAtt": ["IngestRole", "Arn"]}


def test_ingest_role_is_scoped_to_its_queue_prefixes_and_secret(
    storage: dict[str, Any],
) -> None:
    document = props(storage, "IngestRolePolicy")["PolicyDocument"]
    statements = document["Statement"]
    for s in statements:
        assert s["Effect"] == "Allow"
        assert s["Resource"] != "*"
        assert not any(str(a).endswith(":*") for a in as_list(s["Action"])), s["Sid"]
    secret = statement(document, "ReadTheApplicationSecret")
    assert secret["Resource"]["Fn::ImportValue"]["Fn::Sub"].endswith(":AppSecretArn")  # never the master secret
    assert "MasterSecretArn" not in json.dumps(document)
    assert statement(document, "ReceiveFromTheIngestQueue")["Resource"] == {"Fn::GetAtt": ["IngestQueue", "Arn"]}
    assert "sqs:SendMessage" not in json.dumps(document)
    publish = statement(document, "PublishToTheAlertsTopic")
    assert publish["Action"] == "sns:Publish" and publish["Resource"]["Fn::ImportValue"]["Fn::Sub"].endswith(
        ":AlertsTopicArn"
    )
    # objects are read and removed under incoming/ only; results are written under three other prefixes only
    assert statement(document, "ReadAndRemoveIncomingFiles")["Resource"]["Fn::Sub"].endswith("/incoming/*")
    writes = statement(document, "WriteResultsAndMovedFiles")
    assert [r["Fn::Sub"].rsplit("/", 2)[1] for r in writes["Resource"]] == [
        "processed",
        "rejected",
        "reports",
    ]
    assert writes["Action"] == "s3:PutObject"


def test_ingest_role_kms_access_is_by_alias_and_service(
    storage: dict[str, Any],
) -> None:
    document = props(storage, "IngestRolePolicy")["PolicyDocument"]
    services = set()
    for s in document["Statement"]:
        if not any(str(a).startswith("kms:") for a in as_list(s["Action"])):
            continue
        condition = s["Condition"]
        assert condition["ForAnyValue:StringEquals"]["kms:ResourceAliases"][0]["Fn::ImportValue"]["Fn::Sub"].endswith(
            ":DataKeyAlias"
        )
        services.add(condition["StringEquals"]["kms:ViaService"]["Fn::Sub"].split(".")[0])
        assert not {a.lower() for a in map(str, as_list(s["Action"]))} & {
            "kms:createalias",
            "kms:updatealias",
            "kms:deletealias",
        }
    assert services == {"secretsmanager", "sqs", "s3", "sns"}


def test_storage_unit_is_after_the_cluster_and_before_alarms() -> None:
    stacks = json.loads((REPO / "templates" / "stacks.json").read_text(encoding="utf-8"))
    for env in ENVS:
        units = [u["unit"] for u in stacks["environments"][env]]
        assert units.index("cluster") < units.index("storage") < units.index("alarms")
        (entry,) = [u for u in stacks["environments"][env] if u["unit"] == "storage"]
        assert entry["template"] == "templates/storage.yaml" and entry["sam"] is False
        assert "onlyIf" not in entry and "allowReplacement" not in entry


@pytest.mark.parametrize("env", ENVS)
def test_every_parameter_without_default_has_a_value_in_the_parameter_file(
    env: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SUPPORT_EMAIL", "owner@example.invalid")
    given = {
        p["ParameterKey"]: p["ParameterValue"] for p in cfn_deploy.stack_parameters(env, TEMPLATES / "storage.yaml")
    }
    assert {
        "EnvType",
        "RegionCode",
        "AlbLogRetentionDays",
        "ElbLogDeliveryAccountId",
    } <= set(given)
    assert given["EnvType"] == env
    assert len(given["ElbLogDeliveryAccountId"]) == 12

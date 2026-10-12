"""Helpers of the chart tests: locate helm, render the chart with the values a deployment would pass."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

CHART = Path(__file__).resolve().parents[1]
REPO = CHART.parent

DIGEST = "sha256:" + "a" * 64
REPOSITORY = "registry.example.invalid/calc/app"
WAF_ARN = "arn:aws:wafv2:us-east-2:123456789012:regional/webacl/example/00000000-0000-0000-0000-000000000000"
CLIENT_ID = "exampleclientid123"
POOL_ID = "us-east-2_ExamplePool"

HELM_MISSING = "helm is not on PATH: render tests are skipped (install helm 3.22.0 to run them)"

# What the deployment script passes with --set-string (placeholders, never real values)
DEPLOY_VALUES: dict[str, str] = {
    "image.repository": REPOSITORY,
    "image.digest": DIGEST,
    "app.alertsTopicArn": "arn:aws:sns:us-east-2:123456789012:example-alerts",
    "app.appSecretArn": "arn:aws:secretsmanager:us-east-2:123456789012:secret:example-app",
    "cursor.secretArn": "arn:aws:secretsmanager:us-east-2:123456789012:secret:example-cursor",
    "app.masterSecretArn": "arn:aws:secretsmanager:us-east-2:123456789012:secret:example-master",
    "app.dbHost": "proxy.example.invalid",
    "app.dbInstanceHost": "instance.example.invalid",
    "ingress.certificateArn": "arn:aws:acm:us-east-2:123456789012:certificate/example",
    "ingress.securityGroupId": "sg-0123456789abcdef0",
    "ingress.accessLogs.bucket": "example-alb-logs",
    "ingest.queueUrl": "https://sqs.us-east-2.amazonaws.com/123456789012/example-ingest",
    "ingest.bucketName": "example-ingest-bucket",
    "auth.userPoolId": POOL_ID,
    "auth.appClientId": CLIENT_ID,
}

Doc = dict[str, Any]


def find_helm() -> str | None:
    """Path of the helm executable, or None when it is not installed."""
    return shutil.which("helm")


def helm_args(env: str, overrides: Mapping[str, str | bool] | None = None, drop: tuple[str, ...] = ()) -> list[str]:
    """Arguments after `helm <command> <chart>` that select the environment file and pass the deployment values."""
    values: dict[str, str | bool] = {key: value for key, value in DEPLOY_VALUES.items() if key not in drop}
    if env == "prod":
        values["ingress.wafAclArn"] = WAF_ARN
    values.update(overrides or {})
    args = ["-f", str(CHART / f"values-{env}.yaml"), "--namespace", "calculator"]
    for key, value in values.items():
        if isinstance(value, bool):  # real booleans use --set, text always --set-string
            args += ["--set", f"{key}={str(value).lower()}"]
        else:
            args += ["--set-string", f"{key}={value}"]
    return args


def run_helm(command: str, args: list[str]) -> subprocess.CompletedProcess[str]:
    helm = find_helm()
    assert helm is not None, HELM_MISSING
    release = ["calculator"] if command == "template" else []
    return subprocess.run(  # noqa: S603 - fixed arguments, helm found on PATH
        [helm, command, *release, str(CHART), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def render(env: str, overrides: Mapping[str, str | bool] | None = None, drop: tuple[str, ...] = ()) -> list[Doc]:
    """Render the chart for an environment and return the parsed documents."""
    result = run_helm("template", helm_args(env, overrides, drop))
    assert result.returncode == 0, result.stderr
    return [doc for doc in yaml.safe_load_all(result.stdout) if doc]


def of_kind(docs: list[Doc], kind: str) -> list[Doc]:
    return [doc for doc in docs if doc["kind"] == kind]


def named(docs: list[Doc], kind: str, name: str) -> Doc:
    found = [doc for doc in of_kind(docs, kind) if doc["metadata"]["name"] == name]
    assert len(found) == 1, f"{kind}/{name}: {len(found)} found"
    return found[0]


def pod_specs(docs: list[Doc]) -> list[tuple[str, Doc]]:
    """(name, pod spec) of every Deployment and Job."""
    specs = [(doc["metadata"]["name"], doc["spec"]["template"]["spec"]) for doc in of_kind(docs, "Deployment")]
    specs += [(doc["metadata"]["name"], doc["spec"]["template"]["spec"]) for doc in of_kind(docs, "Job")]
    return specs

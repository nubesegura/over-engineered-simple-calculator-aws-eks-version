"""Checks of the chart files that need no helm: values, environment files and template text."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from chart_support import CHART, HELM_MISSING

ROUTES = {
    "add": ["/api/v1/add", "/api/eks/v1/add"],
    "sub": ["/api/v1/sub", "/api/eks/v1/sub"],
    "mul": ["/api/v1/mul", "/api/eks/v1/mul"],
    "div": ["/api/v1/div", "/api/eks/v1/div"],
    "history": ["/api/v1/history", "/api/eks/v1/history"],
}
TEMPLATES = sorted((CHART / "templates").glob("*"))
ALLOWED_KINDS = {
    "Namespace",
    "ResourceQuota",
    "LimitRange",
    "ServiceAccount",
    "Deployment",
    "Service",
    "PodDisruptionBudget",
    "HorizontalPodAutoscaler",
    "Job",
    "NetworkPolicy",
    "Ingress",
}


def load(name: str) -> dict[str, Any]:
    data = yaml.safe_load((CHART / name).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def flatten(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    flat: dict[str, Any] = {}
    for key, value in data.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            flat.update(flatten(value, f"{path}."))
        else:
            flat[path] = value
    return flat


def test_helm_missing_message_is_clear() -> None:
    """The render tests skip with this text when helm is not installed; the checks in this file still run."""
    assert "helm" in HELM_MISSING and "skipped" in HELM_MISSING


def test_workloads_map_to_the_contract_routes() -> None:
    values = load("values.yaml")
    http = {name: w for name, w in values["workloads"].items() if w["kind"] == "http"}
    assert set(http) == set(ROUTES)
    for name, workload in http.items():
        assert workload["routes"] == ROUTES[name]
    assert {name for name, w in values["workloads"].items() if w["kind"] == "worker"} == {"ingest"}
    assert "routes" not in values["workloads"]["ingest"] and values["workloads"]["ingest"]["replicas"] == 1
    accounts = {name: workload["serviceAccount"] for name, workload in values["workloads"].items()}
    assert accounts == {
        "add": "calc",
        "sub": "calc",
        "mul": "calc",
        "div": "calc",
        "history": "history",
        "ingest": "ingest",
    }
    assert values["serviceAccounts"] == ["calc", "history", "migrate", "ingest"]
    assert values["migrate"]["serviceAccount"] == "migrate"


def test_image_values_have_no_tag() -> None:
    image = load("values.yaml")["image"]
    assert set(image) == {"repository", "digest", "pullPolicy"}
    assert image["digest"] == "" and image["repository"] == ""


def test_every_profile_has_requests_and_limits() -> None:
    profiles = load("values.yaml")["resourceProfiles"]
    for name, profile in profiles.items():
        for section in ("requests", "limits"):
            assert profile[section]["cpu"] and profile[section]["memory"], f"{name}.{section}"
    assert profiles["calc"]["requests"]["cpu"] == "250m"


@pytest.mark.parametrize("env", ["dev", "prod"])
def test_environment_files_only_hold_environment_values(env: str) -> None:
    allowed = {
        "envType",
        "replicaCount",
        "app.allowedOrigin",
        "app.logLevel",
        "network.vpcCidr",
        "network.albCidrs",
        "network.databaseCidrs",
        "ingress.wafAclArn",
        "ingress.wafRequired",
        "quota.limitsCpu",
        "quota.limitsMemory",
    }
    base = flatten(load("values.yaml"))
    for key in flatten(load(f"values-{env}.yaml")):
        assert key in allowed or key.startswith("resourceProfiles."), key
        assert key in base, f"{key} is not a key of values.yaml"


def test_environments_set_the_expected_differences() -> None:
    dev, prod = load("values-dev.yaml"), load("values-prod.yaml")
    assert dev["envType"] == "dev" and prod["envType"] == "prod"
    assert dev["replicaCount"] == 1 and prod["replicaCount"] == 2
    assert dev["ingress"]["wafAclArn"] == "" and "wafRequired" not in dev["ingress"]
    assert prod["ingress"]["wafRequired"] is True


def test_environment_vpc_cidr_matches_the_parameters() -> None:
    import json

    for env in ("dev", "prod"):
        parameters = json.loads((CHART.parent / "parameters" / f"{env}.json").read_text(encoding="utf-8"))
        assert load(f"values-{env}.yaml")["network"]["vpcCidr"] == parameters["Parameters"]["VpcCidr"]


def test_environment_subnet_cidrs_are_the_slices_of_the_vpc_range() -> None:
    import ipaddress
    import json

    # Fn::Cidr [VpcCidr, 13, 8] in templates/network.yaml: public subnets are slices 1 and 2, private 11 and 12
    for env in ("dev", "prod"):
        parameters = json.loads((CHART.parent / "parameters" / f"{env}.json").read_text(encoding="utf-8"))
        slices = [str(n) for n in ipaddress.ip_network(parameters["Parameters"]["VpcCidr"]).subnets(new_prefix=24)]
        network = load(f"values-{env}.yaml")["network"]
        assert network["albCidrs"] == [slices[1], slices[2]]
        assert network["databaseCidrs"] == [slices[11], slices[12]]
    template = (CHART.parent / "templates" / "network.yaml").read_text(encoding="utf-8")
    assert "CidrBlock: !Select [1, !Cidr [!Ref VpcCidr, 13, 8]]" in template  # PublicSubnetA
    assert "CidrBlock: !Select [2, !Cidr [!Ref VpcCidr, 13, 8]]" in template  # PublicSubnetB
    assert "CidrBlock: !Select [11, !Cidr [!Ref VpcCidr, 13, 8]]" in template  # PrivateSubnetA
    assert "CidrBlock: !Select [12, !Cidr [!Ref VpcCidr, 13, 8]]" in template  # PrivateSubnetB


def test_chart_files_have_no_account_ids_or_secrets() -> None:
    files = [p for p in CHART.rglob("*") if p.is_file() and "tests" not in p.relative_to(CHART).parts]
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"(?<![0-9])[0-9]{12}(?![0-9])", text), f"account-like number in {path.name}"
        assert not re.search(r"AKIA[0-9A-Z]{16}", text), path.name
        assert "BEGIN PRIVATE KEY" not in text, path.name


def test_template_kinds_are_the_expected_ones() -> None:
    kinds: set[str] = set()
    for path in TEMPLATES:
        kinds.update(re.findall(r"^kind: (\w+)", path.read_text(encoding="utf-8"), flags=re.MULTILINE))
    assert kinds == ALLOWED_KINDS


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: Path(p).name)
def test_templates_have_no_dangerous_settings(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    for forbidden in ("kind: Secret", "hostPath", "hostNetwork", "hostPID", "hostIPC", "privileged: true"):
        assert forbidden not in text
    assert ":latest" not in text
    assert "secretKeyRef" not in text


def test_files_are_utf8_with_lf() -> None:
    for path in CHART.rglob("*"):
        if path.is_file() and (path.suffix in {".yaml", ".tpl", ".py", ".md"} or path.name == ".helmignore"):
            data = path.read_bytes()
            assert b"\r" not in data, path
            data.decode("utf-8")


@pytest.mark.parametrize("template", ["namespace.yaml", "serviceaccounts.yaml"])
def test_resources_applied_before_the_release_carry_the_helm_ownership_metadata(template: str) -> None:
    """The namespace and the service accounts are applied with kubectl before `helm install`: without the Helm
    ownership annotations the release refuses to adopt them (found by the first real deployment)."""
    text = (CHART / "templates" / template).read_text(encoding="utf-8")
    assert "meta.helm.sh/release-name: {{ " in text, template
    assert "meta.helm.sh/release-namespace: {{ " in text, template
    assert "managed-by" in text or "calc.labels" in text, template

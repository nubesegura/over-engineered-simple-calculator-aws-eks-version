"""Tests of scripts/helm_addons.sh (task 3.7): a fake `helm` records the calls, nothing touches a cluster.

The chart render test needs a real `helm` and network access to the chart repositories; it is skipped otherwise.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "helm_addons.sh"
VALUES = REPO / "chart-addons"
LBC_VALUES = VALUES / "aws-load-balancer-controller.values.yaml"
CA_VALUES = VALUES / "cluster-autoscaler.values.yaml"
FAKE_ARCHIVE = b"fake chart archive\n"
FAKE_SHA256 = hashlib.sha256(FAKE_ARCHIVE).hexdigest()
MANDATORY_TAG_KEYS = ["team-owner", "project-name", "app-name", "repo-name", "env-type"]


def find_bash() -> str | None:
    for candidate in (shutil.which("bash"), r"C:\Program Files\Git\bin\bash.exe"):
        if candidate and "system32" not in candidate.lower() and Path(candidate).exists():
            return candidate
    return None


BASH = find_bash()
needs_bash = pytest.mark.skipif(BASH is None, reason="bash is not available")

FAKE_HELM = r"""#!/usr/bin/env bash
echo "$*" >> "$HELM_LOG"
if [ "$1" = "pull" ]; then
  name="$2"; version=""; destination=""
  shift 2
  while [ $# -gt 0 ]; do
    case "$1" in
      --version) version="$2"; shift ;;
      --destination) destination="$2"; shift ;;
    esac
    shift
  done
  printf 'fake chart archive\n' > "$destination/$name-$version.tgz"
fi
exit 0
"""

FAKE_KUBECTL = r"""#!/usr/bin/env bash
echo "$*" >> "$KUBECTL_LOG"
if [ "$1" = "get" ] && [ -n "${FAKE_KUBECTL_DOWN:-}" ]; then exit 1; fi
exit 0
"""

BASE_ENV = {
    "CLUSTER_NAME": "eksc-useast2-oecalc-dev",
    "VPC_ID": "vpc-0123456789abcdef0",
    "KUBERNETES_VERSION": "1.36",
    "ENV_TYPE": "dev",
    "AWS_REGION": "us-east-2",
    "TAG_TEAM_OWNER": "nube-segura",
    "TAG_PROJECT_NAME": "over-engineered-calculator",
    "TAG_APP_NAME": "calc-api",
    "TAG_REPO_NAME": "over-engineered-simple-calculator-aws-eks-version",
}


class Run:
    def __init__(self, result: subprocess.CompletedProcess[str], log: Path, kubectl_log: Path) -> None:
        self.result = result
        self.kubectl = kubectl_log.read_text(encoding="utf-8").splitlines() if kubectl_log.exists() else []
        self.calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    @property
    def ok(self) -> bool:
        return self.result.returncode == 0

    def verbs(self) -> list[str]:
        return [" ".join(call.split()[:2]) for call in self.calls]


@pytest.fixture
def run_script(tmp_path: Path) -> Any:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "helm").write_text(FAKE_HELM, encoding="utf-8", newline="\n")
    (bin_dir / "kubectl").write_text(FAKE_KUBECTL, encoding="utf-8", newline="\n")
    log = tmp_path / "helm.log"
    kubectl_log = tmp_path / "kubectl.log"

    def run(**overrides: str | None) -> Run:
        assert BASH is not None
        log.unlink(missing_ok=True)
        kubectl_log.unlink(missing_ok=True)
        env = {
            **os.environ,
            **BASE_ENV,
            "PATH": f"{bin_dir.as_posix()}{os.pathsep}{os.environ['PATH']}",
            "HELM_LOG": log.as_posix(),
            "KUBECTL_LOG": kubectl_log.as_posix(),
            "LBC_CHART_SHA256": FAKE_SHA256,
            "CA_CHART_SHA256": FAKE_SHA256,
        }
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        result = subprocess.run(
            [BASH, SCRIPT.relative_to(REPO).as_posix()], cwd=REPO, env=env, capture_output=True, text=True, check=False
        )
        return Run(result, log, kubectl_log)

    return run


@needs_bash
def test_installs_the_load_balancer_controller_first_then_the_autoscaler_atomically(run_script: Any) -> None:
    run = run_script()
    assert run.ok, run.result.stderr
    assert run.verbs() == [
        "pull aws-load-balancer-controller",
        "upgrade --install",
        "pull cluster-autoscaler",
        "upgrade --install",
    ]
    _, lbc, _, autoscaler = run.calls
    for call, release in ((lbc, "aws-load-balancer-controller"), (autoscaler, "cluster-autoscaler")):
        assert f"--install {release} " in call
        assert "--atomic" in call
        assert "--namespace kube-system" in call
        assert re.search(r"--timeout \d+m", call)
        assert f"{release}-" in call and call.split()[3].endswith(".tgz")


@needs_bash
def test_chart_versions_are_pinned_and_pulled_by_version(run_script: Any) -> None:
    run = run_script()
    pull_lbc, _, pull_ca, _ = run.calls
    assert "--version 3.6.0" in pull_lbc and "--repo https://aws.github.io/eks-charts" in pull_lbc
    assert "--version 9.59.0" in pull_ca and "--repo https://kubernetes.github.io/autoscaler" in pull_ca
    text = SCRIPT.read_text(encoding="utf-8")
    assert re.search(r'^LBC_CHART_VERSION="\d+\.\d+\.\d+"$', text, flags=re.M)
    assert re.search(r'^CA_CHART_VERSION="\d+\.\d+\.\d+"$', text, flags=re.M)
    assert "latest" not in text.lower()
    for name in ("LBC", "CA"):
        assert re.search(rf'^{name}_CHART_SHA256="\$\{{{name}_CHART_SHA256:-[0-9a-f]{{64}}\}}"$', text, flags=re.M)


@needs_bash
def test_values_files_and_stack_values_reach_the_controller(run_script: Any) -> None:
    run = run_script()
    _, lbc, _, autoscaler = run.calls
    assert "aws-load-balancer-controller.values.yaml" in lbc and "cluster-autoscaler.values.yaml" in autoscaler
    assert "--set-string clusterName=eksc-useast2-oecalc-dev" in lbc
    assert "--set-string region=us-east-2" in lbc
    assert "--set-string vpcId=vpc-0123456789abcdef0" in lbc
    assert "--set replicaCount=1" in lbc
    assert "--set-string autoDiscovery.clusterName=eksc-useast2-oecalc-dev" in autoscaler
    assert "--set-string awsRegion=us-east-2" in autoscaler


@needs_bash
def test_the_five_mandatory_tags_are_default_tags_of_the_controller(run_script: Any) -> None:
    lbc = run_script(ENV_TYPE="dev").calls[1]
    for key in MANDATORY_TAG_KEYS:
        assert f"--set-string defaultTags.{key}=" in lbc
    assert "defaultTags.env-type=dev" in lbc
    assert "defaultTags.repo-name=over-engineered-simple-calculator-aws-eks-version" in lbc


@needs_bash
def test_prod_runs_two_controller_replicas(run_script: Any) -> None:
    lbc = run_script(ENV_TYPE="prod").calls[1]
    assert "--set replicaCount=2" in lbc and "defaultTags.env-type=prod" in lbc


@needs_bash
@pytest.mark.parametrize(
    "missing", ["CLUSTER_NAME", "VPC_ID", "KUBERNETES_VERSION", "ENV_TYPE", "TAG_REPO_NAME", "AWS_REGION"]
)
def test_missing_environment_stops_before_any_helm_call(run_script: Any, missing: str) -> None:
    run = run_script(**{missing: None, "AWS_DEFAULT_REGION": None})
    assert not run.ok
    assert missing in run.result.stderr
    assert run.calls == []


@needs_bash
def test_default_region_variable_is_accepted(run_script: Any) -> None:
    run = run_script(AWS_REGION=None, AWS_DEFAULT_REGION="us-east-2")
    assert run.ok, run.result.stderr
    assert "--set-string region=us-east-2" in run.calls[1]


@needs_bash
def test_changed_chart_archive_fails_before_installing(run_script: Any) -> None:
    run = run_script(LBC_CHART_SHA256="0" * 64)
    assert not run.ok
    assert "checksum" in run.result.stderr
    assert run.verbs() == ["pull aws-load-balancer-controller"]


@needs_bash
def test_autoscaler_of_another_minor_is_skipped_with_a_notice_unless_the_owner_accepts_it(run_script: Any) -> None:
    tag_minor = re.search(r"tag: v(\d+\.\d+)", CA_VALUES.read_text(encoding="utf-8"))
    assert tag_minor is not None
    other = "1.99"
    skipped = run_script(KUBERNETES_VERSION=other)
    assert skipped.ok, skipped.result.stderr
    assert skipped.verbs() == ["pull aws-load-balancer-controller", "upgrade --install"]
    assert "NOTICE: Cluster Autoscaler" in skipped.result.stdout and "task 6.2" in skipped.result.stdout
    assert "cluster-autoscaler PENDING" in skipped.result.stdout
    accepted = run_script(KUBERNETES_VERSION=other, ALLOW_AUTOSCALER_MINOR_SKEW="true")
    assert accepted.ok and "minor skew accepted" in accepted.result.stdout
    assert len(accepted.calls) == 4
    matching = run_script(KUBERNETES_VERSION=f"{tag_minor.group(1)}.4")
    assert matching.ok and "WARNING" not in matching.result.stdout and "NOTICE" not in matching.result.stdout
    assert len(matching.calls) == 4


@needs_bash
def test_operator_rbac_is_applied_after_the_controller_and_only_when_the_cluster_answers(run_script: Any) -> None:
    run = run_script()
    assert run.kubectl[0] == "get --raw=/readyz"
    assert run.kubectl[1].startswith("apply -f ") and run.kubectl[1].endswith("/operator-rbac.yaml")
    down = run_script(FAKE_KUBECTL_DOWN="1")
    assert down.ok and not any(c.startswith("apply") for c in down.kubectl)
    assert "operator-rbac.yaml not applied" in down.result.stdout
    assert down.verbs()[-1] == "upgrade --install"  # the autoscaler is still installed


def test_operator_rbac_manifest_is_read_only_for_nodes_and_metrics_and_bound_to_the_bastion_group() -> None:
    docs = list(yaml.safe_load_all((VALUES / "operator-rbac.yaml").read_text(encoding="utf-8")))
    role, binding = docs
    assert role["kind"] == "ClusterRole" and binding["kind"] == "ClusterRoleBinding"
    resources = {(g, r) for rule in role["rules"] for g in rule["apiGroups"] for r in rule["resources"]}
    assert resources == {("", "nodes"), ("metrics.k8s.io", "nodes"), ("metrics.k8s.io", "pods")}
    verbs = {v for rule in role["rules"] for v in rule["verbs"]}
    assert verbs <= {"get", "list", "watch"} and {"get", "list", "watch"} <= verbs
    assert "secrets" not in {r for _, r in resources} and "*" not in verbs
    assert binding["roleRef"]["name"] == role["metadata"]["name"]
    (subject,) = binding["subjects"]
    assert subject["kind"] == "Group"
    bastion = (REPO / "templates" / "bastion.yaml").read_text(encoding="utf-8")
    assert f"- {subject['name']}" in bastion


# ---------------------------------------------------------------- values files


def load_values(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data


def test_service_accounts_match_the_pod_identity_associations_and_have_no_annotations() -> None:
    lbc, autoscaler = load_values(LBC_VALUES), load_values(CA_VALUES)
    assert lbc["serviceAccount"]["name"] == "aws-load-balancer-controller"
    assert autoscaler["rbac"]["serviceAccount"]["name"] == "cluster-autoscaler"
    assert lbc["serviceAccount"]["annotations"] == {}
    assert autoscaler["rbac"]["serviceAccount"]["annotations"] == {}
    for text in (LBC_VALUES.read_text(encoding="utf-8"), CA_VALUES.read_text(encoding="utf-8")):
        assert "eks.amazonaws.com/role-arn" not in text


def test_one_replica_and_small_requests_and_limits() -> None:
    lbc, autoscaler = load_values(LBC_VALUES), load_values(CA_VALUES)
    assert lbc["replicaCount"] == 1 and autoscaler["replicaCount"] == 1
    for values in (lbc, autoscaler):
        assert set(values["resources"]) == {"requests", "limits"}
        assert set(values["resources"]["requests"]) == {"cpu", "memory"}
        assert set(values["resources"]["limits"]) == {"cpu", "memory"}


def test_autoscaler_arguments_and_image() -> None:
    autoscaler = load_values(CA_VALUES)
    assert autoscaler["cloudProvider"] == "aws"
    args = autoscaler["extraArgs"]
    assert args["expander"] == "least-waste"
    assert args["scale-down-unneeded-time"] == "10m"
    assert re.fullmatch(r"v\d+\.\d+\.\d+", autoscaler["image"]["tag"])
    assert autoscaler["image"]["repository"] == "registry.k8s.io/autoscaling/cluster-autoscaler"
    # discovery tags stay the chart default, which the node group of the cluster stack carries
    assert "tags" not in autoscaler.get("autoDiscovery", {})
    assert "autoscalingGroups" not in autoscaler


def test_values_do_not_fix_what_the_stack_provides() -> None:
    lbc, autoscaler = load_values(LBC_VALUES), load_values(CA_VALUES)
    assert not {"clusterName", "region", "vpcId", "defaultTags"} & set(lbc)
    assert not {"awsRegion"} & set(autoscaler) and "clusterName" not in autoscaler.get("autoDiscovery", {})
    assert not {"awsAccessKeyID", "awsSecretAccessKey"} & set(autoscaler)


# ---------------------------------------------------------------- real chart render (needs helm and network)


@pytest.fixture(scope="module")
def pinned_charts(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    helm = shutil.which("helm")
    if helm is None:
        pytest.skip("helm is not installed")
    text = SCRIPT.read_text(encoding="utf-8")
    charts: dict[str, Path] = {}
    folder = tmp_path_factory.mktemp("charts")
    for prefix, name in (("LBC", "aws-load-balancer-controller"), ("CA", "cluster-autoscaler")):
        repo = re.search(rf'^{prefix}_CHART_REPO="([^"]+)"', text, flags=re.M)
        version = re.search(rf'^{prefix}_CHART_VERSION="([^"]+)"', text, flags=re.M)
        digest = re.search(rf"{prefix}_CHART_SHA256:-([0-9a-f]{{64}})", text)
        assert repo and version and digest
        pulled = subprocess.run(
            [helm, "pull", name, "--repo", repo.group(1), "--version", version.group(1), "--destination", str(folder)],
            capture_output=True,
            text=True,
            check=False,
        )
        if pulled.returncode != 0:
            pytest.skip(f"chart repository not reachable: {pulled.stderr.strip()[:200]}")
        archive = folder / f"{name}-{version.group(1)}.tgz"
        assert hashlib.sha256(archive.read_bytes()).hexdigest() == digest.group(1)
        charts[name] = archive
    return charts


def render(chart: Path, values: Path, *sets: str) -> list[dict[str, Any]]:
    helm = shutil.which("helm")
    assert helm is not None
    command = [helm, "template", chart.name.split("-")[0], str(chart), "--namespace", "kube-system"]
    command += ["--kube-version", "1.37.0", "--values", str(values)]
    for item in sets:
        command += ["--set-string", item]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return [d for d in yaml.safe_load_all(result.stdout) if d]


def test_load_balancer_controller_renders_with_the_values(pinned_charts: dict[str, Path]) -> None:
    docs = render(
        pinned_charts["aws-load-balancer-controller"],
        LBC_VALUES,
        "clusterName=c",
        "region=us-east-2",
        "vpcId=vpc-1",
        *[f"defaultTags.{key}=v" for key in MANDATORY_TAG_KEYS],
    )
    (deployment,) = [d for d in docs if d["kind"] == "Deployment"]
    pod = deployment["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "aws-load-balancer-controller"
    args = pod["containers"][0]["args"]
    assert "--cluster-name=c" in args and "--aws-region=us-east-2" in args and "--aws-vpc-id=vpc-1" in args
    (tags,) = [a for a in args if a.startswith("--default-tags=")]
    assert sorted(item.split("=")[0] for item in tags.removeprefix("--default-tags=").split(",")) == sorted(
        MANDATORY_TAG_KEYS
    )
    (account,) = [d for d in docs if d["kind"] == "ServiceAccount"]
    assert not account["metadata"].get("annotations")


def test_autoscaler_renders_with_the_values(pinned_charts: dict[str, Path]) -> None:
    docs = render(pinned_charts["cluster-autoscaler"], CA_VALUES, "autoDiscovery.clusterName=c", "awsRegion=us-east-2")
    (deployment,) = [d for d in docs if d["kind"] == "Deployment"]
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    assert container["image"].endswith(load_values(CA_VALUES)["image"]["tag"])
    command = container["command"]
    assert (
        "--node-group-auto-discovery=asg:tag=k8s.io/cluster-autoscaler/enabled,k8s.io/cluster-autoscaler/c" in command
    )
    assert "--expander=least-waste" in command and "--scale-down-unneeded-time=10m" in command
    assert deployment["spec"]["template"]["spec"]["serviceAccountName"] == "cluster-autoscaler"

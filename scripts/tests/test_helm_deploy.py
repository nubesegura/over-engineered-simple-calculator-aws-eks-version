"""Tests of scripts/helm_deploy.sh (task 4.7) with fake aws, helm and kubectl executables: nothing touches AWS."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "helm_deploy.sh"
DIGEST = "sha256:" + "c" * 64
APP_SECRET = "arn:aws:secretsmanager:us-east-2:123456789012:secret:app-AbCdEf"
MASTER_SECRET = "arn:aws:secretsmanager:us-east-2:123456789012:secret:master-AbCdEf"
CURSOR_SECRET = "arn:aws:secretsmanager:us-east-2:123456789012:secret:cursor-AbCdEf"
CERTIFICATE = "arn:aws:acm:us-east-2:123456789012:certificate/example"


def find_bash() -> str | None:
    for candidate in (shutil.which("bash"), r"C:\Program Files\Git\bin\bash.exe"):
        if candidate and "system32" not in candidate.lower() and Path(candidate).exists():
            return candidate
    return None


BASH = find_bash()
needs_bash = pytest.mark.skipif(BASH is None, reason="bash is not available")

FAKE_AWS = r"""#!/usr/bin/env bash
echo "aws $*" >> "$CALL_LOG"
if [ "$1" = "ssm" ]; then echo "$FAKE_CERTIFICATE"; exit 0; fi
key="$(printf '%s' "$*" | sed -n "s/.*OutputKey=='\([A-Za-z]*\)'.*/\1/p")"
if [ "$key" = "$FAKE_EMPTY_OUTPUT" ]; then echo None; exit 0; fi
case "$key" in
  ProxyEndpoint) echo proxy.example.invalid ;;
  EndpointAddress) echo instance.example.invalid ;;
  DbName) echo calculator ;;
  AppSecretArn) echo "$FAKE_APP_SECRET" ;;
  MasterSecretArn) echo "$FAKE_MASTER_SECRET" ;;
  CursorSecretArn) echo "$FAKE_CURSOR_SECRET" ;;
  AlertsTopicArn) echo arn:aws:sns:us-east-2:123456789012:alerts ;;
  AlbSecurityGroupId) echo sg-0123456789abcdef0 ;;
  *) exit 1 ;;
esac
"""

FAKE_HELM = r"""#!/usr/bin/env bash
echo "helm $*" >> "$CALL_LOG"
case "$1" in
  template) printf 'kind: Stub\n' ;;
  upgrade) [ -z "${FAKE_UPGRADE_FAIL:-}" ] || { echo "Error: UPGRADE FAILED: timed out waiting" >&2; exit 1; } ;;
esac
exit 0
"""

FAKE_KUBECTL = r"""#!/usr/bin/env bash
echo "kubectl $*" >> "$CALL_LOG"
case "$1 $2 $3 $4" in
  *"apply -f -"*) cat > /dev/null ;;
esac
args="$*"
case "$args" in
  *"get job/"*) printf '%s' "${FAKE_JOB_STATE:-1/}" ;;
  *"get deployments"*) printf 'deployment.apps/add\ndeployment.apps/history\n' ;;
  *"rollout status"*) [ -z "${FAKE_ROLLOUT_FAIL:-}" ] || exit 1 ;;
  *"get ingress"*) printf '%s' "${FAKE_ALB:-}" ;;
  *"logs job/"*) echo "migration log line" ;;
esac
exit 0
"""

BASE_ENV = {
    "IMAGE_DIGEST": DIGEST,
    "RUN_ID": "42",
    "ENV_TYPE": "dev",
    "CONTEXT": "oecalc",
    "REGION_CODE": "useast2",
    "CLUSTER_NAME": "eksc-useast2-oecalc-dev",
    "REPOSITORY_URI": "registry.example.invalid/ecr-useast2-oecalc-eks-dev",
    "AWS_REGION": "us-east-2",
    "WEB_ORIGIN": "https://dev.example.invalid",
    "COGNITO_USER_POOL_ID": "us-east-2_ExamplePool",
    "COGNITO_APP_CLIENT_ID": "exampleclientid123",
    "FAKE_APP_SECRET": APP_SECRET,
    "FAKE_MASTER_SECRET": MASTER_SECRET,
    "FAKE_CURSOR_SECRET": CURSOR_SECRET,
    "FAKE_CERTIFICATE": CERTIFICATE,
    "MIGRATE_POLL_SECONDS": "0",
}


class Run:
    def __init__(self, result: subprocess.CompletedProcess[str], log: Path) -> None:
        self.result = result
        self.calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        self.output = result.stdout + result.stderr

    @property
    def ok(self) -> bool:
        return self.result.returncode == 0

    def index(self, prefix: str) -> int:
        return next(i for i, call in enumerate(self.calls) if call.startswith(prefix))

    def has(self, prefix: str) -> bool:
        return any(call.startswith(prefix) for call in self.calls)

    def find(self, prefix: str) -> str:
        return self.calls[self.index(prefix)]


@pytest.fixture
def run_script(tmp_path: Path) -> Any:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, text in (("aws", FAKE_AWS), ("helm", FAKE_HELM), ("kubectl", FAKE_KUBECTL)):
        (bin_dir / name).write_text(text, encoding="utf-8", newline="\n")
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)  # without the exec bit a real tool on PATH wins on Linux
    log = tmp_path / "calls.log"

    def run(**overrides: str | None) -> Run:
        assert BASH is not None
        log.unlink(missing_ok=True)
        env = {
            **os.environ,
            **BASE_ENV,
            "PATH": f"{bin_dir.as_posix()}{os.pathsep}{os.environ['PATH']}",
            "CALL_LOG": log.as_posix(),
        }
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        result = subprocess.run(
            [BASH, SCRIPT.relative_to(REPO).as_posix()], cwd=REPO, env=env, capture_output=True, text=True, check=False
        )
        return Run(result, log)

    return run


@needs_bash
def test_success_order(run_script: Any) -> None:
    run = run_script(FAKE_ALB="alb.example.invalid")
    assert run.ok, run.output
    steps = [
        run.index("aws cloudformation describe-stacks"),
        run.index("aws ssm get-parameter"),
        run.index("helm template"),
        run.index("kubectl apply -f -"),
        run.index("kubectl --namespace calculator get job/migrate-42"),
        run.index("helm upgrade --install calculator"),
        run.index("kubectl --namespace calculator get deployments"),
        run.index("kubectl --namespace calculator rollout status deployment.apps/add"),
        run.index("kubectl --namespace calculator rollout status deployment.apps/history"),
        run.index("kubectl --namespace calculator get ingress calculator-api"),
    ]
    assert steps == sorted(steps)
    assert "Load balancer address: alb.example.invalid" in run.output
    assert "Summary:" in run.output and "2 deployments rolled out" in run.output


@needs_bash
def test_namespace_accounts_and_job_are_rendered_with_show_only_and_the_run_id(run_script: Any) -> None:
    template = run_script().find("helm template")
    assert "--show-only templates/namespace.yaml" in template
    assert "--show-only templates/serviceaccounts.yaml" in template
    assert "--show-only templates/migrate-job.yaml" in template
    assert "--set-string migrate.runId=42" in template
    assert "--create-namespace" not in template


@needs_bash
def test_release_is_atomic_waits_and_does_not_create_the_job_twice(run_script: Any) -> None:
    upgrade = run_script().find("helm upgrade --install")
    for expected in (
        "--namespace calculator",
        "--atomic",
        "--wait",
        "--timeout 10m",
        "--set migrate.enabled=false",
        f"--set-string image.digest={DIGEST}",
        "--set-string image.repository=registry.example.invalid/ecr-useast2-oecalc-eks-dev",
        "--set-string app.dbHost=proxy.example.invalid",
        "--set-string app.dbInstanceHost=instance.example.invalid",
        "--set-string app.allowedOrigin=https://dev.example.invalid",
        "--set-string auth.userPoolId=us-east-2_ExamplePool",
        "--set-string auth.appClientId=exampleclientid123",
        "--set-string ingress.securityGroupId=sg-0123456789abcdef0",
        f"--set-string ingress.certificateArn={CERTIFICATE}",
        f"--set-string cursor.secretArn={CURSOR_SECRET}",
    ):
        assert expected in upgrade, expected
    assert "values-dev.yaml" in upgrade and "values.yaml" in upgrade
    assert "--create-namespace" not in upgrade


@needs_bash
def test_optional_waf_and_log_bucket_are_passed_only_when_given(run_script: Any) -> None:
    plain = run_script().find("helm upgrade --install")
    assert "wafAclArn" not in plain and "accessLogs" not in plain
    waf = "arn:aws:wafv2:us-east-2:123456789012:regional/webacl/x/00000000"
    full = run_script(ENV_TYPE="prod", WAF_ACL_ARN=waf, ACCESS_LOG_BUCKET="logs-bucket").find("helm upgrade --install")
    assert f"--set-string ingress.wafAclArn={waf}" in full
    assert "--set-string ingress.accessLogs.bucket=logs-bucket" in full and "values-prod.yaml" in full


@needs_bash
@pytest.mark.parametrize("job_state", ["/1", "0/1"])
def test_failed_migration_stops_before_the_release_and_prints_the_log(run_script: Any, job_state: str) -> None:
    run = run_script(FAKE_JOB_STATE=job_state)
    assert not run.ok
    assert not run.has("helm upgrade")
    assert "migration log line" in run.output and "migration failed" in run.output


@needs_bash
def test_migration_that_never_finishes_times_out_before_the_release(run_script: Any) -> None:
    run = run_script(FAKE_JOB_STATE="/", MIGRATE_TIMEOUT_SECONDS="1", MIGRATE_POLL_SECONDS="1")
    assert not run.ok and not run.has("helm upgrade")
    assert "timed out" in run.output


@needs_bash
def test_failed_upgrade_returns_non_zero_with_the_cause_and_waits_for_nothing_else(run_script: Any) -> None:
    run = run_script(FAKE_UPGRADE_FAIL="1")
    assert not run.ok
    assert "UPGRADE FAILED" in run.output and "rolled back" in run.output
    assert run.has("helm history calculator")
    assert not run.has("kubectl --namespace calculator rollout status")


@needs_bash
def test_a_rollout_that_does_not_finish_fails_the_deployment(run_script: Any) -> None:
    run = run_script(FAKE_ROLLOUT_FAIL="1")
    assert not run.ok and "rollout of deployment.apps/add did not finish" in run.output


@needs_bash
def test_no_address_yet_is_a_notice_not_an_error(run_script: Any) -> None:
    run = run_script(FAKE_ALB="")
    assert run.ok and "NOTICE" in run.output and "Load balancer address" not in run.output


@needs_bash
@pytest.mark.parametrize(
    "missing",
    [
        "IMAGE_DIGEST",
        "RUN_ID",
        "ENV_TYPE",
        "CLUSTER_NAME",
        "AWS_REGION",
        "REPOSITORY_URI",
        "WEB_ORIGIN",
        "COGNITO_USER_POOL_ID",
        "COGNITO_APP_CLIENT_ID",
    ],
)
def test_missing_input_is_a_clear_error_before_any_call(run_script: Any, missing: str) -> None:
    run = run_script(**{missing: None, "AWS_DEFAULT_REGION": None})
    assert not run.ok
    assert missing in run.result.stderr
    assert run.calls == []


@needs_bash
@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("IMAGE_DIGEST", "latest"),
        ("IMAGE_DIGEST", "sha256:abc"),
        ("RUN_ID", "Run_1"),
        ("ENV_TYPE", "qa"),
        ("WEB_ORIGIN", "http://dev.example.invalid"),
        ("WEB_ORIGIN", "https://dev.example.invalid/path"),
        ("COGNITO_USER_POOL_ID", "pool"),
    ],
)
def test_malformed_input_is_refused_before_any_call(run_script: Any, name: str, value: str) -> None:
    run = run_script(**{name: value})
    assert not run.ok and name in run.result.stderr
    assert run.calls == []


@needs_bash
def test_an_empty_stack_output_stops_before_touching_the_cluster(run_script: Any) -> None:
    run = run_script(FAKE_EMPTY_OUTPUT="MasterSecretArn")
    assert not run.ok and "MasterSecretArn" in run.result.stderr
    assert not run.has("helm") and not run.has("kubectl")


@needs_bash
def test_no_secret_reference_or_account_id_is_printed(run_script: Any) -> None:
    for run in (run_script(FAKE_ALB="alb.example.invalid"), run_script(FAKE_UPGRADE_FAIL="1")):
        for forbidden in (APP_SECRET, MASTER_SECRET, CURSOR_SECRET, CERTIFICATE, "123456789012", "secretsmanager"):
            assert forbidden not in run.output, forbidden


def test_script_never_enables_command_tracing_or_creates_the_namespace_with_helm() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "set -x" not in text and "--create-namespace" not in text.replace("(not --create-namespace)", "")
    assert "--atomic" in text and "--wait" in text

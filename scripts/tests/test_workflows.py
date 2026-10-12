"""Static checks of the deployment workflows (task 4.8 subset): triggers, prod refusal, pinned actions, secrets."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
import yaml
from conftest import REPO

WORKFLOWS = REPO / ".github" / "workflows"
SHA_PINNED = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")
SECRET_REFERENCE = re.compile(r"\$\{\{\s*secrets\.[A-Za-z0-9_]+\s*\}\}")


def load(name: str) -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    return data


def triggers(workflow: dict[str, Any]) -> dict[str, Any]:
    """PyYAML reads the key `on` as True."""
    raw: dict[Any, Any] = workflow
    value = raw.get("on", raw.get(True))
    if isinstance(value, dict):
        return value
    return {key: None for key in ([value] if isinstance(value, str) else list(value or []))}


def steps(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    return [step for job in workflow["jobs"].values() for step in job.get("steps", [])]


ALL = sorted(path.name for path in WORKFLOWS.glob("*.yml"))


@pytest.mark.parametrize("name", ALL)
def test_workflows_parse(name: str) -> None:
    assert isinstance(load(name)["jobs"], dict)


def test_deploy_dev_is_manual_only_and_serialised() -> None:
    workflow = load("deploy-dev.yml")
    assert workflow["name"] == "Deploy DEV"
    assert set(triggers(workflow)) == {"workflow_dispatch"}  # no push trigger until task 4.8 is completed
    assert workflow["concurrency"]["group"] == "deploy-dev"
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert workflow["jobs"]["ci"]["uses"] == "./.github/workflows/ci.yml"
    deploy = workflow["jobs"]["deploy"]
    assert deploy["needs"] == "ci"
    assert deploy["uses"] == "./.github/workflows/deploy.yml"
    assert deploy["with"] == {"environment": "dev", "ignore_nat_window": "${{ inputs.ignore_nat_window }}"}
    # secrets are passed one by one (never `inherit`), so a new secret is never exposed by accident
    assert set(deploy["secrets"]) == {"ROLE_ARN", "AWS_ACCOUNT_ID", "SUPPORT_EMAIL"}


def test_no_prod_deploy_workflow_yet() -> None:
    assert not (WORKFLOWS / "deploy-prod.yml").exists()


def test_deploy_is_reusable_with_least_privilege() -> None:
    workflow = load("deploy.yml")
    call = triggers(workflow)["workflow_call"]
    assert call["inputs"]["environment"]["required"] is True
    assert workflow["permissions"] == {"id-token": "write", "contents": "read"}


def test_deploy_refuses_prod_before_anything_else() -> None:
    first = steps(load("deploy.yml"))[0]
    assert '!= "dev"' in first["run"]
    assert "exit 1" in first["run"]
    assert first["env"]["TARGET"] == "${{ inputs.environment }}"


def test_deploy_runs_the_guards_in_order_and_builds_after_the_stacks() -> None:
    workflow = load("deploy.yml")
    runs = [step["run"] for step in steps(workflow) if "run" in step]
    joined = "\n".join(runs)
    order = [
        "aws sts get-caller-identity",
        "iac_owner_guard.py check --tool cloudformation --env dev",
        "package_rotation.py",
        "cfn_deploy.py deploy --env dev --through storage",
        "run_deploy_build.py run --env dev --command addons",
        "run_deploy_build.py run --env dev --command deploy",
        "discover_targets.py --cluster-name",
        "cfn_deploy.py deploy --env dev --only",
    ]
    positions = [joined.index(item) for item in order]
    assert positions == sorted(positions)
    assert workflow["env"]["LAST_UNIT"] == "alarms"


def test_account_comparison_is_masked() -> None:
    step = next(s for s in steps(load("deploy.yml")) if s.get("name") == "Verify target AWS account")
    assert "::add-mask::$ACTUAL_ACCOUNT" in step["run"]
    assert step["env"]["EXPECTED_ACCOUNT"] == "${{ secrets.AWS_ACCOUNT_ID }}"
    assert "echo" not in step["run"].replace('echo "::add-mask', "").replace('echo "::error::The credentials', "")


@pytest.mark.parametrize("name", ALL)
def test_actions_are_pinned_by_commit_sha(name: str) -> None:
    for step in steps(load(name)):
        if "uses" in step:
            assert SHA_PINNED.match(step["uses"]), f"{name}: {step['uses']} is not pinned by a 40-hex SHA"


@pytest.mark.parametrize("name", ALL)
def test_secrets_never_appear_in_run_scripts(name: str) -> None:
    """Secrets reach scripts only through env; they are never interpolated into the shell text."""
    for step in steps(load(name)):
        assert not SECRET_REFERENCE.search(step.get("run", "")), f"{name}: secret in run of {step.get('name')}"


def test_secrets_are_not_printed() -> None:
    secret_variables = {"EXPECTED_ACCOUNT", "CFN_PARAM_SupportEmail", "ROLE_ARN", "SUPPORT_EMAIL"}
    for name in ("deploy.yml", "deploy-dev.yml"):
        for step in steps(load(name)):
            for line in step.get("run", "").splitlines():
                if re.match(r"\s*(echo|printf|cat)\b", line):
                    assert not any(
                        f"${variable}" in line or f"${{{variable}}}" in line for variable in secret_variables
                    )
                assert "set -x" not in line


def test_support_email_reaches_the_script_through_the_environment() -> None:
    step = next(s for s in steps(load("deploy.yml")) if "cfn_deploy.py deploy" in s.get("run", ""))
    assert step["env"]["CFN_PARAM_SupportEmail"] == "${{ secrets.SUPPORT_EMAIL }}"


def test_deploy_uses_the_tool_versions_of_ci() -> None:
    ci_env = load("ci.yml")["env"]
    deploy_env = load("deploy.yml")["env"]
    for key in ("PYTHON_VERSION", "CFN_LINT_VERSION", "SAM_CLI_VERSION"):
        assert deploy_env[key] == ci_env[key]


def test_last_unit_exists_in_the_dev_stack_list() -> None:
    import json

    units = [
        e["unit"] for e in json.loads((REPO / "templates" / "stacks.json").read_text("utf-8"))["environments"]["dev"]
    ]
    assert load("deploy.yml")["env"]["LAST_UNIT"] in units


def test_ci_installs_pinned_helm_with_checksum_and_shellcheck() -> None:
    env = load("ci.yml")["env"]
    assert env["HELM_VERSION"] == "v3.22.0"
    assert re.fullmatch("[0-9a-f]{64}", env["HELM_AMD64_SHA256"])
    text = " ".join(step.get("run", "") for step in steps(load("ci.yml")))
    assert "sha256sum -c" in text and "shellcheck scripts/*.sh" in text
    # the arm64 pin of the build project belongs to the same release
    assert env["HELM_VERSION"] in (REPO / "templates" / "deploy.yaml").read_text(encoding="utf-8")


def test_nat_window_preflight_runs_after_the_account_check_and_has_an_override() -> None:
    deploy = load("deploy.yml")
    names = [step.get("name") for step in steps(deploy)]
    # it reads the keep-up SSM parameter, so it needs the verified credentials; no stack is touched before it
    assert names.index("Verify target AWS account") < names.index("NAT window preflight")
    assert names.index("NAT window preflight") < names.index("Owner guard (CloudFormation only)")
    override = triggers(deploy)["workflow_call"]["inputs"]["ignore_nat_window"]
    assert override["type"] == "boolean" and override["default"] is False
    dispatch = triggers(load("deploy-dev.yml"))["workflow_dispatch"]["inputs"]["ignore_nat_window"]
    assert dispatch["type"] == "boolean" and dispatch["default"] is False


def test_ci_has_the_application_jobs_with_postgres_and_the_skip_guard() -> None:
    ci = load("ci.yml")
    assert {"iac", "python-core", "chart", "image"} <= set(ci["jobs"])
    assert ci["permissions"] == {"contents": "read"}
    core = ci["jobs"]["python-core"]
    postgres = core["services"]["postgres"]
    assert re.fullmatch(r"postgres:17\.\d+@sha256:[0-9a-f]{64}", postgres["image"])
    assert "--health-cmd" in postgres["options"]
    assert core["env"]["TEST_DATABASE_URL"].startswith("postgresql://postgres:")  # superuser of the service
    assert postgres["env"]["POSTGRES_USER"] == "postgres"
    runs = "\n".join(step.get("run", "") for step in core["steps"])
    for command in ("ruff check", "ruff format --check", "mypy", "lint-imports", "pytest -q", "pip check", "pip-audit"):
        assert command in runs
    guard = next(step for step in core["steps"] if "skipped" in step.get("name", "").lower())
    assert "-m postgres" in guard["run"] and "--no-cov" in guard["run"] and "exit 1" in guard["run"]


def test_ci_chart_and_image_jobs() -> None:
    ci = load("ci.yml")
    chart_runs = "\n".join(step.get("run", "") for step in ci["jobs"]["chart"]["steps"])
    assert "helm lint chart" in chart_runs and "sha256sum -c" in chart_runs and "pytest chart/tests" in chart_runs
    image = ci["jobs"]["image"]
    assert image["needs"] == "python-core"
    image_runs = "\n".join(step.get("run", "") for step in image["steps"])
    assert "docker buildx build --load --platform linux/arm64 --build-context rotation=src/rotation" in image_runs
    assert "-f src/backend/Dockerfile" in image_runs
    assert image["runs-on"] == "ubuntu-latest" and "arm" not in image["runs-on"]
    uses = {step["uses"].split("@")[0]: step["uses"] for step in image["steps"] if "uses" in step}
    assert uses["docker/setup-qemu-action"].startswith(
        "docker/setup-qemu-action@99012661954931238ded8c8b007157a8430204e1"
    )
    assert uses["docker/setup-buildx-action"].startswith(
        "docker/setup-buildx-action@f87e5991a6d7451dcb8d9637bfbc97413f497069"
    )
    assert image_runs.count("--platform linux/arm64") >= 5
    assert "uid=10001*gid=10001*" in image_runs and "Usage: calculator" in image_runs and "/health" in image_runs
    assert "--read-only" in image_runs and "docker rm --force" in image_runs
    assert "ingest" in image_runs and "-eq 2" in image_runs
    assert "docker push" not in image_runs and "login" not in image_runs
    assert "Dockerfile" in next(s for s in image["steps"] if s.get("id") == "dockerfile")["run"]


def step_index(workflow: dict[str, Any], name: str) -> int:
    return [step.get("name") for step in steps(workflow)].index(name)


def test_deploy_builds_pushes_and_deploys_in_order() -> None:
    workflow = load("deploy.yml")
    order = [
        "Configure AWS Credentials",
        "Verify target AWS account",
        "NAT window preflight",
        "Owner guard (CloudFormation only)",
        "Deploy the stacks (security through storage)",
        "Install the cluster add-ons (CodeBuild, command addons)",
        "Set up QEMU (arm64)",
        "Set up Docker Buildx",
        "Log in to the image registry (ECR)",
        "Build and push the image (immutable tag, digest)",
        "Deploy the release (CodeBuild, command deploy)",
        "Discover the alarm dimensions and build the reminder",
        "Deploy the alarms stack",
        "Deployment summary",
    ]
    positions = [step_index(workflow, name) for name in order]
    assert positions == sorted(positions)
    assert workflow["env"]["LAST_UNIT"] == "alarms"
    assert set(triggers(load("deploy-dev.yml"))) == {"workflow_dispatch"}


def test_image_push_uses_buildx_arm64_ecr_login_and_an_immutable_tag() -> None:
    workflow = load("deploy.yml")
    by_name = {step.get("name"): step for step in steps(workflow)}
    login = by_name["Log in to the image registry (ECR)"]["run"]
    assert "aws ecr get-login-password | docker login --username AWS --password-stdin" in login
    assert "::add-mask::" in login and "${GITHUB_SHA::7}-${GITHUB_RUN_ID}" in login
    push = by_name["Build and push the image (immutable tag, digest)"]["run"]
    assert "docker buildx build --platform linux/arm64 --build-context rotation=src/rotation" in push
    assert "-f src/backend/Dockerfile" in push and "--push" in push and "--metadata-file" in push
    assert "containerimage.digest" in push and "describe-images" in push
    assert ":latest" not in push
    uses = {s["uses"].split("@")[0]: s["uses"] for s in steps(workflow) if "uses" in s}
    assert "99012661954931238ded8c8b007157a8430204e1" in uses["docker/setup-qemu-action"]
    assert "f87e5991a6d7451dcb8d9637bfbc97413f497069" in uses["docker/setup-buildx-action"]


def test_release_is_deployed_by_digest_with_allow_listed_overrides() -> None:
    workflow = load("deploy.yml")
    step = next(s for s in steps(workflow) if s.get("name") == "Deploy the release (CodeBuild, command deploy)")
    run = step["run"]
    assert "--command deploy" in run
    for name in (
        "IMAGE_DIGEST",
        "RUN_ID",
        "WEB_ORIGIN",
        "COGNITO_USER_POOL_ID",
        "COGNITO_APP_CLIENT_ID",
        "INGEST_QUEUE_URL",
        "INGEST_BUCKET",
        "ACCESS_LOG_BUCKET",
    ):
        assert f"--pass {name}" in run
    assert step["env"]["RUN_ID"] == "r${{ github.run_id }}"
    assert step["env"]["COGNITO_USER_POOL_ID"] == "${{ vars.COGNITO_USER_POOL_ID }}"
    assert step["env"]["WAF_ACL_ARN"] == "" and "ACCESS_LOG_BUCKET" not in step["env"]
    # the three storage values are read from the stack outputs and masked before they are passed on
    for key, variable in (
        ("IngestQueueUrl", "INGEST_QUEUE_URL"),
        ("IngestBucketName", "INGEST_BUCKET"),
        ("AlbLogBucketName", "ACCESS_LOG_BUCKET"),
    ):
        assert f"--unit storage --key {key}" in run and f'echo "::add-mask::${variable}"' in run
    assert "IMAGE_TAG" not in run


def test_configuration_check_fails_early_on_empty_cognito_variables_and_web_origin() -> None:
    step = next(s for s in steps(load("deploy.yml")) if s.get("name") == "Validate environment configuration")
    assert "COGNITO_USER_POOL_ID" in step["env"] and "COGNITO_APP_CLIENT_ID" in step["env"]
    assert ".Parameters.WebOrigin" in step["run"] and "parameters/dev.json" in step["run"]
    assert "WebOrigin is empty" in step["run"]


def test_ci_never_pushes_images() -> None:
    text = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    assert "docker push" not in text and "--push" not in text and "docker login" not in text


def test_every_unit_up_to_the_last_one_has_its_template() -> None:
    last = load("deploy.yml")["env"]["LAST_UNIT"]
    stacks = json.loads((REPO / "templates" / "stacks.json").read_text(encoding="utf-8"))["environments"]["dev"]
    units = [entry["unit"] for entry in stacks]
    for entry in stacks[: units.index(last) + 1]:
        assert (REPO / entry["template"]).is_file(), entry["template"]


def test_alarms_are_deployed_alone_after_the_release_with_the_discovered_dimensions() -> None:
    by_name = {step.get("name"): step for step in steps(load("deploy.yml"))}
    discover = by_name["Discover the alarm dimensions and build the reminder"]["run"]
    assert 'discover_targets.py --cluster-name "$cluster" --enable-asg-metrics --github-env "$GITHUB_ENV"' in discover
    assert 'reminder_message.py --env dev --cluster-name "$cluster" --github-env "$GITHUB_ENV"' in discover
    assert "--unit cluster --key ClusterName" in discover
    # the first deployment stops before the alarms (their dimensions do not exist yet); the second one is --only
    assert "--through storage" in by_name["Deploy the stacks (security through storage)"]["run"]
    alarms_run = by_name["Deploy the alarms stack"]["run"].strip()
    assert alarms_run == 'python scripts/cfn_deploy.py deploy --env dev --only "$LAST_UNIT"'


def test_destroy_removes_the_registration_before_the_stacks_are_deleted() -> None:
    workflow = load("destroy-dev.yml")
    names = [step.get("name") for step in steps(workflow)]
    step = steps(workflow)[names.index("Remove the API backend registration (SSM)")]
    assert "scripts/destroy_registry.sh" in step["run"] and "ENV_TYPE=dev" in step["run"]
    assert "inputs.mode == 'destroy'" in step["if"]
    assert names.index("Remove the API backend registration (SSM)") < names.index("Destroy (CloudFormation)")

"""Static checks of the deployment workflows (task 4.8 subset): triggers, prod refusal, pinned actions, secrets."""

from __future__ import annotations

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
    assert deploy["with"] == {"environment": "dev"}
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
        "cfn_deploy.py deploy --env dev --through",
        "run_deploy_build.py run --env dev --command addons",
    ]
    positions = [joined.index(item) for item in order]
    assert positions == sorted(positions)
    assert workflow["env"]["LAST_UNIT"] == "bastion"


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

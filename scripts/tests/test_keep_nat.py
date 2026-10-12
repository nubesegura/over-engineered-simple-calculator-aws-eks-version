"""Static checks of the Keep NAT workflow and its agreement with the scheduler template."""

from __future__ import annotations

import re
from typing import Any

import pytest
import yaml
from conftest import REPO

PATH = REPO / ".github" / "workflows" / "keep-nat.yml"
SHA_PINNED = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")


@pytest.fixture(scope="module")
def workflow() -> dict[Any, Any]:
    loaded: dict[Any, Any] = yaml.safe_load(PATH.read_text(encoding="utf-8"))
    return loaded


def steps(workflow: dict[Any, Any]) -> dict[str, dict[str, Any]]:
    return {step["name"]: step for step in workflow["jobs"]["keep-nat"]["steps"]}


def test_manual_only_with_a_boolean_keep_input_defaulting_to_true(workflow: dict[Any, Any]) -> None:
    assert set(workflow[True]) == {"workflow_dispatch"}
    inputs = workflow[True]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {"keep"}
    assert inputs["keep"]["type"] == "boolean"
    assert inputs["keep"]["default"] is True


def test_dev_environment_oidc_pinned_actions_and_no_inherited_secrets(workflow: dict[Any, Any]) -> None:
    job = workflow["jobs"]["keep-nat"]
    assert job["environment"] == "dev"
    assert workflow["permissions"] == {"id-token": "write", "contents": "read"}
    uses = [s["uses"] for s in job["steps"] if "uses" in s]
    assert uses
    assert all(SHA_PINNED.match(u) for u in uses)
    text = PATH.read_text(encoding="utf-8")
    assert "inherit" not in text
    assert "ecs" not in text.replace("secrets", "").lower()


def test_the_account_check_runs_before_the_write_and_the_account_is_masked(workflow: dict[Any, Any]) -> None:
    named = steps(workflow)
    order = list(named)
    assert order.index("Configure AWS Credentials") < order.index("Verify target AWS account")
    assert order.index("Verify target AWS account") < order.index("Write the keep-up setting")
    assert named["Configure AWS Credentials"]["with"]["mask-aws-account-id"] is True
    assert "::add-mask::$ACTUAL_ACCOUNT" in named["Verify target AWS account"]["run"]
    assert 'echo "$ACTUAL_ACCOUNT"' not in named["Verify target AWS account"]["run"]


def test_it_overwrites_only_the_keep_up_parameter_and_reminds_to_set_false(workflow: dict[Any, Any]) -> None:
    write = steps(workflow)["Write the keep-up setting"]
    assert 'aws ssm put-parameter --name "$PARAMETER_NAME" --type String --overwrite --value "$VALUE"' in write["run"]
    assert workflow["env"]["PARAMETER_NAME"] == "/oecalc/dev/eks/nat-keep-up"
    assert "VALUE=true" in write["run"] and "VALUE=false" in write["run"]
    assert "run this workflow again with keep false" in write["run"]
    assert write["env"]["KEEP"] == "${{ inputs.keep }}"  # inputs reach the shell through env, never inline
    for step in steps(workflow).values():
        assert "${{ inputs." not in step.get("run", "")


def test_the_parameter_name_and_initial_value_match_the_template() -> None:
    template = (REPO / "templates" / "scheduler.yaml").read_text(encoding="utf-8")
    assert '"/${Context}/${EnvType}/eks/nat-keep-up"' in template
    assert 'Value: "false"' in template
    assert "nat-keep-up-until" not in PATH.read_text(encoding="utf-8") + template


def test_the_old_timestamp_design_is_gone_everywhere() -> None:
    needle = "nat-keep-up" + "-until"
    hits = [
        str(p.relative_to(REPO))
        for folder in ("templates", "scripts", "src/scheduler", ".github", "docs")
        for p in (REPO / folder).rglob("*")
        if p.is_file() and p.suffix in {".py", ".yaml", ".yml", ".md", ".json"} and needle in p.read_text("utf-8")
        if p.name != "test_keep_nat.py"
    ]
    assert hits == []

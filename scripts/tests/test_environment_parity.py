"""Requirement 15.2: dev and prod use the same template files and differ only through parameters and chart values."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from conftest import REPO
from test_templates_m1 import load_cfn

TEMPLATES = REPO / "templates"
ENV_LITERAL = re.compile(r"^(dev|prod)$")


def walk(node: Any) -> Any:
    """Every dict and list of a loaded template, depth first."""
    yield node
    children = node.values() if isinstance(node, dict) else node if isinstance(node, list) else []
    for child in children:
        yield from walk(child)


def stack_templates() -> dict[str, dict[str, str]]:
    data = json.loads((TEMPLATES / "stacks.json").read_text(encoding="utf-8"))
    environments: dict[str, list[dict[str, str]]] = data["environments"]
    return {env: {e["unit"]: e["template"] for e in entries} for env, entries in environments.items()}


def test_both_environments_deploy_the_same_template_file_per_unit() -> None:
    by_env = stack_templates()
    assert set(by_env) == {"dev", "prod"}
    assert by_env["dev"] == by_env["prod"]
    for path in by_env["dev"].values():
        assert (REPO / path).is_file()


def test_templates_folder_holds_no_environment_specific_copy() -> None:
    used = {value.split("/")[-1] for value in stack_templates()["dev"].values()}
    on_disk = {p.name for p in TEMPLATES.iterdir() if p.suffix in {".yaml", ".yml", ".json"}}
    assert on_disk - used == {"stacks.json"}  # nothing like network-prod.yaml next to the shared files


def test_chart_has_one_set_of_templates_and_two_environment_value_files() -> None:
    chart = REPO / "chart"
    assert sorted(p.name for p in chart.glob("values*.yaml")) == ["values-dev.yaml", "values-prod.yaml", "values.yaml"]
    assert not list((chart / "templates").glob("*prod*")) and not list((chart / "templates").glob("*dev*"))


@pytest.mark.parametrize("template", sorted(p.name for p in TEMPLATES.glob("*.yaml")))
def test_environment_is_only_read_from_the_env_type_parameter(template: str) -> None:
    """A template may compare EnvType with a literal (a condition); it never hard-codes an environment elsewhere."""
    body = load_cfn(template)
    assert body["Parameters"]["EnvType"]["AllowedValues"] == ["dev", "prod"]
    for node in walk(body.get("Resources", {})):
        if isinstance(node, str):
            assert not ENV_LITERAL.match(node), f"{template}: environment literal in a resource: {node}"
    for node in walk({"Conditions": body.get("Conditions", {}), "Rules": body.get("Rules", {})}):
        if isinstance(node, dict) and "Fn::Equals" in node:
            left, right = node["Fn::Equals"]
            operands = [left, right]
            if any(isinstance(o, str) and ENV_LITERAL.match(o) for o in operands):
                assert {"Ref": "EnvType"} in operands, f"{template}: literal compared with something else than EnvType"

"""Policy documents must be JSON objects, never strings (a string built with Fn::Sub is rejected by CloudFormation).

cfn-lint and sam validate do not catch this: it only fails when the stack is created.
"""

from __future__ import annotations

from typing import Any

import pytest
from test_templates_m2 import TEMPLATES, load_cfn

DOCUMENT_PROPERTIES = {
    "PolicyDocument",
    "AssumeRolePolicyDocument",
    "KeyPolicy",
    "RepositoryPolicyText",
    "ResourcePolicy",
}
STRING_BUILDERS = {"Fn::Sub", "Fn::Join", "Fn::Base64", "Fn::ToJsonString"}


def document_properties(template: dict[str, Any]) -> list[tuple[str, str, Any]]:
    found: list[tuple[str, str, Any]] = []
    for name, resource in template.get("Resources", {}).items():
        for prop, value in (resource.get("Properties") or {}).items():
            if prop in DOCUMENT_PROPERTIES:
                found.append((name, prop, value))
    return found


@pytest.mark.parametrize("template_file", sorted(p.name for p in TEMPLATES.glob("*.yaml")))
def test_policy_documents_are_json_objects(template_file: str) -> None:
    template = load_cfn(template_file)
    for name, prop, value in document_properties(template):
        assert not isinstance(value, str), f"{template_file}: {name}.{prop} is a string"
        assert isinstance(value, dict), f"{template_file}: {name}.{prop} is not an object"
        assert not (set(value) & STRING_BUILDERS), f"{template_file}: {name}.{prop} is built as a string"


def nested_documents(node: Any, path: str = "") -> list[tuple[str, Any]]:
    """Every PolicyDocument-like property at any depth (inline `Policies` of a role, for example)."""
    found: list[tuple[str, Any]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in DOCUMENT_PROPERTIES:
                found.append((f"{path}.{key}", value))
            found.extend(nested_documents(value, f"{path}.{key}"))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            found.extend(nested_documents(item, f"{path}[{index}]"))
    return found


@pytest.mark.parametrize("template_file", sorted(p.name for p in TEMPLATES.glob("*.yaml")))
def test_nested_policy_documents_are_json_objects(template_file: str) -> None:
    """Also covers documents nested below the resource properties, which the test above does not reach."""
    for path, value in nested_documents(load_cfn(template_file).get("Resources", {})):
        assert isinstance(value, dict), f"{template_file}: {path} is not an object"
        assert not (set(value) & STRING_BUILDERS), f"{template_file}: {path} is built as a string"

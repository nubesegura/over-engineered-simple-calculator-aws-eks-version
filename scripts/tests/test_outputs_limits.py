"""Outputs and exports that CloudFormation only rejects when the stack is created.

An exported value is limited to 1024 characters; the certificate authority of an EKS cluster is about 1560, so it
cannot be exported (consumers read it with `aws eks describe-cluster` or `aws eks update-kubeconfig`).
"""

from __future__ import annotations

import json

import pytest
from test_templates_m2 import TEMPLATES, load_cfn

# Attributes known to be longer than the 1024 characters that an export allows
LONG_ATTRIBUTES = ("CertificateAuthorityData", "OpenIdConnectIssuerUrl")


@pytest.mark.parametrize("template_file", sorted(p.name for p in TEMPLATES.glob("*.yaml")))
def test_exports_are_within_cloudformation_limits(template_file: str) -> None:
    outputs = load_cfn(template_file).get("Outputs", {}) or {}
    assert len(outputs) <= 200, f"{template_file}: more than 200 outputs"
    for name, output in outputs.items():
        value = json.dumps(output.get("Value"))
        if "Export" in output:
            for attribute in LONG_ATTRIBUTES:
                assert attribute not in value, f"{template_file}: output {name} exports {attribute}, longer than 1024"
            assert len(value) <= 1024, f"{template_file}: exported output {name} may exceed 1024 characters"

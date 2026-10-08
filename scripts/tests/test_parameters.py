"""The parameter files hold every per-environment difference, so they must stay in step."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MANDATORY_TAGS = {"team-owner", "project-name", "app-name", "repo-name", "env-type"}


def load(env: str) -> dict[str, dict[str, str]]:
    data: dict[str, dict[str, str]] = json.loads((REPO / "parameters" / f"{env}.json").read_text(encoding="utf-8"))
    return data


def test_dev_and_prod_have_the_same_keys() -> None:
    dev, prod = load("dev"), load("prod")
    assert set(dev["Parameters"]) == set(prod["Parameters"])
    assert set(dev["Tags"]) == set(prod["Tags"])


@pytest.mark.parametrize("env", ["dev", "prod"])
def test_environment_identity_and_mandatory_tags(env: str) -> None:
    config = load(env)
    assert config["Parameters"]["EnvType"] == env
    assert config["Tags"]["env-type"] == env
    assert set(config["Tags"]) == MANDATORY_TAGS
    assert config["Tags"]["repo-name"] == "over-engineered-simple-calculator-aws-eks-version"


@pytest.mark.parametrize("env", ["dev", "prod"])
def test_values_are_strings_and_hold_no_account_id_or_email(env: str) -> None:
    for key, value in load(env)["Parameters"].items():
        assert isinstance(value, str), key
        assert "@" not in value, key
    # Only the public regional ELB log delivery account may look like an account id.
    twelve_digits = [k for k, v in load(env)["Parameters"].items() if v.isdigit() and len(v) == 12]
    assert twelve_digits == ["ElbLogDeliveryAccountId"]


def test_production_controls_are_on_and_dev_is_cheap() -> None:
    dev, prod = load("dev")["Parameters"], load("prod")["Parameters"]
    assert prod["KmsDeletionWindowDays"] == "30" and dev["KmsDeletionWindowDays"] == "7"
    assert prod["EnableFlowLogs"] == "true" and dev["EnableFlowLogs"] == "false"
    assert prod["NatGatewayPerAz"] == "true" and prod["NatSchedulerEnabled"] == "false"
    assert dev["NatGatewayPerAz"] == "false" and dev["NatSchedulerEnabled"] == "true"
    assert prod["DbMultiAz"] == "true" and prod["DbDeletionProtection"] == "true"
    assert dev["DbMultiAz"] == "false" and dev["DbDeletionProtection"] == "false"
    assert int(prod["DbBackupRetentionDays"]) == 15 and int(dev["DbBackupRetentionDays"]) == 1
    assert prod["EnableBastion"] == "false" and dev["EnableBastion"] == "true"


def test_stacks_list_has_the_agreed_order_and_names() -> None:
    stacks = json.loads((REPO / "templates" / "stacks.json").read_text(encoding="utf-8"))
    order = [
        "security",
        "network",
        "registry",
        "scheduler",
        "database",
        "rotation",
        "storage",
        "cluster",
        "deploy",
    ]
    order += ["bastion", "alarms"]
    for env, entries in stacks["environments"].items():
        assert [e["unit"] for e in entries] == order
        for entry in entries:
            assert entry["stack"] == f"stk-useast2-oecalc-eks-{entry['unit']}-{env}"
            assert entry["template"] == f"templates/{entry['unit']}.yaml"

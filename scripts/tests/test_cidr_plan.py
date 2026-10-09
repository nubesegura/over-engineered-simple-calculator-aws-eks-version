"""The address plan of docs/architecture.md D23 must not overlap (so VPCs can be joined later)."""

from __future__ import annotations

import ipaddress
import json
from pathlib import Path

import pytest
from test_templates_m1 import load_cfn

REPO = Path(__file__).resolve().parents[2]

# Ranges of the plan in D23 that this project must never overlap (copy of the table, update with the doc).
OTHER_RANGES = {
    "ecs dev": "10.20.0.0/16",
    "ecs prod": "10.21.0.0/16",
    "default VPC": "172.31.0.0/16",
}


def parameter(env: str, name: str) -> ipaddress.IPv4Network:
    data = json.loads((REPO / "parameters" / f"{env}.json").read_text(encoding="utf-8"))
    return ipaddress.IPv4Network(data["Parameters"][name])


@pytest.mark.parametrize("env", ["dev", "prod"])
def test_vpc_cidr_does_not_overlap_the_plan(env: str) -> None:
    vpc = parameter(env, "VpcCidr")
    assert vpc.prefixlen == 16
    for name, cidr in OTHER_RANGES.items():
        assert not vpc.overlaps(ipaddress.ip_network(cidr)), f"{env} VPC overlaps {name}"
    assert not vpc.overlaps(parameter(env, "ServiceIpv4Cidr"))


def test_dev_and_prod_do_not_overlap_each_other_and_match_d23() -> None:
    dev, prod = parameter("dev", "VpcCidr"), parameter("prod", "VpcCidr")
    assert not dev.overlaps(prod)
    assert (str(dev), str(prod)) == ("10.30.0.0/16", "10.31.0.0/16")


def test_subnets_are_derived_from_the_vpc_cidr_with_cidr_function() -> None:
    network = load_cfn("network.yaml")
    assert network["Resources"]["Vpc"]["Properties"]["CidrBlock"] == {"Ref": "VpcCidr"}
    indexes: set[int] = set()
    for name in ("PublicSubnetA", "PublicSubnetB", "PrivateSubnetA", "PrivateSubnetB"):
        index, cidr = network["Resources"][name]["Properties"]["CidrBlock"]["Fn::Select"]
        assert cidr == {"Fn::Cidr": [{"Ref": "VpcCidr"}, 13, 8]}
        indexes.add(index)
    assert len(indexes) == 4
    # 13 slices of /24 fit in a /16 and the highest index used must exist
    assert max(indexes) < 13

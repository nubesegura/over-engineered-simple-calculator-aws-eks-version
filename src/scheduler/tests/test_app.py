"""Tests of the NAT scheduler: moto EC2 for the real flows, a stub client for the failure paths."""

import json
import logging
import time
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws

import app as scheduler

TAGS = {
    "team-owner": "nube-segura",
    "project-name": "over-engineered-calculator",
    "app-name": "calc-api",
    "repo-name": "over-engineered-simple-calculator-aws-eks-version",
    "env-type": "dev",
}
NAT_NAME = "nat-useast2-oecalc-eks-dev"
KEEP_PARAMETER = "/oecalc/dev/eks/nat-keep-up"


class Network:
    """Network fixture: one public subnet, one private route table and one Elastic IP."""

    def __init__(self) -> None:
        self.ec2: Any = boto3.client("ec2", region_name="us-east-2")
        vpc = self.ec2.create_vpc(CidrBlock="10.30.0.0/16")["Vpc"]["VpcId"]
        self.subnet: str = self.ec2.create_subnet(VpcId=vpc, CidrBlock="10.30.0.0/24")["Subnet"]["SubnetId"]
        self.table: str = self.ec2.create_route_table(VpcId=vpc)["RouteTable"]["RouteTableId"]
        self.eip: str = self.ec2.allocate_address(Domain="vpc")["AllocationId"]

    def gateways(self) -> list[Any]:
        found = self.ec2.describe_nat_gateways()["NatGateways"]
        return [g for g in found if g["State"] in ("pending", "available")]

    def finish_nat_deletion(self) -> None:
        """Emulate AWS releasing the Elastic IP once the gateway is gone (moto keeps it associated)."""
        for address in self.ec2.describe_addresses(AllocationIds=[self.eip])["Addresses"]:
            if address.get("AssociationId"):
                self.ec2.disassociate_address(AssociationId=address["AssociationId"])

    def default_target(self) -> str | None:
        tables = self.ec2.describe_route_tables(RouteTableIds=[self.table])["RouteTables"]
        for route in tables[0]["Routes"]:
            if route.get("DestinationCidrBlock") == "0.0.0.0/0":
                return str(route.get("NatGatewayId"))
        return None


@pytest.fixture
def net(monkeypatch: pytest.MonkeyPatch) -> Iterator[Network]:
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-2")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    with mock_aws():
        n = Network()
        monkeypatch.setenv("PUBLIC_SUBNET_ID", n.subnet)
        monkeypatch.setenv("PRIVATE_ROUTE_TABLE_ID", n.table)
        monkeypatch.setenv("EIP_ALLOCATION_ID", n.eip)
        monkeypatch.setenv("NAME_TAG", NAT_NAME)
        monkeypatch.setenv("TAGS_JSON", json.dumps(TAGS))
        monkeypatch.setenv("KEEP_UP_PARAMETER", KEEP_PARAMETER)
        yield n


def run(action: str, **extra: Any) -> dict[str, str]:
    return scheduler.handler({"action": action, **extra}, None)


def test_up_creates_tagged_gateway_and_routes(net: Network) -> None:
    run("up")
    (gateway,) = net.gateways()
    tags = {t["Key"]: t["Value"] for t in gateway["Tags"]}
    assert tags == {"Name": NAT_NAME, **TAGS}
    assert gateway["SubnetId"] == net.subnet
    assert net.default_target() == gateway["NatGatewayId"]


def test_down_then_up_restores_the_route(net: Network) -> None:
    run("up")
    run("down")
    assert net.default_target() is None
    assert net.gateways() == []
    net.finish_nat_deletion()
    run("up")
    (gateway,) = net.gateways()
    assert net.default_target() == gateway["NatGatewayId"]


def test_up_twice_makes_one_gateway(net: Network) -> None:
    run("up")
    first = net.gateways()[0]["NatGatewayId"]
    run("up")
    assert [g["NatGatewayId"] for g in net.gateways()] == [first]
    assert net.default_target() == first


def test_down_when_nothing_exists_succeeds(net: Network) -> None:
    assert run("down") == {"action": "down", "status": "ok"}
    assert run("down", wait=True) == {"action": "down", "status": "ok"}
    assert net.gateways() == []


def test_down_removes_route_before_gateway(net: Network, monkeypatch: pytest.MonkeyPatch) -> None:
    run("up")
    calls: list[str] = []
    real = boto3.client

    def spying_client(*args: Any, **kwargs: Any) -> Any:
        client = real(*args, **kwargs)
        for name in ("delete_route", "delete_nat_gateway") if args[0] == "ec2" else ():
            original = getattr(client, name)

            def wrapper(*a: Any, _n: str = name, _o: Any = original, **k: Any) -> Any:
                calls.append(_n)
                return _o(*a, **k)

            setattr(client, name, wrapper)
        return client

    monkeypatch.setattr(boto3, "client", spying_client)
    run("down")
    assert calls == ["delete_route", "delete_nat_gateway"]


def test_down_keeps_route_and_gateway_of_another_project(net: Network) -> None:
    other_gateway = net.ec2.create_nat_gateway(SubnetId=net.subnet, AllocationId=net.eip)
    other = other_gateway["NatGateway"]["NatGatewayId"]
    net.ec2.create_route(RouteTableId=net.table, DestinationCidrBlock="0.0.0.0/0", NatGatewayId=other)
    run("down")
    assert net.default_target() == other
    assert len(net.gateways()) == 1


def test_up_replaces_an_existing_default_route(net: Network) -> None:
    igw = net.ec2.create_internet_gateway()["InternetGateway"]["InternetGatewayId"]
    net.ec2.create_route(RouteTableId=net.table, DestinationCidrBlock="0.0.0.0/0", GatewayId=igw)
    run("up")
    assert net.default_target() == net.gateways()[0]["NatGatewayId"]


def test_up_recovers_after_failure_between_steps(net: Network, monkeypatch: pytest.MonkeyPatch) -> None:
    real_wait = scheduler._wait_available

    def boom(_ec2: Any, _gid: str) -> None:
        raise RuntimeError("waiter timed out")

    monkeypatch.setattr(scheduler, "_wait_available", boom)
    with pytest.raises(RuntimeError):
        run("up")
    assert net.default_target() is None
    assert len(net.gateways()) == 1
    monkeypatch.setattr(scheduler, "_wait_available", real_wait)
    run("up")
    assert len(net.gateways()) == 1
    assert net.default_target() == net.gateways()[0]["NatGatewayId"]


def test_down_recovers_when_route_is_already_gone(net: Network) -> None:
    run("up")
    net.ec2.delete_route(RouteTableId=net.table, DestinationCidrBlock="0.0.0.0/0")
    run("down")
    assert net.gateways() == []


class FakeEc2:
    """Stub client with scripted states for what moto cannot reproduce."""

    def __init__(self, gateways: list[list[dict[str, Any]]], route_states: list[str | None]) -> None:
        self.gateway_pages = gateways
        self.route_states = route_states
        self.calls: list[str] = []
        self.route_target = "nat-1"

    def describe_nat_gateways(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("describe_nat_gateways")
        page = self.gateway_pages.pop(0) if len(self.gateway_pages) > 1 else self.gateway_pages[0]
        return {"NatGateways": page}

    def describe_route_tables(self, **_k: Any) -> dict[str, Any]:
        state = self.route_states.pop(0) if len(self.route_states) > 1 else self.route_states[0]
        routes = []
        if state is not None:
            routes = [{"DestinationCidrBlock": "0.0.0.0/0", "NatGatewayId": self.route_target, "State": state}]
        return {"RouteTables": [{"Routes": routes}]}

    def get_waiter(self, _name: str) -> Any:
        class Waiter:
            def wait(self, **_k: Any) -> None:
                return None

        return Waiter()

    def create_route(self, **_k: Any) -> None:
        self.calls.append("create_route")

    def replace_route(self, **_k: Any) -> None:
        self.calls.append("replace_route")

    def create_nat_gateway(self, **_k: Any) -> dict[str, Any]:
        self.calls.append("create_nat_gateway")
        return {"NatGateway": {"NatGatewayId": "nat-1"}}


def named(state: str, gateway_id: str = "nat-1") -> dict[str, Any]:
    return {"NatGatewayId": gateway_id, "State": state, "Tags": [{"Key": "Name", "Value": NAT_NAME}]}


def settings() -> scheduler.Settings:
    return scheduler.Settings("subnet-1", "rtb-1", "eipalloc-1", NAT_NAME, TAGS)


def test_route_verification_retries_until_active(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    fake = FakeEc2([[named("available")]], ["blackhole", "blackhole", "active"])
    sleeps: list[int] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    scheduler.action_up(fake, settings())
    assert fake.calls.count("replace_route") == 1
    assert sleeps == [scheduler.ROUTE_VERIFY_DELAY_SECONDS]


def test_route_that_never_becomes_active_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    fake = FakeEc2([[named("available")]], ["blackhole"])
    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(scheduler.SchedulerError, match="did not become active"),
    ):
        scheduler.action_up(fake, settings())
    assert fake.calls.count("replace_route") == scheduler.ROUTE_VERIFY_ATTEMPTS


def test_up_waits_for_a_deleting_gateway_before_creating(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    fake = FakeEc2(
        [
            [],  # live gateways
            [named("deleting", "nat-old")],  # deleting gateways
            [named("deleting", "nat-old")],  # wait poll 1
            [named("deleted", "nat-old")],  # wait poll 2
        ],
        [None, "active"],
    )
    scheduler.action_up(fake, settings())
    assert fake.calls.index("create_nat_gateway") > fake.calls.index("describe_nat_gateways", 3)


def test_down_ignores_a_gateway_already_deleting() -> None:
    deleted: list[str] = []

    class Fake(FakeEc2):
        def delete_nat_gateway(self, NatGatewayId: str) -> None:  # noqa: N803
            deleted.append(NatGatewayId)

    scheduler.action_down(Fake([[named("deleting")]], [None]), settings())
    assert deleted == []


def test_wait_deleted_polls_until_state_is_deleted(monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[int] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    fake = FakeEc2([[{"State": "deleting"}], [{"State": "deleting"}], [{"State": "deleted"}]], [None])
    scheduler._wait_deleted(fake, ["nat-1"])
    assert len(sleeps) == 2


def test_wait_deleted_times_out_with_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(time, "sleep", lambda _s: None)
    with pytest.raises(scheduler.SchedulerError, match="Timed out"):
        scheduler._wait_deleted(FakeEc2([[{"State": "deleting"}]], [None]), ["nat-1"])


def test_down_with_wait_waits_for_deletion(net: Network, monkeypatch: pytest.MonkeyPatch) -> None:
    run("up")
    gateway_id = net.gateways()[0]["NatGatewayId"]
    waited: list[list[str]] = []
    monkeypatch.setattr(scheduler, "_wait_deleted", lambda _ec2, ids: waited.append(ids))
    run("down", wait=True)
    assert waited == [[gateway_id]]


def test_nightly_down_does_not_wait(net: Network, monkeypatch: pytest.MonkeyPatch) -> None:
    run("up")
    waited: list[list[str]] = []
    monkeypatch.setattr(scheduler, "_wait_deleted", lambda _ec2, ids: waited.append(ids))
    run("down")
    assert waited == []


def test_failure_is_reported_by_raising_and_logging(net: Network, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR), pytest.raises(scheduler.SchedulerError, match="'up' or 'down'"):
        run("restart")
    assert any("NAT scheduler action restart failed" in r.getMessage() for r in caplog.records)


def test_ec2_error_makes_the_invocation_fail(net: Network, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("UnauthorizedOperation")

    monkeypatch.setattr(scheduler, "action_up", boom)
    with pytest.raises(RuntimeError, match="Unauthorized"):
        run("up")


def test_missing_and_malformed_settings_raise(net: Network, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TAGS_JSON", "[1]")
    with pytest.raises(scheduler.SchedulerError, match="JSON object"):
        run("up")
    monkeypatch.delenv("NAME_TAG")
    with pytest.raises(scheduler.SchedulerError, match="Missing"):
        run("up")


def set_keep(value: str) -> None:
    boto3.client("ssm", region_name="us-east-2").put_parameter(
        Name=KEEP_PARAMETER, Value=value, Type="String", Overwrite=True
    )


def test_down_is_skipped_while_the_keep_up_parameter_is_true(net: Network, caplog: pytest.LogCaptureFixture) -> None:
    run("up")
    set_keep("true")
    with caplog.at_level(logging.INFO):
        result = run("down")
    assert result == {"action": "down", "status": "skipped"}
    assert len(net.gateways()) == 1
    assert net.default_target() == net.gateways()[0]["NatGatewayId"]
    lines = [json.loads(r.getMessage()) for r in caplog.records if r.getMessage().startswith("{")]
    assert lines == [{"event": "nat_shutdown_skipped", "keep_up": "true"}]


def test_down_goes_ahead_again_once_the_parameter_is_false(net: Network) -> None:
    run("up")
    set_keep("true")
    assert run("down")["status"] == "skipped"
    set_keep("false")
    assert run("down")["status"] == "ok"
    assert net.gateways() == []


@pytest.mark.parametrize("value", [" TRUE ", "True"])
def test_true_is_compared_after_trimming_and_ignoring_case(net: Network, value: str) -> None:
    run("up")
    set_keep(value)
    assert run("down")["status"] == "skipped"
    assert len(net.gateways()) == 1


@pytest.mark.parametrize("value", ["yes", "1", "tomorrow", "2999-01-01T00:00:00Z"])
def test_other_values_count_as_false_with_a_warning(net: Network, caplog: pytest.LogCaptureFixture, value: str) -> None:
    run("up")
    set_keep(value)
    with caplog.at_level(logging.WARNING):
        run("down")
    assert net.gateways() == []
    assert any("not true or false" in r.getMessage() for r in caplog.records)


def test_false_goes_ahead_without_a_warning(net: Network, caplog: pytest.LogCaptureFixture) -> None:
    run("up")
    set_keep("false")
    with caplog.at_level(logging.WARNING):
        run("down")
    assert net.gateways() == []
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_a_missing_parameter_counts_as_false_with_a_warning(net: Network, caplog: pytest.LogCaptureFixture) -> None:
    run("up")
    with caplog.at_level(logging.WARNING):
        run("down")
    assert net.gateways() == []
    assert any("does not exist" in r.getMessage() for r in caplog.records)


def test_force_ignores_a_true_parameter(net: Network) -> None:
    run("up")
    set_keep("true")
    assert run("down", force=True)["status"] == "ok"
    assert net.gateways() == []


def test_force_must_be_the_boolean_true(net: Network) -> None:
    run("up")
    set_keep("true")
    assert run("down", force="true")["status"] == "skipped"
    assert len(net.gateways()) == 1


def test_up_ignores_the_parameter(net: Network) -> None:
    set_keep("true")
    run("up")
    assert len(net.gateways()) == 1


def test_a_read_error_skips_the_shutdown_and_fails_so_the_alarm_fires(
    net: Network, monkeypatch: pytest.MonkeyPatch
) -> None:
    run("up")
    real = boto3.client

    def denied(service: str, *args: Any, **kwargs: Any) -> Any:
        client = real(service, *args, **kwargs)
        if service == "ssm":

            def boom(**_k: Any) -> None:
                raise client.exceptions.ClientError({"Error": {"Code": "AccessDeniedException"}}, "GetParameter")

            client.get_parameter = boom
        return client

    monkeypatch.setattr(boto3, "client", denied)
    with pytest.raises(Exception, match="AccessDenied"):
        run("down")
    assert len(net.gateways()) == 1


def test_without_a_configured_parameter_down_behaves_as_before(net: Network, monkeypatch: pytest.MonkeyPatch) -> None:
    run("up")
    monkeypatch.delenv("KEEP_UP_PARAMETER")
    run("down")
    assert net.gateways() == []

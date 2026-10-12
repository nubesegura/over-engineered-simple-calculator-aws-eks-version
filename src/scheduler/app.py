"""Lambda that removes (action "down") or creates (action "up") the NAT gateway of the dev VPC.

Invoked by two EventBridge Scheduler schedules (22:00 and 08:00 Panama time) and, in the destroy
workflow, by hand with {"action": "down", "wait": true}.

Configuration comes from environment variables set by templates/scheduler.yaml:
PUBLIC_SUBNET_ID, PRIVATE_ROUTE_TABLE_ID, EIP_ALLOCATION_ID, NAME_TAG, TAGS_JSON and KEEP_UP_PARAMETER.

Keeping the NAT gateway up: the SSM parameter named by KEEP_UP_PARAMETER holds the string "true" or "false"
(default "false"). When "down" reads "true" it does nothing, logs one JSON line (nat_shutdown_skipped) and
succeeds (no alarm); the NAT gateway stays up until a later scheduled "down" reads "false". Nothing resets the
parameter automatically: the owner sets it back to "false". Decisions on the odd cases:
- The value is compared after trimming spaces and ignoring case ("True " counts as true).
- Any other value, or a parameter that does not exist: counts as "false" with a warning. A bad value must not
  keep a paid resource running, and the template always creates the parameter, so absence means "not configured".
- Any other read error (permission, throttling, network): "down" FAILS and deletes nothing. The shutdown is
  skipped (the environment stays available, the cost of one more night is small) and the raised error trips the
  existing alarm, so the owner notices. It is never silently ignored.
- {"force": true} ignores the parameter (the destroy workflow, which must remove the NAT gateway).
- "up" never reads the parameter.

Both actions are idempotent and safe to run twice. The private default route never points to a
gateway that is being removed: "down" deletes the route first. "up" creates the gateway with the
existing Elastic IP, waits until it is available, creates the route, then verifies the route and
retries. A failure is logged and raised, so the invocation fails (Lambda `Errors` metric, alarm to
the alerts topic) and the next run resumes from whatever state the previous one left.
"""

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.config import Config

LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)

DEFAULT_CIDR = "0.0.0.0/0"
LIVE_STATES = ["pending", "available"]
ALL_STATES = ["pending", "available", "deleting"]
WAIT_DELAY_SECONDS = 5
WAIT_MAX_ATTEMPTS = 30
ROUTE_VERIFY_ATTEMPTS = 5
ROUTE_VERIFY_DELAY_SECONDS = 3
ACTIONS = ("up", "down")
CLIENT_CONFIG = Config(
    connect_timeout=5,
    read_timeout=15,
    retries={"max_attempts": 5, "mode": "standard"},
)


class SchedulerError(Exception):
    """Raised when the requested action cannot be completed."""


@dataclass(frozen=True)
class Settings:
    """Validated configuration of the function."""

    public_subnet_id: str
    private_route_table_id: str
    eip_allocation_id: str
    name_tag: str
    tags: dict[str, str]
    keep_up_parameter: str = ""


def load_settings() -> Settings:
    """Read and validate the environment variables, failing fast when one is missing."""
    names = ["PUBLIC_SUBNET_ID", "PRIVATE_ROUTE_TABLE_ID", "EIP_ALLOCATION_ID", "NAME_TAG", "TAGS_JSON"]
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise SchedulerError(f"Missing environment variables: {missing}")
    tags = json.loads(os.environ["TAGS_JSON"])
    if not isinstance(tags, dict):
        raise SchedulerError("TAGS_JSON must be a JSON object")
    return Settings(
        public_subnet_id=os.environ["PUBLIC_SUBNET_ID"],
        private_route_table_id=os.environ["PRIVATE_ROUTE_TABLE_ID"],
        eip_allocation_id=os.environ["EIP_ALLOCATION_ID"],
        name_tag=os.environ["NAME_TAG"],
        tags={str(k): str(v) for k, v in tags.items()},
        keep_up_parameter=os.environ.get("KEEP_UP_PARAMETER", ""),
    )


def keep_nat_up(settings: Settings) -> bool:
    """True when the keep-up parameter says the NAT gateway must not be removed.

    A read error other than a missing parameter is raised (see the module docstring).
    """
    if not settings.keep_up_parameter:
        return False
    ssm = boto3.client("ssm", config=CLIENT_CONFIG)
    try:
        value = str(ssm.get_parameter(Name=settings.keep_up_parameter)["Parameter"]["Value"])
    except ssm.exceptions.ParameterNotFound:
        LOGGER.warning("Keep-up parameter %s does not exist: treated as false", settings.keep_up_parameter)
        return False
    normalized = value.strip().lower()
    if normalized not in ("true", "false"):
        LOGGER.warning("Keep-up parameter %s is not true or false: treated as false", settings.keep_up_parameter)
    return normalized == "true"


def _has_name(gateway: dict[str, Any], name: str) -> bool:
    tags = {t["Key"]: t["Value"] for t in gateway.get("Tags", [])}
    return tags.get("Name") == name


def _project_gateways(ec2: Any, settings: Settings, states: list[str]) -> list[dict[str, Any]]:
    """NAT gateways of this project (by the Name tag) in the given states."""
    response = ec2.describe_nat_gateways(
        Filter=[
            {"Name": "tag:Name", "Values": [settings.name_tag]},
            {"Name": "state", "Values": states},
        ]
    )
    return [g for g in response["NatGateways"] if _has_name(g, settings.name_tag)]


def _default_route(ec2: Any, route_table_id: str) -> dict[str, Any] | None:
    tables = ec2.describe_route_tables(RouteTableIds=[route_table_id])["RouteTables"]
    for route in tables[0]["Routes"]:
        if route.get("DestinationCidrBlock") == DEFAULT_CIDR:
            found: dict[str, Any] = route
            return found
    return None


def _wait_available(ec2: Any, gateway_id: str) -> None:
    waiter = ec2.get_waiter("nat_gateway_available")
    waiter.wait(
        NatGatewayIds=[gateway_id],
        WaiterConfig={"Delay": WAIT_DELAY_SECONDS, "MaxAttempts": WAIT_MAX_ATTEMPTS},
    )


def _wait_deleted(ec2: Any, gateway_ids: list[str]) -> None:
    """Poll until every gateway is deleted (a missing gateway counts as deleted)."""
    for _ in range(WAIT_MAX_ATTEMPTS):
        found = ec2.describe_nat_gateways(NatGatewayIds=gateway_ids)["NatGateways"]
        if all(g["State"] == "deleted" for g in found):
            return
        time.sleep(WAIT_DELAY_SECONDS)
    raise SchedulerError("Timed out waiting for the NAT gateway deletion")


def _create_gateway(ec2: Any, settings: Settings) -> str:
    deleting = _project_gateways(ec2, settings, ["deleting"])
    if deleting:
        # The Elastic IP is still attached to the old gateway until it is deleted.
        _wait_deleted(ec2, [g["NatGatewayId"] for g in deleting])
    tags = [{"Key": "Name", "Value": settings.name_tag}]
    tags += [{"Key": k, "Value": v} for k, v in settings.tags.items() if k != "Name"]
    response = ec2.create_nat_gateway(
        SubnetId=settings.public_subnet_id,
        AllocationId=settings.eip_allocation_id,
        TagSpecifications=[{"ResourceType": "natgateway", "Tags": tags}],
    )
    gateway_id: str = response["NatGateway"]["NatGatewayId"]
    LOGGER.info("Created NAT gateway %s", gateway_id)
    return gateway_id


def _point_route(ec2: Any, settings: Settings, gateway_id: str) -> None:
    route = _default_route(ec2, settings.private_route_table_id)
    if route and route.get("NatGatewayId") == gateway_id and route.get("State") == "active":
        return
    args = {
        "RouteTableId": settings.private_route_table_id,
        "DestinationCidrBlock": DEFAULT_CIDR,
        "NatGatewayId": gateway_id,
    }
    if route is None:
        ec2.create_route(**args)
    else:
        ec2.replace_route(**args)
    LOGGER.info("Default route of %s now targets %s", settings.private_route_table_id, gateway_id)


def _route_is_good(ec2: Any, settings: Settings, gateway_id: str) -> bool:
    route = _default_route(ec2, settings.private_route_table_id)
    return bool(route and route.get("NatGatewayId") == gateway_id and route.get("State") == "active")


def _ensure_route(ec2: Any, settings: Settings, gateway_id: str) -> None:
    """Point the default route at the gateway, verify it, and retry a few times before failing."""
    for attempt in range(1, ROUTE_VERIFY_ATTEMPTS + 1):
        _point_route(ec2, settings, gateway_id)
        if _route_is_good(ec2, settings, gateway_id):
            return
        LOGGER.warning("Default route not active yet (attempt %s of %s)", attempt, ROUTE_VERIFY_ATTEMPTS)
        time.sleep(ROUTE_VERIFY_DELAY_SECONDS)
    raise SchedulerError(f"Default route of {settings.private_route_table_id} did not become active")


def action_up(ec2: Any, settings: Settings) -> None:
    """Ensure one NAT gateway exists, is available and is the private default route target."""
    live = _project_gateways(ec2, settings, LIVE_STATES)
    gateway_id = live[0]["NatGatewayId"] if live else _create_gateway(ec2, settings)
    _wait_available(ec2, gateway_id)
    _ensure_route(ec2, settings, gateway_id)


def action_down(ec2: Any, settings: Settings, wait: bool = False) -> None:
    """Remove the default route when it targets this project's gateway, then the gateway.

    With wait=True (destroy workflow) it also waits until the gateways are deleted, so the Elastic IP
    and the subnets can be removed right after. A missing gateway or route is not an error.
    """
    gateways = _project_gateways(ec2, settings, ALL_STATES)
    ids = {g["NatGatewayId"] for g in gateways}
    route = _default_route(ec2, settings.private_route_table_id)
    if route is not None and route.get("NatGatewayId") in ids:
        ec2.delete_route(RouteTableId=settings.private_route_table_id, DestinationCidrBlock=DEFAULT_CIDR)
        LOGGER.info("Deleted default route of %s", settings.private_route_table_id)
    for gateway in gateways:
        if gateway["State"] in LIVE_STATES:
            ec2.delete_nat_gateway(NatGatewayId=gateway["NatGatewayId"])
            LOGGER.info("Deleting NAT gateway %s", gateway["NatGatewayId"])
    if wait and ids:
        _wait_deleted(ec2, sorted(ids))


def handler(event: dict[str, Any], _context: object) -> dict[str, str]:
    """Entrypoint: event {"action": "up" | "down"}; "down" also takes {"wait": true} and {"force": true}."""
    action = event.get("action")
    try:
        if action not in ACTIONS:
            raise SchedulerError("Event action must be 'up' or 'down'")
        settings = load_settings()
        ec2 = boto3.client("ec2", config=CLIENT_CONFIG)
        if action == "up":
            action_up(ec2, settings)
        else:
            if event.get("force") is not True and keep_nat_up(settings):
                LOGGER.info(json.dumps({"event": "nat_shutdown_skipped", "keep_up": "true"}))
                return {"action": "down", "status": "skipped"}
            action_down(ec2, settings, wait=event.get("wait") is True)
    except Exception:
        LOGGER.exception("NAT scheduler action %s failed", action)
        raise
    LOGGER.info("NAT scheduler action %s done", action)
    return {"action": str(action), "status": "ok"}

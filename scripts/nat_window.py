"""Preflight: refuse to start a deployment that needs the NAT gateway outside the hours it is up.

The NAT scheduler (dev) removes the NAT gateway at 22:00 and recreates it at 08:00 Panama time. The units after
`rotation` (cluster, deploy, bastion) need outbound access: with the NAT down the node group cannot pull images
and fails after about 25 minutes, and the add-ons build cannot download charts. A deployment that includes them
may therefore start only between 08:00 and 20:30 (at least 90 minutes before the shutdown) unless the owner
overrides the check.

    python scripts/nat_window.py check --env dev --through bastion [--ignore]

Exit 0 when the deployment may start, 1 when it must wait. Standard library only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

REPO_ROOT = Path(__file__).resolve().parents[1]
STACKS_FILE = REPO_ROOT / "templates" / "stacks.json"
TIME_ZONE = "America/Panama"
WINDOW_START = dt.time(8, 0)
WINDOW_END = dt.time(20, 30)
LAST_UNIT_WITHOUT_NAT = "rotation"


def panama_now(now: dt.datetime | None = None) -> dt.datetime:
    """Current time in Panama (fixed UTC-5, no daylight saving, when the tz database is not installed)."""
    moment = now or dt.datetime.now(dt.UTC)
    try:
        zone: dt.tzinfo = ZoneInfo(TIME_ZONE)
    except ZoneInfoNotFoundError:
        zone = dt.timezone(dt.timedelta(hours=-5))
    return moment.astimezone(zone)


def needs_nat(units: list[str]) -> bool:
    """True when the deployment includes a unit that comes after `rotation` in the stack list."""
    if LAST_UNIT_WITHOUT_NAT not in units:
        return False
    return units.index(LAST_UNIT_WITHOUT_NAT) < len(units) - 1


def in_window(local: dt.datetime) -> bool:
    return WINDOW_START <= local.time().replace(second=0, microsecond=0) <= WINDOW_END


def check(units: list[str], now: dt.datetime | None = None, ignore: bool = False) -> tuple[bool, str]:
    """Return (allowed, message)."""
    local = panama_now(now)
    window = f"{WINDOW_START:%H:%M}-{WINDOW_END:%H:%M}"
    stamp = f"{local:%H:%M} {TIME_ZONE}"
    if not needs_nat(units):
        return True, f"No unit after {LAST_UNIT_WITHOUT_NAT} is deployed: the NAT window does not apply ({stamp})."
    if in_window(local):
        return True, f"It is {stamp}, inside the NAT window {window}: the deployment may start."
    if ignore:
        return True, f"It is {stamp}, outside the NAT window {window}, but ignore_nat_window is true: continuing."
    return False, (
        f"It is {stamp}, outside the window {window} (the NAT gateway is up at least 90 more minutes only inside "
        f"it; the scheduler removes it at 22:00 and recreates it at 08:00). The cluster stack would fail after "
        f"about 25 minutes because the nodes cannot pull images. Run it inside the window, or start it with "
        f"ignore_nat_window=true after turning the NAT on."
    )


def deployed_units(env: str, through: str | None) -> list[str]:
    units = [e["unit"] for e in json.loads(STACKS_FILE.read_text(encoding="utf-8"))["environments"][env]]
    if through is None:
        return units
    if through not in units:
        raise SystemExit(f"unknown unit {through}")
    return units[: units.index(through) + 1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    cmd = sub.add_parser("check")
    cmd.add_argument("--env", required=True, choices=["dev", "prod"])
    cmd.add_argument("--through", default=None)
    cmd.add_argument("--ignore", action="store_true")
    args = parser.parse_args(argv)
    if args.env != "dev":
        print("The NAT window applies to dev only (prod keeps the NAT up).")
        return 0
    allowed, message = check(deployed_units(args.env, args.through), ignore=args.ignore)
    print(message if allowed else f"::error::{message}")
    return 0 if allowed else 1


if __name__ == "__main__":
    sys.exit(main())

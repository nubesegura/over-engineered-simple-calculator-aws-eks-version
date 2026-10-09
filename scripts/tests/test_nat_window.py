"""Tests of the NAT window preflight (scripts/nat_window.py)."""

from __future__ import annotations

import datetime as dt

import nat_window as nw
import pytest

BASE = ["security", "network", "registry", "scheduler", "database", "rotation"]
FULL = [*BASE, "cluster", "deploy", "bastion"]
PANAMA = dt.timezone(dt.timedelta(hours=-5))


def at(hour: int, minute: int = 0) -> dt.datetime:
    """A UTC instant whose Panama time (UTC-5) is hour:minute."""
    return dt.datetime(2026, 10, 8, hour, minute, tzinfo=PANAMA).astimezone(dt.UTC)


@pytest.mark.parametrize(("hour", "minute"), [(8, 0), (12, 0), (20, 30)])
def test_inside_the_window_and_on_its_boundaries(hour: int, minute: int) -> None:
    allowed, message = nw.check(FULL, at(hour, minute))
    assert allowed and "inside" in message and "America/Panama" in message and "08:00-20:30" in message


@pytest.mark.parametrize(("hour", "minute"), [(7, 59), (20, 31), (22, 0), (3, 0), (0, 0)])
def test_outside_the_window_fails_with_a_clear_message(hour: int, minute: int) -> None:
    allowed, message = nw.check(FULL, at(hour, minute))
    assert not allowed
    assert f"{hour:02d}:{minute:02d} America/Panama" in message and "08:00-20:30" in message
    assert "ignore_nat_window" in message


def test_override_lets_it_through_outside_the_window() -> None:
    allowed, message = nw.check(FULL, at(23, 0), ignore=True)
    assert allowed and "ignore_nat_window is true" in message


def test_deployments_without_units_after_rotation_are_not_limited() -> None:
    assert nw.check(BASE, at(23, 0))[0]
    assert not nw.needs_nat(BASE)
    assert nw.needs_nat([*BASE, "cluster"])
    assert not nw.needs_nat(["security"])


def test_through_selects_the_units() -> None:
    assert nw.deployed_units("dev", "rotation")[-1] == "rotation"
    assert "cluster" in nw.deployed_units("dev", "cluster")
    assert nw.deployed_units("dev", None)[0] == "security"


def test_cli_outside_the_window(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(nw, "panama_now", lambda now=None: at(23, 0).astimezone(PANAMA))
    assert nw.main(["check", "--env", "dev", "--through", "bastion"]) == 1
    assert "::error::" in capsys.readouterr().out
    assert nw.main(["check", "--env", "dev", "--through", "bastion", "--ignore"]) == 0
    assert nw.main(["check", "--env", "dev", "--through", "rotation"]) == 0
    assert nw.main(["check", "--env", "prod"]) == 0

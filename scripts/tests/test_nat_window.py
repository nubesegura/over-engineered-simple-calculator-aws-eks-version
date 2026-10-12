"""Tests of the NAT window preflight (scripts/nat_window.py)."""

from __future__ import annotations

import datetime as dt
import subprocess

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
    monkeypatch.setattr(nw, "read_keep_up", lambda _context, _env: False)
    assert nw.main(["check", "--env", "dev", "--through", "bastion"]) == 1
    assert "::error::" in capsys.readouterr().out
    assert nw.main(["check", "--env", "dev", "--through", "bastion", "--ignore"]) == 0
    assert nw.main(["check", "--env", "dev", "--through", "rotation"]) == 0
    assert nw.main(["check", "--env", "prod"]) == 0


def test_a_true_keep_up_setting_opens_the_window_at_any_time() -> None:
    for hour in (21, 23, 3):
        allowed, message = nw.check(FULL, at(hour, 0), keep_up=True)
        assert allowed
        assert "keep-up setting is true" in message


def test_a_false_keep_up_setting_changes_nothing() -> None:
    assert not nw.check(FULL, at(21, 0), keep_up=False)[0]
    allowed, message = nw.check(FULL, at(10, 0), keep_up=True)
    assert allowed and "inside" in message


def test_read_keep_up_reads_the_parameter(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake(command: list[str], **_k: object) -> subprocess.CompletedProcess[str]:
        seen.append(command)
        return subprocess.CompletedProcess(command, 0, "True\n", "")

    monkeypatch.setattr(subprocess, "run", fake)
    assert nw.read_keep_up("oecalc", "dev") is True
    assert "/oecalc/dev/eks/nat-keep-up" in seen[0]
    assert seen[0][:3] == ["aws", "ssm", "get-parameter"]


@pytest.mark.parametrize("stdout", ["false\n", "yes\n", ""])
def test_read_keep_up_is_false_for_anything_but_true(monkeypatch: pytest.MonkeyPatch, stdout: str) -> None:
    monkeypatch.setattr(subprocess, "run", lambda c, **_k: subprocess.CompletedProcess(c, 0, stdout, ""))
    assert nw.read_keep_up("oecalc", "dev") is False


def test_read_keep_up_falls_back_when_the_call_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", lambda c, **_k: subprocess.CompletedProcess(c, 255, "true", "denied"))
    assert nw.read_keep_up("oecalc", "dev") is False

    def missing(*_a: object, **_k: object) -> None:
        raise FileNotFoundError("aws")

    monkeypatch.setattr(subprocess, "run", missing)
    assert nw.read_keep_up("oecalc", "dev") is False

    def slow(*_a: object, **_k: object) -> None:
        raise subprocess.TimeoutExpired("aws", 30)

    monkeypatch.setattr(subprocess, "run", slow)
    assert nw.read_keep_up("oecalc", "dev") is False


def test_cli_uses_the_keep_up_setting_and_falls_back_without_crashing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(nw, "panama_now", lambda now=None: at(21, 0).astimezone(PANAMA))
    monkeypatch.setattr(nw, "read_keep_up", lambda _context, _env: True)
    assert nw.main(["check", "--env", "dev", "--through", "bastion"]) == 0
    assert "keep-up setting is true" in capsys.readouterr().out
    monkeypatch.setattr(nw, "read_keep_up", lambda _context, _env: False)
    assert nw.main(["check", "--env", "dev", "--through", "bastion"]) == 1

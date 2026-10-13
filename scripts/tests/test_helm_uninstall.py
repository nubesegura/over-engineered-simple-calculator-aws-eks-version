"""Tests of scripts/helm_uninstall.sh (task 7.1): fake helm and kubectl record the calls, nothing touches a cluster."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from test_helm_deploy import BASH, REPO, needs_bash

SCRIPT = REPO / "scripts" / "helm_uninstall.sh"
# FAKE_RELEASES lists the installed releases; FAKE_DOWN makes the cluster unreachable; FAKE_NO_CRD hides the CRD.
FAKE_HELM = r"""#!/usr/bin/env bash
echo "helm $*" >> "$CALL_LOG"
if [ "$1" = "list" ]; then
  [ -z "${FAKE_DOWN:-}" ] || exit 1
  for release in $FAKE_RELEASES; do
    case "$*" in *"^${release}\$"*) echo "$release" ;; esac
  done
fi
exit 0
"""
FAKE_KUBECTL = r"""#!/usr/bin/env bash
echo "kubectl $*" >> "$CALL_LOG"
[ -z "${FAKE_DOWN:-}" ] || exit 1
if [ "$1" = "get" ] && [ "$2" = "crd" ] && [ -n "${FAKE_NO_CRD:-}" ]; then exit 1; fi
if [ "$1" = "get" ] && [ "$2" = "namespace" ] && [ -n "${FAKE_NO_NAMESPACE:-}" ]; then exit 1; fi
if [ "$1" = "--namespace" ] && [ -n "${FAKE_INGRESS_STUCK:-}" ]; then exit 1; fi
exit 0
"""


@pytest.fixture
def run_script(tmp_path: Path) -> Any:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("helm", FAKE_HELM), ("kubectl", FAKE_KUBECTL)):
        (bin_dir / name).write_text(body, encoding="utf-8", newline="\n")
        (bin_dir / name).chmod(0o755)
    log = tmp_path / "calls.log"

    def run(**overrides: str | None) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        assert BASH is not None
        log.unlink(missing_ok=True)
        env = {
            **os.environ,
            "CLUSTER_NAME": "eksc-useast2-oecalc-dev",
            "FAKE_RELEASES": "calculator cluster-autoscaler aws-load-balancer-controller",
            "CALL_LOG": log.as_posix(),
            "PATH": f"{bin_dir.as_posix()}{os.pathsep}{os.environ['PATH']}",
        }
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        result = subprocess.run(
            [BASH, SCRIPT.relative_to(REPO).as_posix()], cwd=REPO, env=env, capture_output=True, text=True, check=False
        )
        calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
        return result, calls

    return run


@needs_bash
def test_removes_ingress_then_the_release_then_the_controllers_in_reverse_order(run_script: Any) -> None:
    result, calls = run_script()
    assert result.returncode == 0, result.stderr
    uninstalls = [c.split()[2] for c in calls if c.startswith("helm uninstall")]
    assert uninstalls == ["calculator", "cluster-autoscaler", "aws-load-balancer-controller"]
    position = {
        key: next(i for i, c in enumerate(calls) if key in c)
        for key in (
            "delete ingress",
            "helm uninstall calculator",
            "delete namespace",
            "delete targetgroupbindings",
            "helm uninstall cluster-autoscaler",
            "helm uninstall aws-load-balancer-controller",
        )
    }
    assert list(position.values()) == sorted(position.values())
    assert all("--wait" in c for c in calls if c.startswith("helm uninstall"))


@needs_bash
def test_releases_that_are_absent_are_skipped(run_script: Any) -> None:
    result, calls = run_script(FAKE_RELEASES="", FAKE_NO_CRD="1", FAKE_NO_NAMESPACE="1")
    assert result.returncode == 0, result.stderr
    assert not [c for c in calls if c.startswith("helm uninstall")]
    assert not [c for c in calls if "delete ingress" in c or "delete targetgroupbindings" in c]
    assert "Release calculator is not installed in calculator: skipped" in result.stdout


@needs_bash
def test_an_unreachable_cluster_fails_instead_of_assuming_nothing_is_installed(run_script: Any) -> None:
    result, calls = run_script(FAKE_DOWN="1")
    assert result.returncode != 0
    assert "does not answer" in result.stderr
    assert not [c for c in calls if c.startswith("helm uninstall")]


@needs_bash
def test_a_stuck_ingress_stops_before_the_controller_is_removed(run_script: Any) -> None:
    result, calls = run_script(FAKE_INGRESS_STUCK="1")
    assert result.returncode != 0 and "ingress objects" in result.stderr
    assert not [c for c in calls if c.startswith("helm uninstall")]


@needs_bash
def test_the_cluster_name_is_required(run_script: Any) -> None:
    result, _ = run_script(CLUSTER_NAME=None)
    assert result.returncode != 0 and "CLUSTER_NAME is required" in result.stderr

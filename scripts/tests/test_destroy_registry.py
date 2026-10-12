"""Tests of scripts/destroy_registry.sh (task 5.4) with a fake aws executable: nothing touches AWS."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from test_helm_deploy import BASH, REPO, needs_bash

SCRIPT = REPO / "scripts" / "destroy_registry.sh"
FAKE_AWS = r"""#!/usr/bin/env bash
echo "aws $*" >> "$CALL_LOG"
case "$FAKE_MODE" in
  missing) echo "An error occurred (ParameterNotFound) when calling the DeleteParameter operation" >&2; exit 254 ;;
  denied) echo "An error occurred (AccessDeniedException) when calling the DeleteParameter operation" >&2; exit 254 ;;
esac
exit 0
"""


@pytest.fixture
def run_script(tmp_path: Path) -> Any:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "aws").write_text(FAKE_AWS, encoding="utf-8", newline="\n")
    (bin_dir / "aws").chmod(0o755)
    log = tmp_path / "calls.log"

    def run(mode: str = "ok", **overrides: str | None) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        assert BASH is not None
        log.unlink(missing_ok=True)
        env = {
            **os.environ,
            "ENV_TYPE": "dev",
            "CONTEXT": "oecalc",
            "FAKE_MODE": mode,
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
        return result, (log.read_text(encoding="utf-8").splitlines() if log.exists() else [])

    return run


@needs_bash
def test_both_parameters_are_removed_one_by_one(run_script: Any) -> None:
    result, calls = run_script()
    assert result.returncode == 0, result.stderr
    assert calls == [
        "aws ssm delete-parameter --name /oecalc/dev/api-backends/eks/dns-name",
        "aws ssm delete-parameter --name /oecalc/dev/api-backends/eks/hosted-zone-id",
    ]
    assert "Registration removed (2 of 2" in result.stdout and "shared-resources" in result.stdout


@needs_bash
def test_parameters_that_are_already_gone_do_not_fail_a_repeated_destroy(run_script: Any) -> None:
    result, calls = run_script("missing")
    assert result.returncode == 0 and len(calls) == 2
    assert "Already absent" in result.stdout and "0 of 2" in result.stdout


@needs_bash
def test_any_other_error_stops_the_destroy(run_script: Any) -> None:
    result, calls = run_script("denied")
    assert result.returncode != 0 and len(calls) == 1
    assert "cannot remove /oecalc/dev/api-backends/eks/dns-name" in result.stderr


@needs_bash
@pytest.mark.parametrize(
    ("name", "value"), [("ENV_TYPE", None), ("CONTEXT", None), ("ENV_TYPE", "qa"), ("CONTEXT", "../x")]
)
def test_bad_inputs_are_refused_before_any_call(run_script: Any, name: str, value: str | None) -> None:
    result, calls = run_script(**{name: value})
    assert result.returncode != 0 and calls == []

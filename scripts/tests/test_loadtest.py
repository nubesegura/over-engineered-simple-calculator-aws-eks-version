"""Tests of scripts/loadtest/run.sh (task 6.3): a fake `curl` records its calls, nothing touches the network."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "loadtest" / "run.sh"
TOKEN = "aaaa.bbbb.SECRETtokenvalue"


def find_bash() -> str | None:
    for candidate in (shutil.which("bash"), r"C:\Program Files\Git\bin\bash.exe"):
        if candidate and "system32" not in candidate.lower() and Path(candidate).exists():
            return candidate
    return None


BASH = find_bash()
pytestmark = pytest.mark.skipif(BASH is None, reason="bash is not available")

# The fake records the arguments and the content of the header file, and answers with the status in FAKE_STATUS
FAKE_CURL = r"""#!/usr/bin/env bash
echo "$*" >> "$CURL_LOG"
for arg in "$@"; do
  case "$arg" in @*) cat "${arg#@}" >> "$HEADER_LOG" ;; esac
done
printf '%s' "${FAKE_STATUS:-200}"
"""


def run(tmp_path: Path, *args: str, stdin: str | None = None, status: str = "200") -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    curl = bin_dir / "curl"
    curl.write_bytes(FAKE_CURL.encode("utf-8"))
    curl.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "CURL_LOG": str(tmp_path / "curl.log"),
        "HEADER_LOG": str(tmp_path / "headers.log"),
        "FAKE_STATUS": status,
    }
    return subprocess.run(
        [BASH, str(SCRIPT), *args], input=stdin or "", capture_output=True, text=True, env=env, check=False, timeout=120
    )


def test_sends_every_operation_with_the_token_only_in_the_header_file(tmp_path: Path) -> None:
    result = run(
        tmp_path, "--url", "https://api.example.invalid/", "--duration", "10", "--concurrency", "2", stdin=TOKEN
    )
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / "curl.log").read_text(encoding="utf-8")
    for op in ("add", "sub", "mul", "div"):
        assert "-X POST" in calls and f"https://api.example.invalid/api/eks/v1/{op}" in calls
    assert "https://api.example.invalid/api/eks/v1/history?limit=20" in calls
    assert TOKEN not in calls and TOKEN not in result.stdout and TOKEN not in result.stderr
    assert f"Authorization: Bearer {TOKEN}" in (tmp_path / "headers.log").read_text(encoding="utf-8")
    assert "200" in result.stdout and "kubectl get hpa" in result.stdout and "kubectl get nodes" in result.stdout


def test_reads_the_token_from_a_file_and_stops_on_401(tmp_path: Path) -> None:
    token_file = tmp_path / "token.txt"
    token_file.write_text(TOKEN + "\n", encoding="utf-8")
    result = run(tmp_path, "--url", "https://api.example.invalid", "--token-file", str(token_file), status="401")
    assert result.returncode == 0
    assert "401" in result.stderr and TOKEN not in result.stdout + result.stderr
    assert len((tmp_path / "curl.log").read_text(encoding="utf-8").splitlines()) <= 20  # every worker stopped at once


@pytest.mark.parametrize(
    ("args", "stdin", "message"),
    [
        (["--url", "http://api.example.invalid"], TOKEN, "must start with https://"),
        (["--url", "https://api.example.invalid", "--duration", "5"], TOKEN, "--duration"),
        (["--url", "https://api.example.invalid", "--concurrency", "0"], TOKEN, "--concurrency"),
        (["--url", "https://api.example.invalid"], "not-a-jwt", "not a JWT"),
        (["--token", TOKEN, "--url", "https://api.example.invalid"], TOKEN, "never an argument"),
        ([], TOKEN, "--url is required"),
    ],
)
def test_bad_input_stops_before_any_request(tmp_path: Path, args: list[str], stdin: str, message: str) -> None:
    result = run(tmp_path, *args, stdin=stdin)
    assert result.returncode != 0 and message in result.stderr
    assert not (tmp_path / "curl.log").exists()
    assert TOKEN not in result.stderr


def test_print_watch_needs_no_token_or_url(tmp_path: Path) -> None:
    result = run(tmp_path, "--print-watch")
    assert result.returncode == 0
    for text in ("kubectl get hpa", "kubectl get pods", "kubectl get nodes", "cluster-autoscaler"):
        assert text in result.stdout

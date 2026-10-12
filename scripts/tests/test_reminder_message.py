"""The weekly reminder text and the parameter it becomes (no AWS call: the version service is faked)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import reminder_message as script
from conftest import FakeCli, Reply

KEY = "eks describe-cluster-versions"
OFFERED = {
    "clusterVersions": [
        {
            "clusterVersion": "1.37",
            "versionStatus": "STANDARD_SUPPORT",
            "endOfStandardSupportDate": "2027-12-01T00:00:00+00:00",
        },
    ]
}


def test_message_has_cluster_version_date_days_left_and_the_cost_line() -> None:
    text = script.build_message("dev", "eks-oecalc-dev", "1.37", "2027-12-01", today=date(2027, 11, 21))
    assert "EKS cluster eks-oecalc-dev (dev) is still running" in text
    assert "Kubernetes version: 1.37" in text and "ends 2027-12-01 UTC (10 days left)" in text
    assert "USD 0.10 per hour for the control plane" in text
    assert "Destroy DEV" in text


def test_message_after_the_end_date_says_how_long_ago() -> None:
    text = script.build_message("dev", "c", "1.37", "2027-12-01", today=date(2027, 12, 4))
    assert "(ended 3 days ago)" in text


def test_message_without_a_date_says_where_to_find_it() -> None:
    text = script.build_message("prod", "c", "1.37", None)
    assert "end of standard support of Kubernetes 1.37 is unknown" in text
    assert "eks_latest_version.py --env prod" in text


def test_main_writes_the_message_as_one_json_line_of_the_environment_file(
    cli: FakeCli, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.on(KEY, OFFERED)
    target = tmp_path / "github_env"
    assert script.main(["--env", "dev", "--cluster-name", "eks-oecalc-dev", "--github-env", str(target)]) == 0
    (line,) = target.read_text(encoding="utf-8").splitlines()
    name, _, value = line.partition("=")
    assert name == "CFN_PARAM_ReminderMessageJson"
    decoded: Any = json.loads(value)
    assert decoded == capsys.readouterr().out.rstrip("\n") and "ends 2027-12-01 UTC" in decoded
    assert len(value) <= 2000  # MaxLength of the template parameter


def test_an_unreachable_version_service_does_not_block_the_deployment(
    cli: FakeCli, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.on(KEY, Reply(returncode=255, stderr="boom"))
    assert script.main(["--env", "dev", "--cluster-name", "c"]) == 0
    captured = capsys.readouterr()
    assert "::warning::" in captured.err and "is unknown" in captured.out


def test_a_version_the_service_does_not_offer_gives_an_unknown_date(cli: FakeCli) -> None:
    cli.on(KEY, OFFERED)
    assert script.support_end_date("1.99") is None
    assert script.support_end_date("1.37") == "2027-12-01"

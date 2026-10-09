"""The Kubernetes version check, with faked answers of `aws eks describe-cluster-versions`."""

from __future__ import annotations

import json
from typing import Any

import eks_latest_version as script
import pytest
from conftest import FakeCli, Reply

KEY = "eks describe-cluster-versions"


def offered(*items: tuple[str, str, str]) -> dict[str, Any]:
    return {
        "clusterVersions": [
            {"clusterVersion": v, "versionStatus": s, "status": s, "endOfStandardSupportDate": d} for v, s, d in items
        ]
    }


# 1.37 ends 2027-11-30 19:00 at UTC-5, which is already 2027-12-01 in UTC.
CURRENT = offered(
    ("1.37", "STANDARD_SUPPORT", "2027-11-30T19:00:00-05:00"),
    ("1.36", "STANDARD_SUPPORT", "2027-08-01T19:00:00-05:00"),
    ("1.9", "EXTENDED_SUPPORT", "2020-01-01T00:00:00+00:00"),
    ("1.33", "EXTENDED_SUPPORT", "2026-07-28T19:00:00-05:00"),
)


def test_newest_in_standard_support_passes_and_prints_the_utc_end_date(
    cli: FakeCli, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.on(KEY, CURRENT)
    assert script.main(["--version", "1.37"]) == script.EXIT_OK
    output = capsys.readouterr().out
    assert "Newest Kubernetes version in standard support: 1.37 (standard support ends 2027-12-01 UTC)" in output
    assert "OK" in output
    assert cli.called(KEY)[0][-4:] == ["--region", "us-east-2", "--output", "json"]


def test_older_version_in_standard_support_fails_and_names_the_newest(
    cli: FakeCli, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.on(KEY, CURRENT)
    assert script.main(["--version", "1.36"]) == script.EXIT_NOT_NEWEST
    output = capsys.readouterr().out
    assert "FAIL: 1.37 is available" in output
    assert "2027-08-02 UTC" in output  # the date of the configured version is still reported


def test_version_out_of_standard_support_fails(cli: FakeCli, capsys: pytest.CaptureFixture[str]) -> None:
    cli.on(KEY, CURRENT)
    assert script.main(["--version", "1.33"]) == script.EXIT_NOT_NEWEST
    assert "no longer in standard support" in capsys.readouterr().out


def test_version_the_service_does_not_offer_fails(cli: FakeCli, capsys: pytest.CaptureFixture[str]) -> None:
    cli.on(KEY, CURRENT)
    assert script.main(["--version", "1.99"]) == script.EXIT_NOT_NEWEST
    assert "not offered" in capsys.readouterr().out


def test_versions_compare_as_numbers_not_as_text(cli: FakeCli) -> None:
    cli.on(
        KEY,
        offered(
            ("1.9", "STANDARD_SUPPORT", "2030-01-01T00:00:00+00:00"),
            ("1.10", "STANDARD_SUPPORT", "2030-06-01T00:00:00+00:00"),
        ),
    )
    assert script.main(["--version", "1.10"]) == script.EXIT_OK
    assert script.main(["--version", "1.9"]) == script.EXIT_NOT_NEWEST


def test_end_date_only_prints_just_the_date(cli: FakeCli, capsys: pytest.CaptureFixture[str]) -> None:
    cli.on(KEY, CURRENT)
    assert script.main(["--version", "1.37", "--end-date-only"]) == script.EXIT_OK
    assert capsys.readouterr().out == "2027-12-01\n"


def test_epoch_timestamps_are_accepted(cli: FakeCli, capsys: pytest.CaptureFixture[str]) -> None:
    payload = {
        "clusterVersions": [
            {"clusterVersion": "1.37", "status": "STANDARD_SUPPORT", "endOfStandardSupportDate": 1827619200}
        ]
    }
    cli.on(KEY, payload)
    assert script.main(["--version", "1.37", "--end-date-only"]) == script.EXIT_OK
    assert capsys.readouterr().out.strip() == "2027-12-01"


def test_env_reads_the_parameter_file(cli: FakeCli, capsys: pytest.CaptureFixture[str]) -> None:
    cli.on(KEY, CURRENT)
    # The repository parameter files must hold the newest version at the time of the check.
    assert script.main(["--env", "dev"]) == script.EXIT_OK
    assert script.configured_version("dev") == script.configured_version("prod")
    assert "Configured version 1.37" in capsys.readouterr().out


def test_no_standard_support_version_is_an_error(cli: FakeCli, capsys: pytest.CaptureFixture[str]) -> None:
    cli.on(KEY, offered(("1.33", "EXTENDED_SUPPORT", "2026-07-28T19:00:00-05:00")))
    assert script.main(["--version", "1.33"]) == script.EXIT_ERROR
    assert "no Kubernetes version in standard support" in capsys.readouterr().err


@pytest.mark.parametrize(
    "reply",
    [Reply("", returncode=255, stderr="AccessDenied"), Reply("not json"), Reply(json.dumps({"other": 1}))],
)
def test_service_failures_are_errors(cli: FakeCli, reply: Reply, capsys: pytest.CaptureFixture[str]) -> None:
    cli.on(KEY, reply)
    assert script.main(["--version", "1.37"]) == script.EXIT_ERROR
    assert capsys.readouterr().err.startswith("ERROR:")


def test_a_malformed_version_is_an_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert script.main(["--version", "latest"]) == script.EXIT_ERROR
    assert "not a Kubernetes version" in capsys.readouterr().err

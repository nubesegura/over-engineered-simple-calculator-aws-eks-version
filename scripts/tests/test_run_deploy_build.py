"""Tests of scripts/run_deploy_build.py: the source archive and the CodeBuild run (faked command lines)."""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest
import run_deploy_build as rdb
from cfn_deploy import DeployError
from conftest import FakeCli, Reply

SHA = "[0-9a-f]{64}"


@pytest.fixture
def archive_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(rdb, "OUTPUT_DIR", tmp_path / "out")
    return tmp_path


def test_archive_holds_scripts_and_addons_without_tests_or_caches(archive_dir: Path) -> None:
    path, digest = rdb.build_archive()
    names = zipfile.ZipFile(path).namelist()
    assert "scripts/helm_addons.sh" in names and "scripts/buildspec.yml" in names
    assert any(n.startswith("chart-addons/") for n in names)
    assert not any("/tests/" in n or "__pycache__" in n or n.endswith(".pyc") for n in names)
    assert names == sorted(names)
    assert digest == hashlib.sha256(path.read_bytes()).hexdigest()


def test_archive_is_deterministic(archive_dir: Path) -> None:
    first = rdb.build_archive(target=archive_dir / "a.zip")[1]
    second = rdb.build_archive(target=archive_dir / "b.zip")[1]
    assert first == second


def test_archive_needs_the_addons_script(tmp_path: Path) -> None:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "x.sh").write_text("true\n", encoding="utf-8")
    with pytest.raises(DeployError):
        rdb.build_archive(tmp_path, tmp_path / "o.zip")


def fake_world(cli: FakeCli, status: str) -> None:
    cli.on(
        "cloudformation describe-stacks",
        lambda c: {
            "Stacks": [
                {
                    "StackStatus": "CREATE_COMPLETE",
                    "Outputs": [
                        {
                            "OutputKey": "SourceBucket",
                            "OutputValue": "aws-sam-cli-managed-default-samclisourcebucket-abc",
                        },
                        {"OutputKey": "ProjectName", "OutputValue": "cbp-useast2-oecalc-eks-deploy-dev"},
                    ],
                }
            ]
        },
    )
    cli.on("s3 cp", Reply())
    cli.on("codebuild start-build", {"build": {"id": "cbp-useast2-oecalc-eks-deploy-dev:1"}})
    cli.on(
        "codebuild batch-get-builds",
        {"builds": [{"buildStatus": status, "logs": {"groupName": "g", "streamName": "s"}}]},
    )
    cli.on("logs get-log-events", {"events": [{"message": "arn:aws:iam::123456789012:role/x\n"}, {"message": "done"}]})


def test_run_uploads_starts_and_reports(
    archive_dir: Path, cli: FakeCli, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    summary = archive_dir / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    fake_world(cli, "SUCCEEDED")
    assert rdb.main(["run", "--env", "dev", "--command", "addons"]) == 0
    upload = cli.called("s3 cp")[0]
    assert upload[-1].startswith(
        "s3://aws-sam-cli-managed-default-samclisourcebucket-abc/over-engineered-simple-calculator-aws-eks-version/dev/"
    )
    start = cli.called("codebuild start-build")[0]
    overrides: list[dict[str, Any]] = json.loads(start[start.index("--environment-variables-override") + 1])
    values = {o["name"]: o["value"] for o in overrides}
    assert values["DEPLOY_COMMAND"] == "addons"
    assert values["SOURCE_SHA256"] in upload[-1]
    assert start[start.index("--source-type-override") + 1] == "S3"
    out = capsys.readouterr().out + summary.read_text(encoding="utf-8")
    assert "123456789012" not in out and "done" in out and "SUCCEEDED" in out


@pytest.mark.parametrize("status", ["FAILED", "FAULT", "STOPPED", "TIMED_OUT"])
def test_run_fails_when_the_build_does_not_succeed(archive_dir: Path, cli: FakeCli, status: str) -> None:
    fake_world(cli, status)
    assert rdb.main(["run", "--env", "dev", "--command", "addons"]) == 1


def test_command_is_restricted_to_the_allow_list() -> None:
    with pytest.raises(SystemExit):
        rdb.main(["run", "--env", "dev", "--command", "rm -rf"])

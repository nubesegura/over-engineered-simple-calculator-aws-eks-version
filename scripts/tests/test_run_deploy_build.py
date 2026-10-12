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


# --------------------------------------------------------------------------- build overrides (command deploy)
DIGEST = "sha256:" + "a" * 64
DEPLOY_ENV = {
    "IMAGE_DIGEST": DIGEST,
    "RUN_ID": "r12345",
    "WEB_ORIGIN": "https://page.example.invalid",
    "COGNITO_USER_POOL_ID": "us-east-2_ExamplePool",
    "COGNITO_APP_CLIENT_ID": "exampleclientid123",
    "INGEST_QUEUE_URL": "https://sqs.us-east-2.amazonaws.com/123456789012/example-ingest",
    "INGEST_BUCKET": "example-ingest-bucket",
    "WAF_ACL_ARN": "",
    "ACCESS_LOG_BUCKET": "",
}
ALL_NAMES = list(DEPLOY_ENV)


def deploy_args() -> list[str]:
    args = ["run", "--env", "dev", "--command", "deploy"]
    for name in ALL_NAMES:
        args += ["--pass", name]
    return args


def test_allow_list_has_no_way_to_override_the_source_or_the_command() -> None:
    assert "SOURCE_SHA256" not in rdb.PASSTHROUGH_ALLOWED and "DEPLOY_COMMAND" not in rdb.PASSTHROUGH_ALLOWED
    with pytest.raises(DeployError, match="not allowed"):
        rdb.build_overrides("deploy", [*ALL_NAMES, "SOURCE_SHA256"], {**DEPLOY_ENV, "SOURCE_SHA256": "x"})
    with pytest.raises(SystemExit):
        rdb.main(["run", "--env", "dev", "--command", "deploy", "--pass", "DEPLOY_COMMAND"])


def test_optional_empty_values_are_left_out() -> None:
    overrides = rdb.build_overrides("deploy", ALL_NAMES, DEPLOY_ENV)
    assert {o["name"] for o in overrides} == set(rdb.PASSTHROUGH_REQUIRED)
    with_waf = rdb.build_overrides("deploy", ALL_NAMES, {**DEPLOY_ENV, "WAF_ACL_ARN": "arn:x"})
    assert "WAF_ACL_ARN" in {o["name"] for o in with_waf}
    with_logs = rdb.build_overrides("deploy", ALL_NAMES, {**DEPLOY_ENV, "ACCESS_LOG_BUCKET": "example-alb-logs"})
    assert "ACCESS_LOG_BUCKET" in {o["name"] for o in with_logs}


def test_storage_values_are_required_for_the_worker() -> None:
    assert {"INGEST_QUEUE_URL", "INGEST_BUCKET"} <= set(rdb.PASSTHROUGH_REQUIRED)


@pytest.mark.parametrize("name", rdb.PASSTHROUGH_REQUIRED)
def test_empty_required_value_fails_without_printing_it(name: str) -> None:
    with pytest.raises(DeployError, match=f"{name} is empty"):
        rdb.build_overrides("deploy", ALL_NAMES, {**DEPLOY_ENV, name: "  "})


def test_deploy_needs_every_required_name_to_be_passed() -> None:
    with pytest.raises(DeployError, match="needs IMAGE_DIGEST"):
        rdb.build_overrides("deploy", [n for n in ALL_NAMES if n != "IMAGE_DIGEST"], DEPLOY_ENV)
    assert rdb.build_overrides("addons", [], {}) == []


@pytest.mark.parametrize(
    ("name", "bad"),
    [
        ("IMAGE_DIGEST", "latest"),
        ("IMAGE_DIGEST", "sha256:abc"),
        ("RUN_ID", "Run_1"),
        ("WEB_ORIGIN", "http://page.example.invalid"),
        ("WEB_ORIGIN", "https://page.example.invalid/path"),
        ("INGEST_QUEUE_URL", "http://sqs.us-east-2.amazonaws.com/123456789012/q"),
        ("INGEST_QUEUE_URL", "https://example.invalid/123456789012/q"),
        ("INGEST_BUCKET", "Bad_Bucket"),
        ("ACCESS_LOG_BUCKET", "bucket/with/slash"),
    ],
)
def test_formats_are_checked_and_the_value_is_not_echoed(name: str, bad: str) -> None:
    with pytest.raises(DeployError) as error:
        rdb.build_overrides("deploy", ALL_NAMES, {**DEPLOY_ENV, name: bad})
    assert bad not in str(error.value)


def test_deploy_run_passes_the_digest_and_never_logs_values(
    archive_dir: Path, cli: FakeCli, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    summary = archive_dir / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    for name, value in DEPLOY_ENV.items():
        monkeypatch.setenv(name, value)
    fake_world(cli, "SUCCEEDED")
    assert rdb.main(deploy_args()) == 0
    start = cli.called("codebuild start-build")[0]
    values = {o["name"]: o["value"] for o in json.loads(start[start.index("--environment-variables-override") + 1])}
    assert values["IMAGE_DIGEST"] == DIGEST and values["DEPLOY_COMMAND"] == "deploy"
    assert values["WEB_ORIGIN"] == DEPLOY_ENV["WEB_ORIGIN"] and "WAF_ACL_ARN" not in values
    out = capsys.readouterr().out + summary.read_text(encoding="utf-8")
    assert DIGEST in out  # the digest is public and goes to the summary
    for private in ("exampleclientid123", "us-east-2_ExamplePool", "page.example.invalid", "123456789012"):
        assert private not in out


def test_empty_required_value_stops_before_anything_is_uploaded(
    archive_dir: Path, cli: FakeCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in {**DEPLOY_ENV, "COGNITO_USER_POOL_ID": ""}.items():
        monkeypatch.setenv(name, value)
    fake_world(cli, "SUCCEEDED")
    assert rdb.main(deploy_args()) == 1
    assert not cli.called("s3 cp") and not cli.called("codebuild start-build")


def test_source_archive_includes_the_chart_and_is_deterministic(archive_dir: Path) -> None:
    first_path, first = rdb.build_archive(target=archive_dir / "a.zip")
    names = zipfile.ZipFile(first_path).namelist()
    assert "chart/Chart.yaml" in names and "scripts/helm_deploy.sh" in names
    assert not any(n.startswith("chart/tests") for n in names)
    assert rdb.build_archive(target=archive_dir / "b.zip")[1] == first

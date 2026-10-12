"""Package the deployment source archive and run the CodeBuild deployment project with it.

Steps of `run`: build a deterministic zip of scripts/ (without tests), chart-addons/ and chart/ (when it
exists), compute its SHA-256, upload it to the SAM artifact bucket under <repo-name>/<env>/ (the bucket
is read from the output of the aws-sam-cli-managed-default stack), start the project of the deploy stack
with sourceLocationOverride and the environment variables SOURCE_SHA256 and DEPLOY_COMMAND, wait for the
build, print the last log lines (account ids masked) and write the outcome to the job summary. A build that
does not succeed makes the command fail.

    python scripts/run_deploy_build.py run --env dev --command addons
    IMAGE_DIGEST=... RUN_ID=... python scripts/run_deploy_build.py run --env dev --command deploy \\
        --pass IMAGE_DIGEST --pass RUN_ID --pass WEB_ORIGIN --pass COGNITO_USER_POOL_ID ...
    python scripts/run_deploy_build.py zip            # only build the archive and print its SHA-256

Standard library plus the AWS CLI (through cfn_deploy.run). Writes to AWS only when run by the pipeline.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import zipfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import cfn_deploy as cfn

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = REPO_ROOT / ".build" / "deploy-source"
SOURCE_DIRS = ("scripts", "chart-addons", "chart")
IGNORED_PARTS = {"__pycache__", "tests", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
IGNORED_SUFFIXES = {".pyc", ".pyo"}
FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
FILE_MODE = 0o644 << 16
COMMANDS = ("addons", "deploy", "uninstall")
BUILD_TIMEOUT_SECONDS = 45 * 60
BUILD_POLL_SECONDS = 15
LOG_LINES = 60
# Environment variables the workflow may pass to the build (--pass NAME reads the value from the environment of
# this process, so it never appears on a command line). Nothing else can be overridden: SOURCE_SHA256 and
# DEPLOY_COMMAND are set here and are not in the list. Values are never printed.
PASSTHROUGH_REQUIRED = (
    "IMAGE_DIGEST",
    "RUN_ID",
    "WEB_ORIGIN",
    "COGNITO_USER_POOL_ID",
    "COGNITO_APP_CLIENT_ID",
)
PASSTHROUGH_OPTIONAL = ("WAF_ACL_ARN", "ACCESS_LOG_BUCKET")
PASSTHROUGH_ALLOWED = (*PASSTHROUGH_REQUIRED, *PASSTHROUGH_OPTIONAL)
PASSTHROUGH_FORMATS = {
    "IMAGE_DIGEST": re.compile(r"sha256:[0-9a-f]{64}"),
    "RUN_ID": re.compile(r"[a-z0-9]([-a-z0-9]{0,30}[a-z0-9])?"),
    "WEB_ORIGIN": re.compile(r"https://[A-Za-z0-9.-]+(:[0-9]+)?"),
}


def source_files(root: Path = REPO_ROOT) -> list[Path]:
    """Files of the archive, sorted by their archive path."""
    found: list[Path] = []
    for name in SOURCE_DIRS:
        base = root / name
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            relative = path.relative_to(root)
            if not path.is_file() or IGNORED_PARTS & set(relative.parts) or path.suffix in IGNORED_SUFFIXES:
                continue
            found.append(path)
    return sorted(found, key=lambda p: p.relative_to(root).as_posix())


def build_archive(root: Path = REPO_ROOT, target: Path | None = None) -> tuple[Path, str]:
    """Write the deterministic zip (sorted entries, fixed timestamps and modes) and return it with its SHA-256."""
    target = target or OUTPUT_DIR / "source.zip"
    target.parent.mkdir(parents=True, exist_ok=True)
    files = source_files(root)
    if not any(p.relative_to(root).as_posix() == "scripts/helm_addons.sh" for p in files):
        raise cfn.DeployError("scripts/helm_addons.sh is missing: nothing to run in the build")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            info = zipfile.ZipInfo(path.relative_to(root).as_posix(), FIXED_TIMESTAMP)
            info.external_attr = FILE_MODE | (0o111 << 16 if path.suffix == ".sh" else 0)
            info.compress_type = zipfile.ZIP_DEFLATED
            data = path.read_bytes()
            archive.writestr(info, data.replace(b"\r\n", b"\n") if path.suffix in {".sh", ".yml", ".yaml"} else data)
    return target, hashlib.sha256(target.read_bytes()).hexdigest()


def artifact_bucket() -> str:
    """Name of the SAM managed artifact bucket (output of the SAM managed stack)."""
    return cfn.sam_artifact_bucket()


def tail_log(build: dict[str, Any]) -> list[str]:
    logs = build.get("logs") or {}
    group, stream = logs.get("groupName"), logs.get("streamName")
    if not group or not stream:
        return ["(the build has no log stream)"]
    page = cfn.aws_json(
        [
            "logs",
            "get-log-events",
            "--log-group-name",
            group,
            "--log-stream-name",
            stream,
            "--limit",
            str(LOG_LINES),
            "--no-start-from-head",
        ],
        check=False,
    )
    messages = [e.get("message", "").rstrip() for e in (page or {}).get("events", [])]
    return [cfn.redact_account_ids(m) for m in messages] or ["(no log lines yet)"]


def wait_build(build_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + BUILD_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        data = cfn.aws_json(["codebuild", "batch-get-builds", "--ids", build_id])
        build: dict[str, Any] = data["builds"][0]
        if build.get("buildStatus") != "IN_PROGRESS":
            return build
        time.sleep(BUILD_POLL_SECONDS)
    raise cfn.DeployError("timed out waiting for the deployment build")


def build_overrides(command: str, names: list[str], environ: Mapping[str, str] | None = None) -> list[dict[str, str]]:
    """StartBuild overrides for the names in the allow-list. Fails without printing any value.

    A required name (command deploy) must be passed and non-empty; an optional name that is empty is left out.
    """
    source = os.environ if environ is None else environ
    unknown = [n for n in names if n not in PASSTHROUGH_ALLOWED]
    if unknown:
        raise cfn.DeployError(f"not allowed as a build override: {', '.join(unknown)}")
    if command == "deploy":
        absent = [n for n in PASSTHROUGH_REQUIRED if n not in names]
        if absent:
            raise cfn.DeployError(f"the deploy command needs {', '.join(absent)}")
    overrides: list[dict[str, str]] = []
    for name in dict.fromkeys(names):
        value = source.get(name, "").strip()
        if not value:
            if name in PASSTHROUGH_REQUIRED:
                raise cfn.DeployError(f"{name} is empty: set it before starting the build")
            continue
        pattern = PASSTHROUGH_FORMATS.get(name)
        if pattern and not pattern.fullmatch(value):
            raise cfn.DeployError(f"{name} does not have the expected format")
        overrides.append({"name": name, "value": value, "type": "PLAINTEXT"})
    return overrides


def command_run(args: argparse.Namespace) -> int:
    config = cfn.load_parameters(args.env)
    repo_name = config["Tags"]["repo-name"]
    extra = build_overrides(args.command, args.passthrough or [])  # before anything is uploaded
    archive, sha256 = build_archive()
    key = f"{repo_name}/{args.env}/source-{sha256}.zip"
    bucket = artifact_bucket()
    cfn.run(["aws", "s3", "cp", "--only-show-errors", str(archive), f"s3://{bucket}/{key}"])
    project = cfn.stack_outputs(cfn.find_entry(args.env, "deploy")["stack"])["ProjectName"]
    overrides = [
        {"name": "SOURCE_SHA256", "value": sha256, "type": "PLAINTEXT"},
        {"name": "DEPLOY_COMMAND", "value": args.command, "type": "PLAINTEXT"},
        *extra,
    ]
    digest = next((o["value"] for o in extra if o["name"] == "IMAGE_DIGEST"), None)
    started = cfn.aws_json(
        [
            "codebuild",
            "start-build",
            "--project-name",
            project,
            "--source-type-override",
            "S3",
            "--source-location-override",
            f"{bucket}/{key}",
            "--environment-variables-override",
            json.dumps(overrides),
        ]
    )
    build_id = started["build"]["id"]
    print(f"Build started: {build_id} (command {args.command}, source sha256 {sha256})")
    build = wait_build(build_id)
    status = str(build.get("buildStatus"))
    lines = tail_log(build)
    print("::group::Last build log lines")
    print("\n".join(lines))
    print("::endgroup::")
    summary = [
        f"### Deployment build ({args.command}) in {args.env}",
        "",
        f"- Project: `{project}`",
        f"- Build id: `{build_id}`",
        f"- Source archive SHA-256: `{sha256}`",
        *([f"- Image digest: `{digest}`"] if digest else []),
        f"- Result: **{status}**",
        "",
        "Last log lines:",
        "```",
        *lines,
        "```",
    ]
    cfn.write_step_summary(summary)
    if status != "SUCCEEDED":
        print(f"::error::Deployment build ended with {status}")
        return 1
    return 0


def command_zip(_args: argparse.Namespace) -> int:
    archive, sha256 = build_archive()
    print(f"{sha256}  {archive}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = parser.add_subparsers(dest="action", required=True)
    run = sub.add_parser("run")
    run.add_argument("--env", required=True, choices=["dev", "prod"])
    run.add_argument("--command", required=True, choices=COMMANDS)
    run.add_argument(
        "--pass",
        dest="passthrough",
        action="append",
        choices=PASSTHROUGH_ALLOWED,
        metavar="NAME",
        help="environment variable to pass to the build (value read from this process; allow-list only)",
    )
    run.set_defaults(handler=command_run)
    sub.add_parser("zip").set_defaults(handler=command_zip)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except cfn.DeployError as exc:
        print(f"::error::{cfn.redact_account_ids(str(exc))}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

"""Tests of scripts/iac_owner_guard.py with faked AWS CLI responses (no AWS access).

check: owner none, terraform or cloudformation, each with the requested tool (six cases), both tools, and
unreadable CloudFormation or Terraform state. detect: none, terraform, cloudformation, both, unreadable.
The repository uses CloudFormation only, so the Terraform side is exercised with a fixture stacks file that
defines the state patterns, and the real stacks.json is checked to be CloudFormation-only.
"""

from __future__ import annotations

import json
from pathlib import Path

import iac_owner_guard as guard
import pytest
from conftest import REPO

ACCOUNT = "111122223333"
STACK_SECURITY = "stk-useast2-oecalc-eks-security-dev"
STATE_WITH_RESOURCES = json.dumps(
    {
        "version": 4,
        "resources": [
            {"mode": "data", "type": "aws_region", "instances": [{}]},
            {
                "mode": "managed",
                "type": "aws_vpc",
                "instances": [{"attributes": {"secret": "never printed"}}],
            },
        ],
    }
)
STATE_EMPTY = json.dumps({"version": 4, "resources": []})


@pytest.fixture
def stacks_file(tmp_path: Path) -> Path:
    entries = [
        {"unit": unit, "stack": f"stk-useast2-oecalc-eks-{unit}-dev"} for unit in ("security", "network", "alarms")
    ]
    data = {
        "region": "us-east-2",
        "repository": "repo",
        "terraformStateBucketPattern": "bckt-{region_code}-tf-state-{env}-{account_id}",
        "terraformStateKeyPattern": "{repository}/{env}/{unit}/terraform.tfstate",
        "environments": {"dev": entries},
    }
    path = tmp_path / "stacks.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class FakeAws:
    """Answers the CLI calls of the guard from a small in-memory description of the account."""

    def __init__(
        self,
        stacks: dict[str, str] | None = None,
        states: dict[str, str] | None = None,
        cfn_error: bool = False,
        s3_error: bool = False,
    ) -> None:
        self.stacks = stacks or {}
        self.states = states or {}
        self.cfn_error = cfn_error
        self.s3_error = s3_error
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str], region: str, profile: str | None) -> guard.CliResult:
        self.calls.append(args)
        if args[:2] == ["sts", "get-caller-identity"]:
            return guard.CliResult(0, ACCOUNT + "\n", "")
        if args[:2] == ["cloudformation", "describe-stacks"]:
            if self.cfn_error:
                return guard.CliResult(254, "", "An error occurred (AccessDenied) when calling DescribeStacks")
            name = args[args.index("--stack-name") + 1]
            if name not in self.stacks:
                return guard.CliResult(
                    254, "", f"An error occurred (ValidationError): Stack with id {name} does not exist"
                )
            return guard.CliResult(0, json.dumps({"Stacks": [{"StackStatus": self.stacks[name]}]}), "")
        if args[:2] == ["s3api", "head-object"]:
            if self.s3_error:
                return guard.CliResult(
                    254, "", "An error occurred (403) when calling the HeadObject operation: Forbidden"
                )
            unit = args[args.index("--key") + 1].split("/")[-2]
            if unit not in self.states:
                return guard.CliResult(
                    254, "", "An error occurred (404) when calling the HeadObject operation: Not Found"
                )
            return guard.CliResult(0, "{}", "")
        if args[:2] == ["s3api", "get-object"]:
            unit = args[args.index("--key") + 1].split("/")[-2]
            if unit == "alarms" and "broken-get" in self.states:
                return guard.CliResult(1, "", "boom")
            Path(args[args.index("--key") + 2]).write_text(self.states[unit], encoding="utf-8")
            return guard.CliResult(0, "{}", "")
        raise AssertionError(f"unexpected AWS CLI call: {args}")


def run_guard(
    monkeypatch: pytest.MonkeyPatch,
    fake: FakeAws,
    stacks_file: Path,
    tool: str,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str]:
    monkeypatch.setattr(guard, "run_aws", fake)
    code = guard.main(["check", "--tool", tool, "--env", "dev", "--stacks-file", str(stacks_file)])
    return code, capsys.readouterr().out


def run_detect(
    monkeypatch: pytest.MonkeyPatch, fake: FakeAws, stacks_file: Path, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str]:
    monkeypatch.setattr(guard, "run_aws", fake)
    code = guard.main(["detect", "--env", "dev", "--stacks-file", str(stacks_file)])
    return code, capsys.readouterr().out


def cfn_owned() -> FakeAws:
    return FakeAws(stacks={STACK_SECURITY: "ROLLBACK_COMPLETE"})


def tf_owned() -> FakeAws:
    return FakeAws(states={"network": STATE_WITH_RESOURCES, "security": STATE_EMPTY})


@pytest.mark.parametrize(
    ("fake", "tool", "code"),
    [
        (FakeAws(states={"security": STATE_EMPTY}), "terraform", guard.EXIT_ALLOWED),
        (FakeAws(), "cloudformation", guard.EXIT_ALLOWED),
        (tf_owned(), "terraform", guard.EXIT_ALLOWED),
        (tf_owned(), "cloudformation", guard.EXIT_OTHER_TOOL),
        (cfn_owned(), "cloudformation", guard.EXIT_ALLOWED),
        (cfn_owned(), "terraform", guard.EXIT_OTHER_TOOL),
        (
            FakeAws(
                stacks={"stk-useast2-oecalc-eks-network-dev": "UPDATE_IN_PROGRESS"},
                states={"alarms": STATE_WITH_RESOURCES},
            ),
            "terraform",
            guard.EXIT_BOTH_TOOLS,
        ),
        (FakeAws(cfn_error=True), "cloudformation", guard.EXIT_UNREADABLE),
        (FakeAws(s3_error=True), "terraform", guard.EXIT_UNREADABLE),
    ],
)
def test_check_nine_cases(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stacks_file: Path,
    fake: FakeAws,
    tool: str,
    code: int,
) -> None:
    result, out = run_guard(monkeypatch, fake, stacks_file, tool, capsys)
    assert result == code
    assert "never printed" not in out


def test_check_reports_the_owner_and_never_the_state_content(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], stacks_file: Path
) -> None:
    _, out = run_guard(monkeypatch, tf_owned(), stacks_file, "cloudformation", capsys)
    assert "network: 1 resources" in out
    assert "deployed with terraform" in out
    assert "Destroy it with that tool" in out
    _, out = run_guard(monkeypatch, cfn_owned(), stacks_file, "terraform", capsys)
    assert f"{STACK_SECURITY} (ROLLBACK_COMPLETE)" in out
    assert "deployed with cloudformation" in out


def test_check_unreadable_says_failing_closed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], stacks_file: Path
) -> None:
    _, out = run_guard(monkeypatch, FakeAws(cfn_error=True), stacks_file, "cloudformation", capsys)
    assert "Failing closed" in out


@pytest.mark.parametrize(
    ("fake", "expected"),
    [
        (FakeAws(states={"security": STATE_EMPTY}), (0, "owner=none\n")),
        (tf_owned(), (0, "owner=terraform\n")),
        (cfn_owned(), (0, "owner=cloudformation\n")),
        (
            FakeAws(
                stacks={"stk-useast2-oecalc-eks-alarms-dev": "DELETE_FAILED"},
                states={"alarms": STATE_WITH_RESOURCES},
            ),
            (guard.EXIT_BOTH_TOOLS, "owner=both\n"),
        ),
        (FakeAws(s3_error=True), (guard.EXIT_UNREADABLE, "")),
        (FakeAws(cfn_error=True), (guard.EXIT_UNREADABLE, "")),
    ],
)
def test_detect_five_outputs(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    stacks_file: Path,
    fake: FakeAws,
    expected: tuple[int, str],
) -> None:
    assert run_detect(monkeypatch, fake, stacks_file, capsys) == expected


def test_a_delete_complete_stack_is_not_an_owner(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], stacks_file: Path
) -> None:
    fake = FakeAws(stacks={STACK_SECURITY: "DELETE_COMPLETE"})
    assert run_detect(monkeypatch, fake, stacks_file, capsys) == (0, "owner=none\n")


def test_real_stacks_file_has_no_terraform_side_so_the_owner_is_cloudformation_or_none(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    real = REPO / "templates" / "stacks.json"
    fake = FakeAws(states={"security": STATE_WITH_RESOURCES})
    assert run_detect(monkeypatch, fake, real, capsys) == (0, "owner=none\n")
    assert not [c for c in fake.calls if c[0] == "s3api"]
    cfn = FakeAws(stacks={STACK_SECURITY: "CREATE_COMPLETE"})
    assert run_detect(monkeypatch, cfn, real, capsys) == (0, "owner=cloudformation\n")


def test_malformed_state_fails_closed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], stacks_file: Path
) -> None:
    code, _ = run_guard(monkeypatch, FakeAws(states={"network": "not json"}), stacks_file, "terraform", capsys)
    assert code == guard.EXIT_UNREADABLE


def test_state_download_failure_fails_closed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], stacks_file: Path
) -> None:
    fake = FakeAws(states={"alarms": STATE_EMPTY, "broken-get": ""})
    code, _ = run_guard(monkeypatch, fake, stacks_file, "terraform", capsys)
    assert code == guard.EXIT_UNREADABLE


def test_malformed_stack_response_fails_closed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], stacks_file: Path
) -> None:
    fake = FakeAws(stacks={"stk-useast2-oecalc-eks-network-dev": "x"})
    original = fake.__call__

    def broken(args: list[str], region: str, profile: str | None) -> guard.CliResult:
        if args[:2] == ["cloudformation", "describe-stacks"]:
            return guard.CliResult(0, "<html>", "")
        return original(args, region, profile)

    monkeypatch.setattr(guard, "run_aws", broken)
    assert (
        guard.main(["check", "--tool", "terraform", "--env", "dev", "--stacks-file", str(stacks_file)])
        == guard.EXIT_UNREADABLE
    )


def test_stack_without_a_status_fails_closed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], stacks_file: Path
) -> None:
    monkeypatch.setattr(guard, "run_aws", lambda *_a: guard.CliResult(0, json.dumps({"Stacks": [{}]}), ""))
    assert (
        guard.main(["check", "--tool", "terraform", "--env", "dev", "--stacks-file", str(stacks_file)])
        == guard.EXIT_UNREADABLE
    )


def test_unreadable_account_fails_closed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], stacks_file: Path
) -> None:
    def no_account(args: list[str], _region: str, _profile: str | None) -> guard.CliResult:
        if args[0] == "sts":
            return guard.CliResult(1, "", "denied")
        return guard.CliResult(254, "", "Stack does not exist")

    monkeypatch.setattr(guard, "run_aws", no_account)
    assert guard.main(["detect", "--env", "dev", "--stacks-file", str(stacks_file)]) == guard.EXIT_UNREADABLE


def test_missing_stacks_file_fails_closed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = guard.main(["detect", "--env", "dev", "--stacks-file", str(tmp_path / "missing.json")])
    assert code == guard.EXIT_UNREADABLE


def test_invalid_arguments_fail_closed() -> None:
    with pytest.raises(SystemExit) as raised:
        guard.main(["check", "--tool", "pulumi", "--env", "dev"])
    assert raised.value.code == guard.EXIT_UNREADABLE


def test_prod_message_mentions_no_switch() -> None:
    code, message = guard.decide("cloudformation", "prod", False, True)
    assert code == guard.EXIT_OTHER_TOOL
    assert "not supported" in message


def test_run_aws_returns_cli_errors_as_results(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*_a: object, **_k: object) -> None:
        raise OSError("no aws")

    monkeypatch.setattr("subprocess.run", missing)
    result = guard.run_aws(["sts", "get-caller-identity"], "us-east-2", "profile")
    assert result.returncode == 255
    assert "no aws" in result.stderr

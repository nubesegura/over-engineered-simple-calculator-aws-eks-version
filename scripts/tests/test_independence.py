"""Independence from the ecs and sls versions (requirements 12.1 and 12.2, task 7.4).

Offline: no template imports a value from, or names, a stack of the ecs or sls versions, and no workflow or
script refers to their repositories. At acceptance time (after a deployment) run

    python scripts/tests/test_independence.py --env dev [--profile P] [--region us-east-2]

to check in the account that no stack of this project imports an export of another project and that no other
project imports an export of this one. The check only calls `list-exports` and `list-imports`.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import FakeCli, Reply

REPO = Path(__file__).resolve().parents[2]
# "ecs" or "sls" as a word of a name (stk-useast2-oecalc-ecs-network-dev), not inside "secrets" or "AWS::ECS::"
FOREIGN_NAME = re.compile(r"(?i)(?<![a-z0-9])(ecs|sls)(?![a-z0-9])")
FOREIGN_REPOSITORY = re.compile(
    r"(?i)aws-(ecs|sls)-version|\b(ecs|sls)[ -](version|repositor(y|ies))\b|\.\./[^\s\"']*\b(ecs|sls)\b"
)
NOT_IMPORTED = "is not imported by any stack"


class Loader(yaml.SafeLoader):
    """CloudFormation YAML: short intrinsic functions become {"Fn::X": value} like in JSON."""


def _intrinsic(loader: yaml.SafeLoader, suffix: str, node: yaml.Node) -> Any:
    if isinstance(node, yaml.ScalarNode):
        value: Any = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    elif isinstance(node, yaml.MappingNode):
        value = loader.construct_mapping(node, deep=True)
    else:
        raise TypeError(f"unsupported YAML node for !{suffix}")
    return {"Ref" if suffix == "Ref" else f"Fn::{suffix}": value}


Loader.add_multi_constructor("!", _intrinsic)


def strings(node: Any) -> list[str]:
    """Every string of a loaded template (keys and values), without the AWS resource type names."""
    if isinstance(node, str):
        return [] if node.startswith("AWS::") else [node]
    if isinstance(node, dict):
        return [s for k, v in node.items() for s in [*strings(k), *strings(v)]]
    if isinstance(node, list):
        return [s for item in node for s in strings(item)]
    return []


def foreign_names_in_templates(folder: Path) -> list[str]:
    """`file: text` for each string of a template that names an ecs or sls stack, export or resource."""
    found: list[str] = []
    for path in sorted(folder.glob("*.y*ml")):
        template = yaml.load(path.read_text(encoding="utf-8"), Loader=Loader)  # noqa: S506 - own CloudFormation loader
        found += [f"{path.name}: {text}" for text in strings(template) if FOREIGN_NAME.search(text)]
    return found


def foreign_repository_references(root: Path) -> list[str]:
    """`file: text` for each line of a workflow or script (not the tests) that refers to another repository."""
    files = sorted((root / ".github" / "workflows").glob("*.y*ml"))
    for pattern in ("*.py", "*.sh"):
        files += sorted(p for p in (root / "scripts").rglob(pattern) if "tests" not in p.relative_to(root).parts)
    found: list[str] = []
    for path in files:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if FOREIGN_REPOSITORY.search(line):
                found.append(f"{path.relative_to(root).as_posix()}:{number}: {line.strip()}")
    return found


def project_stacks(root: Path, env: str) -> set[str]:
    data = json.loads((root / "templates" / "stacks.json").read_text(encoding="utf-8"))
    return {entry["stack"] for entry in data["environments"][env]}


def aws(args: list[str], region: str, profile: str | None) -> dict[str, Any]:
    command = ["aws", *args, "--region", region, "--output", "json"] + (["--profile", profile] if profile else [])
    done = subprocess.run(command, capture_output=True, text=True, check=False)  # noqa: S603
    if done.returncode != 0:
        if NOT_IMPORTED in done.stderr:
            return {"Imports": []}
        raise RuntimeError(
            f"aws {' '.join(args[:2])} failed: {re.sub(r'[0-9]{12}', '************', done.stderr)[:200]}"
        )
    data: dict[str, Any] = json.loads(done.stdout) if done.stdout.strip() else {}
    return data


def cross_stack_imports(stacks: set[str], region: str = "us-east-2", profile: str | None = None) -> list[str]:
    """Imports that cross the border of the project: a stack of the project importing an export of another
    stack, or another stack importing an export of the project. Empty when the project is independent."""
    problems: list[str] = []
    exports = aws(["cloudformation", "list-exports"], region, profile).get("Exports", [])
    for export in exports:
        exporter = export["ExportingStackId"].split("/")[1]  # arn:...:stack/<name>/<uuid>
        importers = aws(["cloudformation", "list-imports", "--export-name", export["Name"]], region, profile)
        for importer in importers.get("Imports", []):
            if (exporter in stacks) != (importer in stacks):
                problems.append(f"{importer} imports {export['Name']} exported by {exporter}")
    return sorted(problems)


# -- tests -------------------------------------------------------------------------------------------------


def test_templates_do_not_import_or_name_ecs_or_sls_stacks() -> None:
    assert foreign_names_in_templates(REPO / "templates") == []


def test_workflows_and_scripts_do_not_refer_to_the_ecs_or_sls_repositories() -> None:
    assert foreign_repository_references(REPO) == []


def test_templates_check_catches_a_seeded_import(tmp_path: Path) -> None:
    (tmp_path / "ok.yaml").write_text(
        'Resources:\n  Q:\n    Type: AWS::SQS::Queue\n    Properties:\n      QueueName: !Sub "sqs-${EnvType}"\n'
        '      KmsMasterKeyId: !ImportValue "stk-useast2-oecalc-eks-security-dev:DataKeyArn"\n',
        encoding="utf-8",
    )
    assert foreign_names_in_templates(tmp_path) == []
    (tmp_path / "bad.yaml").write_text(
        "Resources:\n  Q:\n    Type: AWS::SQS::Queue\n    Properties:\n"
        '      KmsMasterKeyId: !ImportValue "stk-useast2-oecalc-ecs-security-dev:DataKeyArn"\n'
        '      QueueName: !Sub "${AWS::StackName}-sls-queue"\n',
        encoding="utf-8",
    )
    found = foreign_names_in_templates(tmp_path)
    assert len(found) == 2 and all(line.startswith("bad.yaml") for line in found)


def test_repository_check_catches_seeded_references_and_skips_tests(tmp_path: Path) -> None:
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / "scripts" / "tests").mkdir(parents=True)
    (tmp_path / "scripts" / "fine.sh").write_text("echo secrets and AWS::ECS\n", encoding="utf-8")
    (tmp_path / "scripts" / "tests" / "fixture.py").write_text(
        "X = 'over-engineered-simple-calculator-aws-ecs-version'\n", encoding="utf-8"
    )
    assert foreign_repository_references(tmp_path) == []
    (tmp_path / ".github" / "workflows" / "w.yml").write_text(
        "run: git clone https://example.invalid/over-engineered-simple-calculator-aws-ecs-version\n", encoding="utf-8"
    )
    (tmp_path / "scripts" / "copy.sh").write_text(
        "cp ../over-engineered-simple-calculator-aws-sls-version/x .\n", encoding="utf-8"
    )
    (tmp_path / "scripts" / "note.py").write_text("# same approach as the ecs repository\n", encoding="utf-8")
    found = foreign_repository_references(tmp_path)
    assert sorted(line.split(":")[0] for line in found) == [
        ".github/workflows/w.yml",
        "scripts/copy.sh",
        "scripts/note.py",
    ]


def test_project_stacks_come_from_the_stack_list() -> None:
    assert "stk-useast2-oecalc-eks-network-dev" in project_stacks(REPO, "dev")
    assert project_stacks(REPO, "dev").isdisjoint(project_stacks(REPO, "prod"))


def export(name: str, stack: str) -> dict[str, str]:
    return {"Name": name, "ExportingStackId": f"arn:aws:cloudformation:us-east-2:123456789012:stack/{stack}/uuid"}


OURS = {"stk-a-dev", "stk-b-dev"}


def wire(cli: FakeCli, exports: list[dict[str, str]], importers: dict[str, list[str]]) -> None:
    def imports(command: list[str]) -> Reply:
        name = command[command.index("--export-name") + 1]
        if not importers.get(name):
            return Reply(returncode=254, stderr=f"Export '{name}' {NOT_IMPORTED}.")
        return Reply({"Imports": importers[name]})

    cli.on("cloudformation list-exports", {"Exports": exports})
    cli.on("cloudformation list-imports", imports)


def test_acceptance_check_accepts_imports_inside_the_project(cli: FakeCli) -> None:
    wire(cli, [export("stk-a-dev:Key", "stk-a-dev"), export("other:X", "other")], {"stk-a-dev:Key": ["stk-b-dev"]})
    assert cross_stack_imports(OURS) == []  # an export nobody imports is not an error either


def test_acceptance_check_finds_both_directions(cli: FakeCli) -> None:
    wire(
        cli,
        [export("stk-a-dev:Key", "stk-a-dev"), export("stk-ecs-net:Vpc", "stk-ecs-net")],
        {"stk-a-dev:Key": ["stk-ecs-app"], "stk-ecs-net:Vpc": ["stk-b-dev", "stk-ecs-app"]},
    )
    assert cross_stack_imports(OURS, profile="p") == [
        "stk-b-dev imports stk-ecs-net:Vpc exported by stk-ecs-net",
        "stk-ecs-app imports stk-a-dev:Key exported by stk-a-dev",
    ]
    assert "--profile" in cli.calls[0]


def test_acceptance_check_stops_on_a_failed_read(cli: FakeCli) -> None:
    cli.on("cloudformation list-exports", Reply(returncode=255, stderr="AccessDenied for 123456789012"))
    with pytest.raises(RuntimeError, match=r"AccessDenied for \*+"):
        cross_stack_imports(OURS)


def test_command_line_exit_codes(cli: FakeCli, capsys: pytest.CaptureFixture[str]) -> None:
    stack = "stk-useast2-oecalc-eks-network-dev"
    wire(cli, [export("x:Y", stack)], {"x:Y": ["stk-ecs-app"]})
    assert main(["--env", "dev"]) == 1
    assert "stk-ecs-app imports x:Y" in capsys.readouterr().out
    wire(cli, [], {})
    assert main(["--env", "dev"]) == 0
    cli.on("cloudformation list-exports", Reply(returncode=255, stderr="boom"))
    assert main(["--env", "dev"]) == 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check at acceptance time that the project has no cross-stack import")
    parser.add_argument("--env", required=True, choices=["dev", "prod"])
    parser.add_argument("--region", default="us-east-2")
    parser.add_argument("--profile", default=None)
    args = parser.parse_args(argv)
    try:
        problems = cross_stack_imports(project_stacks(REPO, args.env), args.region, args.profile)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    for problem in problems:
        print(f"CROSS-PROJECT IMPORT: {problem}")
    print(f"{len(problems)} cross-project imports")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

"""Static checks of .github/dependabot.yml: it must keep pointing at real manifests and stay quiet (QG-009)."""

from __future__ import annotations

from typing import Any

import pytest
import yaml
from conftest import REPO

CONFIG: dict[str, Any] = yaml.safe_load((REPO / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
UPDATES: list[dict[str, Any]] = CONFIG["updates"]
MANIFESTS = {"pip": ("requirements.txt", "requirements-dev.txt", "pyproject.toml"), "docker": ("Dockerfile",)}


def directories(update: dict[str, Any]) -> list[str]:
    return list(update.get("directories") or [update["directory"]])


def test_one_weekly_utc_entry_per_ecosystem_with_the_pr_limit_and_no_assignees() -> None:
    assert sorted(u["package-ecosystem"] for u in UPDATES) == ["docker", "github-actions", "pip"]
    for update in UPDATES:
        schedule = update["schedule"]
        assert schedule["interval"] == "weekly" and schedule["day"] == "monday" and schedule["timezone"] == "Etc/UTC"
        assert update["open-pull-requests-limit"] == 5
        assert update["commit-message"]["prefix"].startswith("deps(")
        assert "assignees" not in update and "reviewers" not in update
        assert update["groups"], "updates must be grouped so one week produces few pull requests"


@pytest.mark.parametrize("update", UPDATES, ids=lambda u: u["package-ecosystem"])
def test_every_directory_exists_and_holds_a_manifest_of_its_ecosystem(update: dict[str, Any]) -> None:
    ecosystem = update["package-ecosystem"]
    for directory in directories(update):
        assert directory.startswith("/")
        folder = REPO / directory.lstrip("/")
        assert folder.is_dir(), directory
        if ecosystem == "github-actions":
            assert any((REPO / ".github" / "workflows").glob("*.yml"))
        else:
            assert any((folder / name).is_file() for name in MANIFESTS[ecosystem]), directory


def test_every_requirements_file_of_the_repository_is_covered() -> None:
    covered = {REPO / d.lstrip("/") for u in UPDATES if u["package-ecosystem"] == "pip" for d in directories(u)}
    skipped = {".venv", "node_modules", "specs"}
    found = {path.parent for path in REPO.glob("**/requirements*.txt") if not skipped & set(path.parts)}
    assert found <= covered, sorted(str(p) for p in found - covered)

"""The versioned pipeline page must match its generators: a workflow change without a regenerated page fails here."""

from __future__ import annotations

import subprocess
import sys

from conftest import REPO

BUILD = REPO / "scripts" / "diagrams" / "workflows" / "build.py"


def test_the_workflows_page_is_up_to_date_with_its_generators() -> None:
    result = subprocess.run(  # noqa: S603 - fixed interpreter and script of this repository
        [sys.executable, str(BUILD), "--check"], cwd=REPO, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, (
        "docs/workflows-flow-animation.html is stale: run `python scripts/diagrams/workflows/build.py` "
        f"and commit the page.\n{result.stdout}{result.stderr}"
    )

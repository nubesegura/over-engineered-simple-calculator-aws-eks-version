"""Shared fixtures: the scripts folder on sys.path and a scripted fake of the AWS and SAM command lines."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


class Reply:
    """What a fake command prints."""

    def __init__(self, stdout: Any = "", returncode: int = 0, stderr: str = "") -> None:
        self.stdout = stdout if isinstance(stdout, str) else json.dumps(stdout)
        self.returncode = returncode
        self.stderr = stderr


Handler = Callable[[list[str]], Reply | Any]


class FakeCli:
    """Replaces subprocess.run. Commands are routed by their first two words ("cloudformation describe-stacks").

    A handler gets the full argument list and returns a Reply, or any JSON-able value (exit code 0). A list of
    replies is consumed in order and its last element repeats. An unknown command fails the test.
    """

    def __init__(self) -> None:
        self.routes: dict[str, Handler | list[Any]] = {}
        self.calls: list[list[str]] = []

    def on(self, key: str, handler: Handler | list[Any] | Reply | Any) -> None:
        self.routes[key] = handler if callable(handler) or isinstance(handler, list) else [handler]

    def called(self, key: str) -> list[list[str]]:
        words = key.split()
        return [c for c in self.calls if c[: len(words)] == words or c[1 : 1 + len(words)] == words]

    def __call__(self, command: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        words = command[1:3] if command[0] in ("aws", "sam") else command[:2]
        key = " ".join(words)
        if command[0] == "sam":
            key = f"sam {command[1]}"
        if key not in self.routes:
            raise AssertionError(f"unexpected command: {' '.join(command)}")
        route = self.routes[key]
        if callable(route):
            reply: Any = route(command)
        else:
            reply = route.pop(0) if len(route) > 1 else route[0]
        if not isinstance(reply, Reply):
            reply = Reply(reply)
        return subprocess.CompletedProcess(command, reply.returncode, reply.stdout, reply.stderr)


@pytest.fixture
def cli(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeCli]:
    fake = FakeCli()
    monkeypatch.setattr(subprocess, "run", fake)
    monkeypatch.setattr("time.sleep", lambda _s: None)
    yield fake

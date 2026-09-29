from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

from cargorewind.runner import CommandResult

FIXTURES = Path(__file__).parent / "fixtures"


class GitRepo:
    """A throwaway git repository driven with the real git binary."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.mkdir(parents=True, exist_ok=True)
        self.git("init", "--quiet", "--initial-branch=main")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        self.git("config", "commit.gpgsign", "false")

    def git(self, *args: str, env: dict[str, str] | None = None) -> str:
        return subprocess.run(
            ["git", "-C", str(self.path), *args],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        ).stdout

    def write(self, files: dict[str, str | None]) -> None:
        for name, content in files.items():
            target = self.path / name
            if content is None:
                target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)

    def commit(self, message: str, files: dict[str, str | None], date: str) -> str:
        self.write(files)
        self.git("add", "-A")
        env = {**os.environ, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
        self.git("commit", "--quiet", "-m", message, env=env)
        return self.git("rev-parse", "HEAD").strip()


@pytest.fixture
def make_repo(tmp_path: Path) -> Callable[[str], GitRepo]:
    def factory(name: str = "origin") -> GitRepo:
        return GitRepo(tmp_path / name)

    return factory


class FakeRunner:
    """Records every call and answers from scripted results matched by argv prefix."""

    def __init__(self, script: list[tuple[tuple[str, ...], CommandResult]] | None = None) -> None:
        self.script = list(script or [])
        self.calls: list[dict[str, object]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        stdin: bytes | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        args = tuple(argv)
        self.calls.append({"argv": args, "cwd": cwd, "stdin": stdin, "timeout": timeout})
        for prefix, result in self.script:
            if args[: len(prefix)] == prefix:
                return CommandResult(
                    args, result.returncode, result.stdout, result.stderr, result.timed_out
                )
        return CommandResult(args, 0, "", "")


@pytest.fixture
def fake_runner() -> FakeRunner:
    return FakeRunner()

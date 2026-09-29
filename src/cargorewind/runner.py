"""Process boundary: every git and docker call goes through a ``Runner``.

Unit tests substitute a fake runner, so nothing in the test suite needs Docker.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

TIMEOUT_EXIT_CODE = 124


@dataclass(frozen=True)
class CommandResult:
    """Outcome of one external command."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


class CommandError(RuntimeError):
    """An external command exited non-zero where success was required."""

    def __init__(self, result: CommandResult) -> None:
        tail = (result.stderr or result.stdout).strip().splitlines()[-8:]
        detail = "\n".join(tail)
        what = "timed out" if result.timed_out else f"exited {result.returncode}"
        super().__init__(f"command {what}: {' '.join(result.argv)}\n{detail}".rstrip())
        self.result = result


class Runner(Protocol):
    """Runs an external command and captures its output."""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        stdin: bytes | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> CommandResult: ...


def _decode(data: bytes | str | None) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    return data.decode("utf-8", errors="replace")


class SubprocessRunner:
    """Runner backed by ``subprocess.run``; never raises on a non-zero exit."""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        stdin: bytes | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        full_env = {**os.environ, **env} if env else None
        args = tuple(argv)
        try:
            proc = subprocess.run(
                args,
                cwd=cwd,
                input=stdin,
                capture_output=True,
                env=full_env,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            return CommandResult(
                args, TIMEOUT_EXIT_CODE, _decode(exc.stdout), _decode(exc.stderr), timed_out=True
            )
        return CommandResult(args, proc.returncode, _decode(proc.stdout), _decode(proc.stderr))


def checked(result: CommandResult) -> CommandResult:
    """Return ``result`` unchanged, or raise ``CommandError`` when it failed."""
    if not result.ok:
        raise CommandError(result)
    return result

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from cargorewind.runner import CommandError, CommandResult, SubprocessRunner, checked


def test_subprocess_runner_captures_output_and_exit_code(tmp_path: Path) -> None:
    result = SubprocessRunner().run(
        [
            sys.executable,
            "-c",
            "import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)",
        ],
        cwd=tmp_path,
    )
    assert result.returncode == 3
    assert result.stdout.strip() == "out"
    assert result.stderr.strip() == "err"
    assert not result.ok


def test_subprocess_runner_passes_stdin_and_env() -> None:
    code = "import os, sys; print(sys.stdin.read() + os.environ['CR_TEST'])"
    result = SubprocessRunner().run(
        [sys.executable, "-c", code], stdin=b"in-", env={"CR_TEST": "env"}
    )
    assert result.ok
    assert result.stdout.strip() == "in-env"


def test_subprocess_runner_reports_timeout() -> None:
    result = SubprocessRunner().run(
        [sys.executable, "-c", "import time; print('x', flush=True); time.sleep(5)"], timeout=0.5
    )
    assert result.timed_out
    assert result.returncode == 124
    assert not result.ok


def test_checked_raises_with_command_tail() -> None:
    bad = CommandResult(("git", "status"), 128, "", "fatal: not a git repository\n")
    with pytest.raises(CommandError, match="exited 128: git status") as info:
        checked(bad)
    assert "not a git repository" in str(info.value)
    assert info.value.result is bad


def test_checked_reports_timeouts_and_passes_success_through() -> None:
    good = CommandResult(("true",), 0, "", "")
    assert checked(good) is good
    with pytest.raises(CommandError, match="timed out"):
        checked(CommandResult(("sleep",), 124, "", "", timed_out=True))

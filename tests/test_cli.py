from __future__ import annotations

import pytest
from typer.testing import CliRunner

from cargorewind import __version__, cli

runner = CliRunner()


def test_version_prints_package_version() -> None:
    result = runner.invoke(cli.app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_no_args_shows_help() -> None:
    result = runner.invoke(cli.app, [])
    assert "historical commit" in result.output


def test_check_tools_reports_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda name: None if name == "docker" else "/bin/git")
    assert cli.check_tools() == {"git": "/bin/git", "docker": None}


def test_doctor_succeeds_when_all_tools_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")
    result = runner.invoke(cli.app, ["doctor"])
    assert result.exit_code == 0
    assert "/usr/bin/docker" in result.stdout


def test_doctor_fails_when_a_tool_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda name: None if name == "docker" else "/bin/git")
    result = runner.invoke(cli.app, ["doctor"])
    assert result.exit_code == 1
    assert "docker   MISSING" in result.stdout

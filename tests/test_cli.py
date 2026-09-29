from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from cargorewind import __version__, cli
from cargorewind.backend import ReplayBackend
from cargorewind.libtest import Outcome, compute_flip

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


DEMO = Path(__file__).resolve().parents[1] / "examples" / "strsim"


def _demo_args(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "rewind",
        str(DEMO / "strsim-rs.bundle"),
        "--fix",
        "605c81c9b9",
        "--out",
        str(tmp_path / "out"),
        "--workdir",
        str(tmp_path / "work"),
        *extra,
    ]


def test_rewind_replay_prints_verified_flip(tmp_path: Path) -> None:
    result = runner.invoke(cli.app, _demo_args(tmp_path, "--replay", str(DEMO / "transcript.json")))
    assert result.exit_code == 0, result.output
    assert "FAIL_TO_PASS  2" in result.stdout
    assert "  tests::jaro_winkler_same_one_character" in result.stdout
    assert "verdict       VERIFIED fail-to-pass flip" in result.stdout
    assert (tmp_path / "out" / "task.json").is_file()


def test_rewind_record_writes_a_transcript(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    replayed = ReplayBackend(DEMO / "transcript.json")
    monkeypatch.setattr(cli, "DockerBackend", lambda runner, timeout: replayed)
    record = tmp_path / "rec.json"
    result = runner.invoke(cli.app, _demo_args(tmp_path, "--record", str(record)))
    assert result.exit_code == 0, result.output
    assert json.loads(record.read_text())["runs"]["after"]["exit_code"] == 0


def test_rewind_rejects_conflicting_or_unpinned_options(tmp_path: Path) -> None:
    both = runner.invoke(cli.app, _demo_args(tmp_path, "--record", "a", "--replay", "b"))
    assert both.exit_code == 2
    unpinned = runner.invoke(cli.app, _demo_args(tmp_path, "--image", "rust:1.39.0-slim"))
    assert unpinned.exit_code == 2


def test_rewind_reports_errors_with_exit_code_1(tmp_path: Path) -> None:
    args = _demo_args(tmp_path, "--replay", str(DEMO / "transcript.json"))
    args[3] = "0000000000"
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 1
    assert "unknown commit" in result.output


def test_rewind_exits_2_when_the_flip_is_not_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = SimpleNamespace(
        flip=compute_flip({"a": Outcome.PASSED}, {"a": Outcome.PASSED}, {"a": Outcome.FAILED})
    )
    monkeypatch.setattr(cli, "rewind", lambda *args: report)
    missing = runner.invoke(cli.app, _demo_args(tmp_path, "--replay", "x.json"))
    assert missing.exit_code == 1
    assert "cannot read transcript" in missing.output
    result = runner.invoke(cli.app, _demo_args(tmp_path, "--replay", str(DEMO / "transcript.json")))
    assert result.exit_code == 2
    assert "regressions   1: a" in result.stdout
    assert "NOT VERIFIED" in result.stdout


def test_rewind_registry_options_reach_the_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[bool, Path | None]] = []
    real = cli.make_resolver

    def spy(registry: bool, cache_dir: Path | None = None) -> object:
        seen.append((registry, cache_dir))
        return real(False)

    monkeypatch.setattr(cli, "make_resolver", spy)
    args = _demo_args(
        tmp_path,
        "--replay",
        str(DEMO / "transcript.json"),
        "--registry",
        "--cache-dir",
        str(tmp_path / "cache"),
    )
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert seen == [(True, tmp_path / "cache")]

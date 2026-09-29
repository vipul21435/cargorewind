from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from cargorewind import __version__, cli
from cargorewind.backend import ReplayBackend
from cargorewind.libtest import Outcome, compute_flip
from cargorewind.registry import DigestCache, HttpResponse, ImageResolver, RegistryClient
from tests.test_registry import FakeHttp

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


TOOLCHAIN_FIXTURES = Path(__file__).parent / "fixtures" / "toolchain"


def test_toolchain_command_on_the_demo_bundle(tmp_path: Path) -> None:
    report = tmp_path / "reports" / "toolchain.json"
    args = [
        "toolchain",
        str(DEMO / "strsim-rs.bundle"),
        "605c81c9b9",
        "--workdir",
        str(tmp_path / "work"),
        "--json",
        str(report),
    ]
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[2] == "file      none: no rust-toolchain.toml or rust-toolchain at the root"
    assert "date      1.39.0: newest stable before 2019-12-13 (1.39.0 released 2019-11-07)" in lines
    assert "floor     1.0.0: 1.39.0 meets every floor" in lines
    assert "          offline-table: offline digest table" in lines
    document = json.loads(report.read_text())
    assert document["schema_version"] == 1
    assert document["fix_commit"].startswith("605c81c9b9")
    assert document["toolchain"]["version"] == "1.39.0"
    assert document["image"]["source"] == "offline-table"


def _msrv_repo(make_repo: Any) -> tuple[Any, str]:
    repo = make_repo("msrv")
    files = {
        p.relative_to(TOOLCHAIN_FIXTURES / "msrv-raise").as_posix(): p.read_text()
        for p in (TOOLCHAIN_FIXTURES / "msrv-raise").rglob("*")
        if p.is_file()
    }
    repo.commit("base", {**files, "src/lib.rs": "pub fn f() {}\n"}, "2024-01-10T12:00:00+00:00")
    fix = repo.commit(
        "fix", {"src/lib.rs": "pub fn f() -> u8 { 1 }\n"}, "2024-01-11T12:00:00+00:00"
    )
    return repo, fix


def test_toolchain_command_raises_to_the_msrv_and_needs_a_digest(
    make_repo: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, fix = _msrv_repo(make_repo)
    args = ["toolchain", str(repo.path), fix, "--workdir", str(tmp_path / "work")]
    offline = runner.invoke(cli.app, args)
    assert offline.exit_code == 1
    assert "raise     1.65.0: raised from 1.60.0 to meet 1.65.0" in offline.stdout
    assert "toolchain 1.65.0 (rust-version)" in offline.stdout
    assert "no pinned digest for rust:1.65.0-slim; pass --registry" in offline.output

    digest = "sha256:" + "f" * 64
    head = HttpResponse(
        200,
        {
            "docker-content-digest": digest,
            "content-type": "application/vnd.oci.image.index.v1+json",
        },
    )
    http = FakeHttp({("HEAD", "v2/library/rust/manifests/1.65.0-slim"): head})
    seen: list[tuple[bool, Path | None]] = []

    def fake_resolver(registry: bool, cache_dir: Path | None = None) -> ImageResolver:
        seen.append((registry, cache_dir))
        return ImageResolver(registry=RegistryClient(http, auth=None), cache=DigestCache(tmp_path))

    monkeypatch.setattr(cli, "make_resolver", fake_resolver)
    online = runner.invoke(cli.app, [*args, "--registry", "--cache-dir", str(tmp_path)])
    assert online.exit_code == 0, online.output
    assert f"image     rust:1.65.0-slim@{digest}" in online.stdout
    assert seen == [(True, tmp_path)]


def test_toolchain_command_reports_unknown_commits(make_repo: Any, tmp_path: Path) -> None:
    repo, _ = _msrv_repo(make_repo)
    args = ["toolchain", str(repo.path), "0" * 40, "--workdir", str(tmp_path / "work")]
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 1
    assert "unknown commit" in result.output

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cargorewind import cli
from cargorewind.gitops import Git, GitError, GitTree, open_checkout
from cargorewind.patchsplit import parse_diff, split_diff
from cargorewind.runner import SubprocessRunner
from cargorewind.splitreport import (
    SplitChecks,
    check_split,
    log_checks,
    require,
    resolve_commits,
    split_commit,
)
from tests.conftest import GitRepo

DEMO = Path(__file__).resolve().parents[1] / "examples" / "strsim"
LIB = "pub fn f() -> u8 {\n    1\n}\n\n#[cfg(test)]\nmod tests {\n    fn a() {}\n}\n"
FIXED = LIB.replace("    1\n", "    2\n").replace("fn a() {}", "fn a() {}\n    fn b() {}")
runner = CliRunner()


def _repo(make_repo: Callable[[str], GitRepo]) -> tuple[Git, str, str]:
    repo = make_repo("origin")
    base = repo.commit(
        "base",
        {"Cargo.toml": '[package]\nname = "p"\n', "src/lib.rs": LIB, "README.md": "a\n"},
        "2024-01-10T12:00:00+00:00",
    )
    fix = repo.commit("fix", {"src/lib.rs": FIXED, "README.md": "b\n"}, "2024-01-11T12:00:00+00:00")
    return Git(SubprocessRunner(), repo.path), base, fix


def test_split_commit_writes_patches_and_split_json(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    git, base, fix = _repo(make_repo)
    lines: list[str] = []
    commits = resolve_commits(git, fix[:10], None, lines.append)
    assert commits.base == base
    split, checks = split_commit(commits, "origin", tmp_path, lines.append, verbose=True)
    assert checks.ok
    doc = json.loads((tmp_path / "split.json").read_text())
    assert doc["summary"]["shared_files"] == ["src/lib.rs"]
    assert doc["checks"] == {
        "test_patch_applies_at_base": True,
        "fix_patch_applies_after_test_patch": True,
        "patches_reproduce_fix": True,
    }
    assert [(f["path"], f["role"], f["patch"]) for f in doc["files"]] == [
        ("README.md", "other", "fix"),
        ("src/lib.rs", "source", "both"),
    ]
    assert doc["packages"][0]["targets"] == [{"kind": "lib", "name": "p", "path": "src/lib.rs"}]
    assert doc["cfg_test_regions"][0]["start_line"] == 5
    assert "file      source  both  src/lib.rs [lib]" in lines
    assert "region    src/lib.rs:5-9 module tests, cfg(test)" in lines
    assert (tmp_path / "test.patch").read_text() == split.test_patch
    assert git.matches(base, ["src/lib.rs", "README.md"])  # checkout restored to base


def test_check_split_reports_a_test_patch_that_does_not_apply(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    git, base, fix = _repo(make_repo)
    split = split_diff(parse_diff(git.diff(base, fix)), GitTree(git, base), GitTree(git, fix))
    (tmp_path / "test.patch").write_text(split.test_patch.replace("fn a() {}", "fn zzz() {}"))
    (tmp_path / "fix.patch").write_text(split.fix_patch)
    checks = check_split(git, base, fix, split, tmp_path)
    assert (checks.test_patch_applies, checks.fix_patch_applies) == (False, None)
    assert not checks.ok
    assert checks.error().startswith("test.patch does not apply at base\nerror:")
    with pytest.raises(GitError, match="does not apply at base"):
        require(checks)


def test_check_split_reports_a_fix_patch_that_does_not_apply(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    git, base, fix = _repo(make_repo)
    split = split_diff(parse_diff(git.diff(base, fix)), GitTree(git, base), GitTree(git, fix))
    (tmp_path / "test.patch").write_text(split.test_patch)
    (tmp_path / "fix.patch").write_text(split.fix_patch.replace("-a", "-nope"))
    checks = check_split(git, base, fix, split, tmp_path)
    assert (checks.test_patch_applies, checks.fix_patch_applies) == (True, False)
    assert checks.error().startswith("fix.patch does not apply after test.patch")


def test_check_messages_for_empty_patches_and_mismatches() -> None:
    lines: list[str] = []
    checks = SplitChecks(None, True, False)
    log_checks(checks, 3, lines.append)
    assert lines == [
        "check     test.patch applies at base (git apply --check): skipped (empty patch)",
        "check     fix.patch applies on top (git apply --check): ok",
        "check     both patches reproduce the fix (3 paths): FAILED",
    ]
    assert checks.error() == "test.patch + fix.patch do not reproduce the fix commit"
    require(SplitChecks(None, None, True))


def test_split_command_on_the_demo_bundle(tmp_path: Path) -> None:
    out = tmp_path / "split"
    args = ["split", str(DEMO / "strsim-rs.bundle"), "--fix", "605c81c9b9", "--out", str(out)]
    result = runner.invoke(cli.app, [*args, "--workdir", str(tmp_path / "work")])
    assert result.exit_code == 0, result.output
    assert "file      source  both  src/lib.rs [lib]" in result.stdout
    assert "shared    src/lib.rs @@ -491,6 +493,11 @@: 5 test line(s), 0 fix line(s)" in (
        result.stdout
    )
    assert "check     both patches reproduce the fix (2 paths): ok" in result.stdout
    doc = json.loads((out / "split.json").read_text())
    assert doc["summary"]["test_patch_files"] == ["src/lib.rs"]
    assert [h["test_patch_hunk"] for h in doc["shared_hunks"]] == [
        None,
        "@@ -491,6 +491,11 @@",
        "@@ -561,6 +566,11 @@",
    ]


def test_split_command_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base_args = ["split", str(DEMO / "strsim-rs.bundle"), "--workdir", str(tmp_path / "w")]
    unknown = runner.invoke(cli.app, [*base_args, "--fix", "0000000000"])
    assert unknown.exit_code == 1
    assert "unknown commit" in unknown.output

    monkeypatch.setattr(
        cli, "split_commit", lambda *args, **kwargs: (None, SplitChecks(False, None, False))
    )
    failed = runner.invoke(cli.app, [*base_args, "--fix", "605c81c9b9", "--out", str(tmp_path)])
    assert failed.exit_code == 1
    assert "test.patch does not apply at base" in failed.output


def test_parallel_splits_share_one_checkout(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    # Review finding: parallel runs on one work checkout broke each other's checks.
    repo = make_repo("origin")
    repo.commit(
        "root",
        {"Cargo.toml": '[package]\nname = "p"\n', "src/lib.rs": LIB},
        "2024-01-09T12:00:00+00:00",
    )
    fixes: list[str] = []
    for n in range(3):
        text = LIB.replace("    1\n", f"    {n + 2}\n").replace("fn a() {}", f"fn a{n}() {{}}")
        fixes.append(repo.commit(f"fix {n}", {"src/lib.rs": text}, "2024-01-10T12:00:00+00:00"))
        repo.commit(f"reset {n}", {"src/lib.rs": LIB}, "2024-01-10T12:00:00+00:00")
    workdir = tmp_path / "work"
    open_checkout(SubprocessRunner(), str(repo.path), workdir)
    results: dict[str, bool] = {}
    errors: list[BaseException] = []

    def one(fix: str, out: Path) -> None:
        try:
            git = open_checkout(SubprocessRunner(), str(repo.path), workdir)
            commits = resolve_commits(git, fix, None, lambda _line: None)
            _, checks = split_commit(commits, "origin", out, lambda _line: None)
            results[f"{out.name}"] = checks.ok
        except BaseException as exc:  # reported below with its thread
            errors.append(exc)

    for round_ in range(2):
        threads = [
            threading.Thread(target=one, args=(fix, tmp_path / f"out{round_}-{n}"))
            for n, fix in enumerate(fixes)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
    assert errors == []
    assert len(results) == 6 and all(results.values()), results

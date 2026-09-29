from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from cargorewind.backend import BuildResult, Overlay, ReplayBackend, RunResult
from cargorewind.gitops import GitError
from cargorewind.rewind import RewindOptions, rewind
from cargorewind.runner import SubprocessRunner
from tests.conftest import GitRepo

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO = REPO_ROOT / "examples" / "strsim"
DEMO_FIX = "605c81c9b9"

LIB = """\
pub fn double(x: i32) -> i32 {
    x * 3
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn zero() {
        assert_eq!(double(0), 0);
    }
}
"""
FIXED = LIB.replace("x * 3", "x * 2").replace(
    "    use super::*;\n",
    "    use super::*;\n\n    #[test]\n    fn two() {\n        assert_eq!(double(2), 4);\n    }\n",
)


class ScriptedBackend:
    """Answers each stage with canned libtest output and records what it was sent."""

    def __init__(self, outputs: dict[str, tuple[int, str]]) -> None:
        self.outputs = outputs
        self.overlays: dict[str, Overlay] = {}
        self.dockerfile = ""

    def build(self, context: Path, tag: str) -> BuildResult:
        self.dockerfile = (context / "Dockerfile").read_text()
        assert (context / "repo" / "src" / "lib.rs").read_text() == LIB
        return BuildResult("sha256:fake", "built\n")

    def run_tests(self, tag: str, stage: str, overlay: Overlay) -> RunResult:
        self.overlays[stage] = overlay
        code, text = self.outputs[stage]
        return RunResult(code, text)


def _crate(repo: GitRepo, lockfile: bool) -> tuple[str, str]:
    files: dict[str, str | None] = {
        "Cargo.toml": '[package]\nname = "demo"\nversion = "0.1.0"\n',
        "src/lib.rs": LIB,
        "rust-toolchain": "1.70\n",
    }
    if lockfile:
        files["Cargo.lock"] = "version = 3\n"
    base = repo.commit("base", files, "2024-01-10T12:00:00+00:00")
    fix = repo.commit("fix", {"src/lib.rs": FIXED}, "2024-01-11T12:00:00+00:00")
    return base, fix


def test_rewind_synthetic_crate_end_to_end(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    repo = make_repo("origin")
    base, fix = _crate(repo, lockfile=True)
    backend = ScriptedBackend(
        {
            "base": (0, "test tests::zero ... ok\n"),
            "before": (101, "test tests::zero ... ok\ntest tests::two ... FAILED\n"),
            "after": (0, "test tests::zero ... ok\ntest tests::two ... ok\n"),
        }
    )
    lines: list[str] = []
    out = tmp_path / "out"
    options = RewindOptions(str(repo.path), fix[:8], out, tmp_path / "work", base=base)
    report = rewind(options, SubprocessRunner(), backend, lines.append)

    assert report.flip is not None and report.flip.verified
    task = json.loads((out / "task.json").read_text())
    assert task["FAIL_TO_PASS"] == ["tests::two"]
    assert task["PASS_TO_PASS"] == ["tests::zero"]
    assert task["toolchain"]["version"] == "1.70.0"
    assert task["toolchain"]["source"] == "rust-toolchain"
    assert task["lockfile"] == "committed"
    assert task["split"]["shared_files"] == ["src/lib.rs"]
    hunk = task["split"]["cfg_test_hunks"][0]
    assert (hunk["test_lines"], hunk["fix_lines"]) == (5, 2)
    assert task["split"]["test_files"] == task["split"]["fix_files"] == ["src/lib.rs"]
    assert task["runs"]["before"] == {
        "exit_code": 101,
        "timed_out": False,
        "passed": 1,
        "failed": 1,
        "ignored": 0,
    }
    assert "RUN cargo fetch --locked" in backend.dockerfile
    assert (out / "Dockerfile").read_text() == backend.dockerfile
    assert (out / "logs" / "before.log").read_text().endswith("FAILED\n")

    assert backend.overlays["base"] == Overlay()
    before = backend.overlays["before"].files["src/lib.rs"].decode()
    assert "fn two()" in before and "x * 3" in before
    assert backend.overlays["after"].files["src/lib.rs"].decode() == FIXED
    assert any(line.startswith("toolchain 1.70.0") for line in lines)
    assert not any("first parent" in line for line in lines)


def test_rewind_detects_patches_that_do_not_reproduce_the_fix(
    make_repo: Callable[[str], GitRepo], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo("origin")
    _, fix = _crate(repo, lockfile=False)
    monkeypatch.setattr("cargorewind.gitops.Git.matches", lambda self, rev, paths: False)
    backend = ScriptedBackend({})
    options = RewindOptions(str(repo.path), fix, tmp_path / "out", tmp_path / "work")
    with pytest.raises(GitError, match="do not reproduce"):
        rewind(options, SubprocessRunner(), backend, lambda _: None)


def test_rewind_replays_the_bundled_strsim_demo(tmp_path: Path) -> None:
    """The committed demo: a real Docker run of strsim-rs, replayed offline."""
    lines: list[str] = []
    out = tmp_path / "out"
    options = RewindOptions(str(DEMO / "strsim-rs.bundle"), DEMO_FIX, out, tmp_path / "work")
    report = rewind(
        options, SubprocessRunner(), ReplayBackend(DEMO / "transcript.json"), lines.append
    )

    assert report.flip is not None and report.flip.verified
    assert report.flip.fail_to_pass == [
        "tests::jaro_same_one_character",
        "tests::jaro_winkler_same_one_character",
    ]
    assert len(report.flip.pass_to_pass) == 102
    assert report.toolchain.version == "1.39.0"
    assert report.split.shared_files == ["src/lib.rs"]
    assert [h.test_lines for h in report.split.shared_hunks] == [5, 5]
    assert "@@ -72,9 +72,11 @@" in (out / "fix.patch").read_text()
    assert "fn jaro_same_one_character" in (out / "test.patch").read_text()
    assert "RUN cargo generate-lockfile" in (out / "Dockerfile").read_text()
    assert "base      c4cdd9c35dfa (first parent of fix)" in lines

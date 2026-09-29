from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from cargorewind.backend import BuildResult, Overlay, ReplayBackend, RunResult
from cargorewind.deps import LOCK_BEGIN, LOCK_END, Pin
from cargorewind.gitops import GitError
from cargorewind.registry import HttpResponse, ImageResolver, RegistryClient
from cargorewind.rewind import RewindOptions, rewind
from cargorewind.runner import SubprocessRunner
from cargorewind.toolchain import IMAGE_DIGESTS, ToolchainError
from tests.conftest import GitRepo
from tests.test_deps import INDEX, FakeCargo
from tests.test_registry import FakeHttp

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


class CargoModelSession:
    """Runs the pin loop's scripts against ``FakeCargo``, one cargo command at a time."""

    def __init__(self, cargo: FakeCargo) -> None:
        self.cargo = cargo
        self.steps: list[str] = []
        self.closed = False

    def run(self, step: str, script: str) -> RunResult:
        self.steps.append(step)
        out: list[str] = []
        lock = ""
        for line in script.splitlines():
            if line.startswith("cargo generate-lockfile"):
                lock = self.cargo.generate()
                out.append("--- cargorewind: exit 0 generate ---")
            elif line.startswith("cargo update -p "):
                words = line.split(";")[0].split()
                spec, version, label = words[3], words[5], line.rsplit(" ", 2)[-2]
                name, old = spec.split(":")
                lock, failed = self.cargo.pin([Pin(name, old, version, "")])
                code = 101 if failed else 0
                out += [*failed.values(), f"--- cargorewind: exit {code} {label} ---"]
        out += [LOCK_BEGIN, lock or self.cargo.render(), LOCK_END]
        return RunResult(0, "\n".join(out) + "\n")

    def close(self) -> None:
        self.closed = True


class ScriptedBackend:
    """Answers each stage with canned libtest output and records what it was sent."""

    def __init__(
        self, outputs: dict[str, tuple[int, str]], session: CargoModelSession | None = None
    ) -> None:
        self.outputs = outputs
        self.overlays: dict[str, Overlay] = {}
        self.commands: dict[str, tuple[str, ...]] = {}
        self.builds: list[tuple[str, str | None, bool]] = []
        self.dockerfile = ""
        self.session = session

    def build(self, context: Path, tag: str, target: str | None = None) -> BuildResult:
        self.dockerfile = (context / "Dockerfile").read_text()
        assert (context / "repo" / "src" / "lib.rs").read_text() == LIB
        self.builds.append((tag, target, (context / "Cargo.lock").exists()))
        return BuildResult("sha256:fake", "built\n")

    def run_tests(
        self, tag: str, stage: str, overlay: Overlay, command: tuple[str, ...] = ()
    ) -> RunResult:
        self.overlays[stage] = overlay
        self.commands[stage] = command
        code, text = self.outputs[stage]
        return RunResult(code, text)

    def open_session(self, tag: str) -> CargoModelSession:
        assert self.session is not None, "no session expected"
        return self.session


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
    assert task["image"] == f"rust:1.70.0-slim@{IMAGE_DIGESTS['1.70.0']}"
    assert task["image_source"] == {"source": "offline-table", "reason": "offline digest table"}
    assert task["toolchain_report"] == "toolchain.json"
    report = json.loads((out / "toolchain.json").read_text())
    assert report["toolchain"]["version"] == "1.70.0"
    assert report["image"]["reference"] == task["image"]
    assert task["split"]["shared_files"] == ["src/lib.rs"]
    hunk = task["split"]["cfg_test_hunks"][0]
    assert (hunk["test_lines"], hunk["fix_lines"]) == (5, 2)
    assert task["split"]["test_files"] == task["split"]["fix_files"] == ["src/lib.rs"]
    assert task["split"]["report"] == "split.json"
    split_doc = json.loads((out / "split.json").read_text())
    assert split_doc["checks"]["patches_reproduce_fix"] is True
    assert split_doc["files"][0]["role"] == "source"
    assert task["runs"]["before"] == {
        "exit_code": 101,
        "timed_out": False,
        "passed": 1,
        "failed": 1,
        "ignored": 0,
    }
    assert "RUN cargo fetch --locked" in backend.dockerfile
    # The checkout carries rust-toolchain, so rustup must not follow it at run time.
    assert "ENV RUSTUP_TOOLCHAIN=1.70.0" in backend.dockerfile
    assert task["toolchain"]["toolchain_file"] == "rust-toolchain"
    assert [d["step"] for d in task["toolchain"]["decisions"]][:2] == ["file", "channel"]
    assert (out / "Dockerfile").read_text() == backend.dockerfile
    assert (out / "logs" / "before.log").read_text().endswith("FAILED\n")

    assert backend.overlays["base"] == Overlay()
    before = backend.overlays["before"].files["src/lib.rs"].decode()
    assert "fn two()" in before and "x * 3" in before
    assert backend.overlays["after"].files["src/lib.rs"].decode() == FIXED
    assert any(line.startswith("toolchain 1.70.0") for line in lines)
    assert not any("first parent" in line for line in lines)


PASSING = {
    "base": (0, "test tests::zero ... ok\n"),
    "before": (101, "test tests::zero ... ok\ntest tests::two ... FAILED\n"),
    "after": (0, "test tests::zero ... ok\ntest tests::two ... ok\n"),
}


def test_rewind_image_from_the_registry_or_an_override(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    repo = make_repo("origin")
    _, fix = _crate(repo, lockfile=True)
    digest = "sha256:" + "d" * 64
    head = HttpResponse(
        200,
        {
            "docker-content-digest": digest,
            "content-type": "application/vnd.oci.image.index.v1+json",
        },
    )
    http = FakeHttp({("HEAD", "v2/library/rust/manifests/1.70.0-slim"): head})
    resolver = ImageResolver(registry=RegistryClient(http, auth=None))
    out = tmp_path / "out"
    options = RewindOptions(str(repo.path), fix, out, tmp_path / "work", resolver=resolver)
    backend = ScriptedBackend(PASSING)
    rewind(options, SubprocessRunner(), backend, lambda _: None)
    task = json.loads((out / "task.json").read_text())
    assert task["image"] == f"rust:1.70.0-slim@{digest}"
    assert task["image_source"]["source"] == "registry"
    assert task["image_source"]["reason"].endswith("differs from the offline table")
    assert f"FROM rust:1.70.0-slim@{digest}" in backend.dockerfile

    pinned = "rust:1.70.0-slim@sha256:" + "e" * 64
    options = RewindOptions(str(repo.path), fix, out, tmp_path / "work", image=pinned)
    rewind(options, SubprocessRunner(), ScriptedBackend(PASSING), lambda _: None)
    task = json.loads((out / "task.json").read_text())
    assert (task["image"], task["image_source"]["source"]) == (pinned, "override")


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
    assert [h.test_lines for h in report.split.shared_hunks] == [0, 5, 5]
    assert [h.fix_lines for h in report.split.shared_hunks] == [6, 0, 0]
    assert "@@ -72,9 +72,11 @@" in (out / "fix.patch").read_text()
    assert "fn jaro_same_one_character" in (out / "test.patch").read_text()
    assert "RUN cargo generate-lockfile" in (out / "Dockerfile").read_text()
    assert "base      c4cdd9c35dfa (first parent of fix)" in lines
    lock = json.loads((out / "lock.json").read_text())
    assert (lock["strategy"], lock["requirements"], lock["rounds"]) == ("generated", [], [])


HOME_MANIFEST = '[package]\nname = "demo"\nversion = "0.1.0"\n\n[dependencies]\nhome = "0.5.4"\n'


def _home_crate(repo: GitRepo) -> tuple[str, str]:
    files: dict[str, str | None] = {
        "Cargo.toml": HOME_MANIFEST,
        "src/lib.rs": LIB,
        "rust-toolchain": "1.75\n",
    }
    base = repo.commit("base", files, "2024-02-20T12:00:00+00:00")
    fix = repo.commit("fix", {"src/lib.rs": FIXED}, "2024-03-01T00:00:00+00:00")
    return base, fix


def test_rewind_bounds_a_missing_lockfile_by_the_commit_date(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    repo = make_repo("origin")
    base, fix = _home_crate(repo)
    session = CargoModelSession(FakeCargo(INDEX, {"demo": [("home", "0.5.4")]}))
    backend = ScriptedBackend(PASSING, session)
    out = tmp_path / "out"
    lines: list[str] = []
    options = RewindOptions(str(repo.path), fix, out, tmp_path / "work", index=INDEX, vendor=True)
    report = rewind(options, SubprocessRunner(), backend, lines.append)

    assert report.flip is not None and report.flip.verified
    (stage, final) = backend.builds
    assert stage[0].startswith("cargorewind/toolchain-stage:") and stage[1:] == ("toolchain", False)
    assert final[0].startswith(f"cargorewind/origin:{base[:12]}-") and final[1:] == (None, True)
    assert session.closed and session.steps == [
        "generate-lockfile",
        "pin-round-1",
        "pin-round-2",
        "pin-round-3",
    ]
    lock_text = (out / "Cargo.lock").read_text()
    assert 'name = "home"\nversion = "0.5.9"' in lock_text
    lock = json.loads((out / "lock.json").read_text())
    assert lock["strategy"] == "date-bounded" and lock["bounded"] is True
    assert lock["cutoff"] == "2024-03-01T00:00:00+00:00"
    assert [len(r["pins"]) for r in lock["rounds"]] == [1, 1, 7]
    assert lock["requirements"][0]["name"] == "home"
    assert (lock["vendor"], lock["cargo_config"]) == (True, "config.toml")
    task = json.loads((out / "task.json").read_text())
    assert (task["lockfile"], task["lock_report"], task["vendored"]) == (
        "date-bounded",
        "lock.json",
        True,
    )
    assert task["test_command"] == "cargo test --no-fail-fast --offline"
    assert backend.commands["after"][-1] == "--offline"
    dockerfile = (out / "Dockerfile").read_text()
    assert (
        "FROM toolchain" in dockerfile and "COPY --chown=rewind:rewind Cargo.lock ./" in dockerfile
    )
    assert "cargo vendor --locked /home/rewind/vendor" in dockerfile
    assert any(line.startswith("lockfile  none: 1 crates.io requirement(s)") for line in lines)
    assert any("9 pin(s) in 3 round(s); every crates.io package is bounded" in ln for ln in lines)


def test_rewind_refuses_to_vendor_with_a_cargo_that_has_no_vendor(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    repo = make_repo("origin")
    base = repo.commit(
        "base",
        {"Cargo.toml": HOME_MANIFEST, "src/lib.rs": LIB, "rust-toolchain": "1.36\n"},
        "2019-08-01T00:00:00+00:00",
    )
    fix = repo.commit("fix", {"src/lib.rs": FIXED}, "2019-08-02T00:00:00+00:00")
    options = RewindOptions(str(repo.path), fix, tmp_path / "out", tmp_path / "w", vendor=True)
    with pytest.raises(ToolchainError, match=r"--vendor needs cargo 1\.37\.0\+"):
        rewind(options, SubprocessRunner(), ScriptedBackend({}), lambda _: None)
    assert base

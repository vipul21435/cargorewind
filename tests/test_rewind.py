from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from cargorewind import rewind as rewind_module
from cargorewind.backend import BuildResult, Overlay, ReplayBackend, RunResult
from cargorewind.buildcache import BuildCache
from cargorewind.bundle import read_task
from cargorewind.crateindex import DirectoryIndex
from cargorewind.deps import LOCK_BEGIN, LOCK_END, Pin
from cargorewind.dockerfile import PROBE_MARKER
from cargorewind.flip import compute_flip, stage_tests
from cargorewind.gitops import GitError
from cargorewind.libtest import Status, parse_libtest
from cargorewind.probes import HostCheck, ProbeReport
from cargorewind.registry import HttpResponse, ImageResolver, RegistryClient
from cargorewind.rewind import ProbeError, RewindOptions, StageRun, rewind
from cargorewind.runner import SubprocessRunner
from cargorewind.testtargets import TargetMap, rerun_command
from cargorewind.toolchain import IMAGE_DIGESTS, ToolchainError
from tests.conftest import GitRepo
from tests.test_buildcache import FakeDocker
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


RerunHook = Callable[[str, int, str], Status | None]  # (stage, round, test name) -> status


class ScriptedBackend:
    """Answers each stage with canned libtest output and records what it was sent.

    Rerun scripts are answered test by test from the stage's output, unless ``reruns``
    gives another status for that stage, round and test (flaky scenarios).
    """

    def __init__(
        self,
        outputs: dict[str, tuple[int, str]],
        session: CargoModelSession | None = None,
        reruns: RerunHook | None = None,
    ) -> None:
        self.outputs = outputs
        self.overlays: dict[str, Overlay] = {}
        self.commands: dict[str, tuple[str, ...]] = {}
        self.builds: list[tuple[str, str | None, bool]] = []
        self.no_cache: list[bool] = []
        self.probes: dict[str, tuple[str, ...]] = {}
        self.tags: dict[str, str] = {}
        self.dockerfile = ""
        self.session = session
        self.reruns = reruns
        self.scripts: dict[str, str] = {}
        self.rerun_overlays: dict[str, Overlay] = {}

    def build(
        self, context: Path, tag: str, target: str | None = None, *, no_cache: bool = False
    ) -> BuildResult:
        self.dockerfile = (context / "Dockerfile").read_text()
        assert (context / "repo" / "src" / "lib.rs").read_text() == LIB
        self.builds.append((tag, target, (context / "Cargo.lock").exists()))
        self.no_cache.append(no_cache)
        return BuildResult("sha256:fake", "built\n")

    def run_tests(
        self,
        tag: str,
        stage: str,
        overlay: Overlay,
        command: tuple[str, ...] = (),
        probes: tuple[str, ...] = (),
    ) -> RunResult:
        self.overlays[stage] = overlay
        self.commands[stage] = command
        self.probes[stage] = probes
        self.tags[stage] = tag
        code, text = self.outputs[stage]
        return RunResult(code, text)

    def open_session(self, tag: str) -> CargoModelSession:
        assert self.session is not None, "no session expected"
        return self.session

    def run_script(self, tag: str, run: str, overlay: Overlay, script: str) -> RunResult:
        self.scripts[run] = script
        self.rerun_overlays[run] = overlay
        stage = run.removeprefix("rerun-")
        known = parse_libtest(self.outputs[stage][1]).by_name()
        out: list[str] = []
        round_ = 0
        for line in script.splitlines():
            begin = re.match(r"^echo '(--- cargorewind: rerun (\d+) \d+ ---)'$", line)
            if begin is not None:
                out.append(begin.group(1))
                round_ = int(begin.group(2))
                continue
            command = re.match(r"^timeout -k \d+ \d+ (.*) 2>&1; echo ", line)
            assert command is not None, line
            argv = shlex.split(command.group(1))
            name = argv[argv.index("--exact") + 1] if "--exact" in argv else argv[-1]
            status = (self.reruns(stage, round_, name) if self.reruns else None) or known.get(name)
            if status is None:
                out += ["running 0 tests", "--- cargorewind: exit 0 ---"]
                continue
            word = {Status.PASSED: "ok", Status.FAILED: "FAILED", Status.IGNORED: "ignored"}[status]
            code = 101 if status is Status.FAILED else 0
            out += [
                "running 1 test",
                f"test {name} ... {word}",
                f"--- cargorewind: exit {code} ---",
            ]
        return RunResult(0, "\n".join(out) + "\n")


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
        "state": "ran",
        "passed": 1,
        "failed": 1,
        "ignored": 0,
    }
    # Both candidates were rerun three times by exact name in before and after.
    assert sorted(backend.scripts) == ["rerun-after", "rerun-before"]
    assert (
        backend.scripts["rerun-after"].count("cargo test --no-fail-fast -- --exact tests::two") == 3
    )
    assert backend.rerun_overlays["rerun-after"] == backend.overlays["after"]
    assert task["reruns"]["rounds"] == 3 and task["reruns"]["test_timeout"] == 300
    assert task["reruns"]["stages"]["before"] == {
        "tests": 2,
        "exit_code": 0,
        "timed_out": False,
        "changed": 0,
        "log": "logs/rerun-before.log",
    }
    assert task["tests"]["tests::two"] == {
        "target": "unknown",
        "name": "tests::two",
        "command": "cargo test --no-fail-fast -- --exact tests::two",
        "statuses": {"base": "missing", "before": "failed", "after": "passed"},
        "reruns": {"before": ["failed"] * 3, "after": ["passed"] * 3},
    }
    assert task["flaky"] == []
    assert (
        (out / "logs" / "rerun-after.log").read_text().startswith("--- cargorewind: rerun 1 0 ---")
    )
    assert any(
        ln.startswith("rerun     after  exit   0  3 x 2 test(s) by exact name") for ln in lines
    )
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


def test_rewind_writes_a_self_contained_bundle(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    repo = make_repo("origin")
    base, fix = _crate(repo, lockfile=True)
    out = tmp_path / "out"
    options = RewindOptions(str(repo.path), fix, out, tmp_path / "work", base=base)
    rewind(options, SubprocessRunner(), ScriptedBackend(PASSING), lambda _: None)
    task = json.loads((out / "task.json").read_text())
    assert task["schema_version"] == 2
    assert (out / "fail_to_pass.txt").read_text() == "tests::two\n"
    assert (out / "pass_to_pass.txt").read_text() == "tests::zero\n"
    # The base tree travels as a one-commit git bundle whose tree id is the base
    # commit's, and task.json lists the sha256 of every other file.
    assert task["base_tree"]["file"] == "base.bundle"
    assert task["base_tree"]["tree"] == repo.git("rev-parse", f"{base}^{{tree}}").strip()
    heads = repo.git("bundle", "list-heads", str(out / "base.bundle"))
    assert heads.split() == [task["base_tree"]["commit"], "refs/heads/cargorewind-bundle"]
    work = ["git", "-C", str(tmp_path / "work" / "repo"), "branch", "--list", "cargorewind-bundle"]
    assert subprocess.run(work, check=True, capture_output=True, text=True).stdout == ""
    assert set(task["files"]) >= {"Dockerfile", "base.bundle", "logs/rerun-after.log"}
    digest = hashlib.sha256((out / "fix.patch").read_bytes()).hexdigest()
    assert task["files"]["fix.patch"] == digest and "task.json" not in task["files"]
    assert read_task(out / "task.json").fail_to_pass == ["tests::two"]
    # The same base commit gives the same root commit on every run.
    again = RewindOptions(str(repo.path), fix, tmp_path / "again", tmp_path / "work", base=base)
    rewind(again, SubprocessRunner(), ScriptedBackend(PASSING), lambda _: None)
    assert (
        json.loads((tmp_path / "again" / "task.json").read_text())["base_tree"] == task["base_tree"]
    )


def test_rewind_probes_and_recipe_of_the_synthetic_crate(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    repo = make_repo("origin")
    base, fix = _crate(repo, lockfile=True)
    backend = ScriptedBackend(PASSING)
    lines: list[str] = []
    out = tmp_path / "out"
    options = RewindOptions(str(repo.path), fix, out, tmp_path / "work", base=base)
    rewind(options, SubprocessRunner(), backend, lines.append)
    task = json.loads((out / "task.json").read_text())
    # Sanity probes: the test adds fn two, which must be absent at base.
    assert task["probes"]["identifiers"] == [
        {"kind": "fn", "name": "two", "patch": "test", "path": "src/lib.rs", "line": 10}
    ]
    assert task["probes"]["ok"] is True and task["verified"] is True
    assert task["probes"]["container_checks"] == {
        "build": "passed",
        "before": "passed",
        "after": "passed",
    }
    assert "        -e two \\\n" in backend.dockerfile
    assert backend.probes == {"base": (), "before": ("two",), "after": ("two",)}
    probes = json.loads((out / "probes.json").read_text())
    assert probes["patch_checks"] == {
        "test_patch_applies_at_base": True,
        "fix_patch_applies_after_test_patch": True,
        "patches_reproduce_fix": True,
    }
    assert [c["ok"] for c in probes["host_checks"]] == [True, True, True, True]
    assert any(
        ln.startswith("probe     test fn two (src/lib.rs:10): absent at base") for ln in lines
    )

    # The recipe: its hash tags the image and labels the Dockerfile.
    recipe = json.loads((out / "recipe.json").read_text())
    assert task["recipe"] == {"hash": recipe["hash"], "report": "recipe.json"}
    assert backend.dockerfile.endswith(f"LABEL cargorewind.recipe={recipe['hash']}\n")
    assert task["image_tag"] == f"cargorewind/origin:{recipe['hash'][:16]}"
    assert backend.tags["after"] == task["image_tag"]
    assert task["build_cache"]["status"] == "off"
    assert backend.no_cache == [False]


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


def test_rewind_pins_the_toolchain_when_the_fix_adds_a_toolchain_file(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    # Regression: only a toolchain file at base set RUSTUP_TOOLCHAIN. A fix that adds
    # rust-toolchain put the file into the after overlay, and rustup then tried to
    # install "stable" under --network none, so a correct fix was NOT VERIFIED.
    repo = make_repo("origin")
    manifest = '[package]\nname = "demo"\nversion = "0.1.0"\n'
    files = {"Cargo.toml": manifest, "src/lib.rs": LIB, "Cargo.lock": "version = 3\n"}
    repo.commit("base", files, "2024-01-10T12:00:00+00:00")
    fix = repo.commit(
        "fix", {"src/lib.rs": FIXED, "rust-toolchain": "stable\n"}, "2024-01-11T12:00:00+00:00"
    )
    backend = ScriptedBackend(PASSING)
    out = tmp_path / "out"
    lines: list[str] = []
    options = RewindOptions(str(repo.path), fix, out, tmp_path / "work")
    rewind(options, SubprocessRunner(), backend, lines.append)

    assert sorted(backend.overlays["after"].files) == ["rust-toolchain", "src/lib.rs"]
    assert "ENV RUSTUP_TOOLCHAIN=1.75.0" in backend.dockerfile
    task = json.loads((out / "task.json").read_text())
    assert task["toolchain"]["toolchain_file"] is None  # the base has none
    pin = task["toolchain"]["decisions"][-1]
    assert (pin["step"], pin["outcome"]) == ("pin", "1.75.0")
    assert pin["reason"].startswith("the patches add or change rust-toolchain; every stage keeps")
    report = json.loads((out / "toolchain.json").read_text())
    assert report["toolchain"]["decisions"][-1] == pin


TRIPLE = LIB.replace(
    "#[cfg(test)]",
    "pub fn triple(x: i32) -> i32 {\n    x * 3\n}\n\n#[cfg(test)]",
).replace(
    "    use super::*;\n",
    "    use super::*;\n\n    #[test]\n    fn triple_works() {\n"
    "        assert_eq!(triple(1), 3);\n    }\n",
)


def _triple_crate(repo: GitRepo) -> str:
    files = {
        "Cargo.toml": '[package]\nname = "demo"\nversion = "0.1.0"\n',
        "src/lib.rs": LIB,
        "rust-toolchain": "1.70\n",
        "Cargo.lock": "version = 3\n",
    }
    repo.commit("base", files, "2024-01-10T12:00:00+00:00")
    return repo.commit("fix", {"src/lib.rs": TRIPLE}, "2024-01-11T12:00:00+00:00")


TRIPLE_RUNS = {
    "base": (0, "test tests::zero ... ok\n"),
    "before": (101, "error[E0425]: cannot find function `triple` in this scope\n"),
    "after": (0, "test tests::zero ... ok\ntest tests::triple_works ... ok\n"),
}


def test_rewind_marks_a_test_flaky_when_a_rerun_changes_its_outcome(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    repo = make_repo("origin")
    base, fix = _crate(repo, lockfile=True)

    def flaky(stage: str, round_: int, name: str) -> Status | None:
        if (stage, round_, name) == ("after", 2, "tests::two"):
            return Status.FAILED
        if (stage, name) == ("before", "tests::zero") and round_ == 3:
            return Status.IGNORED
        return None

    backend = ScriptedBackend(PASSING, reruns=flaky)
    lines: list[str] = []
    out = tmp_path / "out"
    options = RewindOptions(str(repo.path), fix, out, tmp_path / "work", base=base, reruns=3)
    report = rewind(options, SubprocessRunner(), backend, lines.append)

    assert report.flip is not None and not report.flip.verified
    task = json.loads((out / "task.json").read_text())
    assert task["FAIL_TO_PASS"] == [] and task["PASS_TO_PASS"] == []
    assert task["flaky"] == [
        {
            "id": "tests::two",
            "reason": "outcome changed between the stage run and its reruns: "
            "after passed, passed, failed, passed",
        },
        {
            "id": "tests::zero",
            "reason": "outcome changed between the stage run and its reruns: "
            "before passed, passed, passed, ignored",
        },
    ]
    assert task["tests"]["tests::two"]["reruns"] == {
        "before": ["failed"] * 3,
        "after": ["passed", "failed", "passed"],
    }
    assert task["reruns"]["stages"]["after"]["changed"] == 1
    assert task["verified"] is False
    assert "rerun     after  exit   0  3 x 2 test(s) by exact name, 1 changed outcome" in lines
    assert any(ln.startswith("flaky     tests::two: outcome changed") for ln in lines)


def test_rewind_without_reruns_and_with_a_base_rerun_for_a_compile_error(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    repo = make_repo("origin")
    base, fix = _crate(repo, lockfile=True)
    backend = ScriptedBackend(PASSING)
    out = tmp_path / "out"
    options = RewindOptions(str(repo.path), fix, out, tmp_path / "work", base=base, reruns=0)
    report = rewind(options, SubprocessRunner(), backend, lambda _: None)
    assert report.flip is not None and report.flip.verified
    assert backend.scripts == {} and report.reruns == {}
    task = json.loads((out / "task.json").read_text())
    assert task["reruns"] == {"rounds": 0, "test_timeout": 300, "stages": {}}
    assert task["tests"]["tests::two"]["reruns"] == {}

    # The before run does not compile: PASS_TO_PASS came from base, so base is rerun.
    triple = make_repo("triple")
    fix = _triple_crate(triple)
    backend = ScriptedBackend(TRIPLE_RUNS)
    options = RewindOptions(str(triple.path), fix, out, tmp_path / "w2", reruns=2, test_timeout=9)
    report = rewind(options, SubprocessRunner(), backend, lambda _: None)
    assert report.flip is not None and report.flip.verified
    assert sorted(backend.scripts) == ["rerun-after", "rerun-base"]
    assert (
        "timeout -k 10 9 cargo test --no-fail-fast -- --exact tests::zero"
        in backend.scripts["rerun-base"]
    )
    assert "tests::triple_works" not in backend.scripts["rerun-base"]
    task = json.loads((out / "task.json").read_text())
    assert task["runs"]["before"]["state"] == "compile-error"
    assert task["tests"]["tests::triple_works"]["statuses"]["before"] == "compile-error"
    assert task["tests"]["tests::zero"]["reruns"] == {
        "base": ["passed"] * 2,
        "after": ["passed"] * 2,
    }
    assert task["reruns"]["stages"]["base"]["tests"] == 1


def test_rewind_asks_a_nightly_toolchain_for_json_output(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    repo = make_repo("origin")
    manifest = '[package]\nname = "demo"\nversion = "0.1.0"\n'
    files = {
        "Cargo.toml": manifest,
        "src/lib.rs": LIB,
        "Cargo.lock": "version = 3\n",
        "rust-toolchain": "nightly-2024-01-01\n",
    }
    repo.commit("base", files, "2024-01-10T12:00:00+00:00")
    fix = repo.commit("fix", {"src/lib.rs": FIXED}, "2024-01-11T12:00:00+00:00")
    json_runs = {
        stage: (code, "".join(_json_line(line) for line in text.splitlines()))
        for stage, (code, text) in PASSING.items()
    }
    backend = ScriptedBackend(json_runs)
    out = tmp_path / "out"
    options = RewindOptions(str(repo.path), fix, out, tmp_path / "work", reruns=1)
    report = rewind(options, SubprocessRunner(), backend, lambda _: None)
    assert report.flip is not None and report.flip.fail_to_pass == ["tests::two"]
    json_args = ("--", "-Z", "unstable-options", "--format", "json")
    assert backend.commands["after"] == ("cargo", "test", "--no-fail-fast", *json_args)
    assert (
        "cargo test --no-fail-fast -- --exact tests::two -Z unstable-options --format json"
        in (backend.scripts["rerun-after"])
    )
    task = json.loads((out / "task.json").read_text())
    assert task["test_command"].endswith("--format json")
    assert task["runs"]["after"] == {
        "exit_code": 0,
        "timed_out": False,
        "state": "ran",
        "passed": 2,
        "failed": 0,
        "ignored": 0,
    }


def _json_line(text_line: str) -> str:
    name, _, word = text_line.removeprefix("test ").partition(" ... ")
    event = {"ok": "ok", "FAILED": "failed"}[word]
    return json.dumps({"type": "test", "name": name, "event": event}) + "\n"


def test_rewind_probes_a_new_function_of_the_fix(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    repo = make_repo("origin")
    fix = _triple_crate(repo)
    backend = ScriptedBackend(TRIPLE_RUNS)
    out = tmp_path / "out"
    report = rewind(
        RewindOptions(str(repo.path), fix, out, tmp_path / "work"),
        SubprocessRunner(),
        backend,
        lambda _: None,
    )
    assert [(p.patch, p.name) for p in report.probes.probes] == [
        ("fix", "triple"),
        ("test", "triple_works"),
    ]
    # The before run needs only the test's name; the test calls triple, which must not
    # be defined yet (it is not: that is why the before run fails to compile).
    assert backend.probes["before"] == ("triple_works",)
    assert backend.probes["after"] == ("triple", "triple_works")
    assert report.verified and report.flip is not None
    assert report.flip.fail_to_pass == ["tests::triple_works"]
    assert report.image_id == "sha256:fake"
    assert "        -e triple \\\n        -e triple_works \\\n" in backend.dockerfile


def test_a_failed_stage_probe_blocks_verification(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    repo = make_repo("origin")
    fix = _triple_crate(repo)
    runs = {**TRIPLE_RUNS, "after": (97, f"{PROBE_MARKER} triple is missing\n")}
    out = tmp_path / "out"
    options = RewindOptions(str(repo.path), fix, out, tmp_path / "work")
    report = rewind(options, SubprocessRunner(), ScriptedBackend(runs), lambda _: None)
    assert not report.verified
    task = json.loads((out / "task.json").read_text())
    assert task["verified"] is False and task["probes"]["ok"] is False
    assert task["probes"]["container_checks"]["after"] == "failed"
    assert json.loads((out / "probes.json").read_text())["ok"] is False


def test_a_failed_host_probe_stops_before_docker(
    make_repo: Callable[[str], GitRepo], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo("origin")
    fix = _triple_crate(repo)
    real = rewind_module.plan_probes

    def broken(*args: Any, **kwargs: Any) -> ProbeReport:
        report = real(*args, **kwargs)
        report.checks[0] = HostCheck("base", "no probe identifier occurs at base", False, "x")
        return report

    monkeypatch.setattr(rewind_module, "plan_probes", broken)
    backend = ScriptedBackend({})
    out = tmp_path / "out"
    options = RewindOptions(str(repo.path), fix, out, tmp_path / "work")
    with pytest.raises(ProbeError, match="sanity probe failed on the host"):
        rewind(options, SubprocessRunner(), backend, lambda _: None)
    assert backend.builds == []
    assert json.loads((out / "probes.json").read_text())["ok"] is False


class LabellingBackend(ScriptedBackend):
    """Registers each build in FakeDocker with the recipe label of its Dockerfile."""

    def __init__(self, docker: FakeDocker) -> None:
        super().__init__(PASSING)
        self.docker = docker

    def build(
        self, context: Path, tag: str, target: str | None = None, *, no_cache: bool = False
    ) -> BuildResult:
        result = super().build(context, tag, target, no_cache=no_cache)
        label = self.dockerfile.rstrip().rsplit("=", 1)[-1]
        self.docker.images[tag] = (f"sha256:built{len(self.builds)}", label)
        return result


def test_rewind_reuses_the_image_of_an_unchanged_recipe(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    repo = make_repo("origin")
    _, fix = _crate(repo, lockfile=True)
    docker = FakeDocker()
    backend = LabellingBackend(docker)
    cache = BuildCache(tmp_path / "cache", docker)
    lines: list[str] = []

    def run(out: str, rebuild: bool = False) -> dict[str, Any]:
        options = RewindOptions(
            str(repo.path), fix, tmp_path / out, tmp_path / "work", cache=cache, rebuild=rebuild
        )
        rewind(options, SubprocessRunner(), backend, lines.append)
        return dict(json.loads((tmp_path / out / "task.json").read_text()))

    first, second = run("one"), run("two")
    assert first["build_cache"]["status"] == "miss"
    assert second["build_cache"]["status"] == "hit"
    assert first["recipe"] == second["recipe"] and first["image_tag"] == second["image_tag"]
    assert len(backend.builds) == 1 and second["verified"] is True
    assert any(ln.startswith(f"build     {first['image_tag']} (cache hit: image ") for ln in lines)
    third = run("three", rebuild=True)
    assert third["build_cache"]["status"] == "miss"
    assert third["build_cache"]["reason"].startswith("--rebuild: built with --no-cache")
    assert backend.no_cache == [False, True]
    assert list(cache.entries()) == [first["recipe"]["hash"]]


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
    _, fix = _home_crate(repo)
    session = CargoModelSession(FakeCargo(INDEX, {"demo": [("home", "0.5.4")]}))
    backend = ScriptedBackend(PASSING, session)
    out = tmp_path / "out"
    lines: list[str] = []
    options = RewindOptions(str(repo.path), fix, out, tmp_path / "work", index=INDEX, vendor=True)
    report = rewind(options, SubprocessRunner(), backend, lines.append)

    assert report.flip is not None and report.flip.verified
    (stage, final) = backend.builds
    assert stage[0].startswith("cargorewind/toolchain-stage:") and stage[1:] == ("toolchain", False)
    recipe = json.loads((out / "recipe.json").read_text())
    assert final == (f"cargorewind/origin:{recipe['hash'][:16]}", None, True)
    assert recipe["lock"] == "date-bounded" and len(recipe["lockfile_sha256"]) == 64
    assert (
        recipe["lockfile_sha256"] == hashlib.sha256((out / "Cargo.lock").read_bytes()).hexdigest()
    )
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


def test_rewind_reads_the_index_through_the_given_cache_dir(
    make_repo: Callable[[str], GitRepo], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression: rewind built the live index without --cache-dir, so index files always
    # went to the home cache even when the run was pointed at another directory.
    repo = make_repo("origin")
    _, fix = _home_crate(repo)
    seen: list[tuple[object, Path | None]] = []

    def spy(cutoff: object, cache_dir: Path | None = None) -> DirectoryIndex:
        seen.append((cutoff, cache_dir))
        return INDEX

    monkeypatch.setattr(rewind_module, "default_index", spy)
    session = CargoModelSession(FakeCargo(INDEX, {"demo": [("home", "0.5.4")]}))
    cache = tmp_path / "private-cache"
    options = RewindOptions(str(repo.path), fix, tmp_path / "out", tmp_path / "w", cache_dir=cache)
    rewind(options, SubprocessRunner(), ScriptedBackend(PASSING, session), lambda _: None)
    assert [c for _, c in seen] == [cache]


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


@dataclass
class _TableState:
    """The part of an ``Execution`` that ``tests_table`` reads."""

    runs: dict[str, StageRun]
    recipe: Any


def test_the_rerun_command_of_every_test_row_is_shell_quoted(tmp_path: Path) -> None:
    # rustdoc prints item paths with generics and lifetimes; the item path is the filter.
    output = (
        "   Doc-tests x\n\nrunning 3 tests\n"
        "test src/lib.rs - Parser<'a>::new (line 3) ... ok\n"
        "test src/lib.rs - Wrapper<T>::get (line 9) ... ok\n"
        "test src/lib.rs - Out>file (line 20) ... ok\n"
    )
    runs = {
        stage: StageRun(stage, run, stage_tests(stage, run, TargetMap()))
        for stage, run in (("base", RunResult(0, output)), ("before", RunResult(0, output)))
    }
    runs["after"] = StageRun("after", RunResult(0, output), runs["before"].tests)
    flip = compute_flip(*(runs[s].tests for s in ("base", "before", "after")))
    state = _TableState(runs, SimpleNamespace(test_command=("cargo", "test", "--no-fail-fast")))
    table = rewind_module.tests_table(cast(Any, state), flip)
    commands = {test_id: row.command for test_id, row in table.items()}
    assert commands["src/lib.rs - Parser<'a>::new"] == "cargo test --doc -- 'Parser<'\"'\"'a>::new'"
    for test_id, command in commands.items():
        key = flip.keys[test_id]
        raw = runs["after"].tests.results[key].raw
        argv = rerun_command(key[0], raw, ("cargo", "test", "--no-fail-fast"))
        assert shlex.split(command) == list(argv)
        # A shell reads the same words back, with no redirection or open quote.
        echoed = subprocess.run(
            ["sh", "-c", 'printf "%s\\n" ' + command.removeprefix("cargo ")],
            capture_output=True,
            text=True,
            check=True,
            cwd=tmp_path,
        )
        assert echoed.stdout.splitlines() == list(argv[1:])
    assert list(tmp_path.iterdir()) == []  # nothing was redirected into a file


class StoppedReruns(ScriptedBackend):
    """The after stage's rerun run hits --timeout when round 2 is about to start."""

    def run_script(self, tag: str, run: str, overlay: Overlay, script: str) -> RunResult:
        result = super().run_script(tag, run, overlay, script)
        if run != "rerun-after":
            return result
        index = next(i for i, ln in enumerate(script.splitlines()) if "tests::two" in ln) // 2
        cut = result.output.index(f"--- cargorewind: rerun 2 {index} ---")
        return RunResult(124, result.output[:cut], timed_out=True)


def test_reruns_the_run_timeout_stopped_are_left_out_of_the_flaky_check(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    repo = make_repo("origin")
    base, fix = _crate(repo, lockfile=True)
    lines: list[str] = []
    out = tmp_path / "out"
    options = RewindOptions(str(repo.path), fix, out, tmp_path / "work", base=base)
    report = rewind(options, SubprocessRunner(), StoppedReruns(PASSING), lines.append)
    assert report.flip is not None and report.flip.verified and report.flip.flaky == []
    task = json.loads((out / "task.json").read_text())
    assert task["FAIL_TO_PASS"] == ["tests::two"] and task["PASS_TO_PASS"] == ["tests::zero"]
    assert task["reruns"]["stages"]["after"]["timed_out"] is True
    assert task["reruns"]["stages"]["after"]["changed"] == 0
    assert task["tests"]["tests::two"]["reruns"] == {
        "before": ["failed"] * 3,
        "after": ["passed"],  # rounds 2 and 3 never started
    }
    assert (
        "rerun     after  4 of 6 rerun(s) did not run their test (the run hit --timeout); "
        "they are left out of the flaky check"
    ) in lines


class SlowBuild(ScriptedBackend):
    """The first after rerun spends its whole --test-timeout compiling."""

    def run_script(self, tag: str, run: str, overlay: Overlay, script: str) -> RunResult:
        result = super().run_script(tag, run, overlay, script)
        if run != "rerun-after":
            return result
        _, rest = result.output.split("--- cargorewind: rerun 1 1 ---", 1)
        building = "--- cargorewind: rerun 1 0 ---\n   Compiling demo v0.1.0\n"
        stopped = building + "--- cargorewind: exit 124 ---\n"
        return RunResult(0, stopped + "--- cargorewind: rerun 1 1 ---" + rest)


def test_a_rerun_still_building_at_its_test_timeout_is_left_out(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    repo = make_repo("origin")
    base, fix = _crate(repo, lockfile=True)
    lines: list[str] = []
    options = RewindOptions(str(repo.path), fix, tmp_path / "out", tmp_path / "work", base=base)
    report = rewind(options, SubprocessRunner(), SlowBuild(PASSING), lines.append)
    assert report.flip is not None and report.flip.verified and report.flip.flaky == []
    assert report.flip.reruns["tests::two"]["after"] == [Status.PASSED] * 2
    assert (
        "rerun     after  1 of 6 rerun(s) did not run their test (still building); "
        "they are left out of the flaky check"
    ) in lines

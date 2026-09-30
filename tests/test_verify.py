from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from cargorewind.backend import ReplayBackend
from cargorewind.bundle import BundleError, sha256_file
from cargorewind.rewind import RewindOptions, rewind
from cargorewind.runner import SubprocessRunner
from cargorewind.verify import VerifyOptions, VerifyReport, verify
from tests.conftest import GitRepo
from tests.test_deps import INDEX, FakeCargo
from tests.test_rewind import (
    DEMO,
    DEMO_FIX,
    FIXED,
    PASSING,
    CargoModelSession,
    ScriptedBackend,
    _crate,
    _home_crate,
)

FAILING = {**PASSING, "after": (101, "test tests::zero ... ok\ntest tests::two ... FAILED\n")}


def _bundle(make_repo: Callable[[str], GitRepo], tmp_path: Path) -> Path:
    """A bundle of the synthetic crate, written by rewind with the scripted backend."""
    repo = make_repo("origin")
    base, fix = _crate(repo, lockfile=True)
    out = tmp_path / "bundle"
    options = RewindOptions(str(repo.path), fix, out, tmp_path / "work", base=base)
    rewind(options, SubprocessRunner(), ScriptedBackend(PASSING), lambda _: None)
    return out


def _verify(
    bundle: Path, tmp_path: Path, backend: Any, **kwargs: Any
) -> tuple[VerifyReport, list[str]]:
    lines: list[str] = []
    options = VerifyOptions(bundle, bundle / "verify", tmp_path / "verify-work", **kwargs)
    return verify(options, SubprocessRunner(), backend, lines.append), lines


def test_verify_rebuilds_the_task_from_the_bundle_alone(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    bundle = _bundle(make_repo, tmp_path)
    backend = ScriptedBackend(PASSING)  # its build() asserts the base tree is in the context
    report, lines = _verify(bundle, tmp_path, backend)

    assert report.verified
    assert [c.name for c in report.checks] == [
        "files",
        "recipe",
        "dockerfile",
        "probes",
        "FAIL_TO_PASS",
        "PASS_TO_PASS",
    ]
    assert all(c.ok for c in report.checks)
    assert backend.dockerfile == (bundle / "Dockerfile").read_text()
    assert backend.overlays["after"].files["src/lib.rs"].decode() == FIXED
    before = backend.overlays["before"].files["src/lib.rs"].decode()
    assert "fn two()" in before and "x * 3" in before
    assert backend.probes == {"base": (), "before": ("two",), "after": ("two",)}
    assert sorted(backend.scripts) == ["rerun-after", "rerun-before"]
    task = json.loads((bundle / "task.json").read_text())
    assert backend.tags["after"] == task["image_tag"]
    document = json.loads((bundle / "verify" / "verify.json").read_text())
    assert document["verified"] is True
    assert document["FAIL_TO_PASS"] == document["expected"]["FAIL_TO_PASS"] == ["tests::two"]
    assert document["checks"][0] == {
        "name": "files",
        "ok": True,
        "detail": f"{len(task['files'])} file(s) match their sha256 in task.json",
    }
    assert document["task"]["recipe"] == task["recipe"]["hash"]
    assert document["runs"]["before"]["failed"] == 1
    assert document["tests"]["tests::two"]["statuses"]["after"] == "passed"
    assert (bundle / "verify" / "logs" / "rerun-after.log").exists()
    assert any(line.startswith("base      base.bundle: tree ") for line in lines)
    assert "check     FAIL_TO_PASS: ok (1 test(s), as recorded)" in lines
    assert "check     PASS_TO_PASS: ok (1 test(s), as recorded)" in lines
    # The rounds and timeout come from the bundle unless overridden.
    assert report.rerun_rounds == 3 and report.test_timeout == 300
    again, _ = _verify(bundle, tmp_path, ScriptedBackend(PASSING), reruns=0, test_timeout=5)
    assert again.rerun_rounds == 0 and again.reruns == {} and again.verified


def test_verify_reports_a_flip_that_no_longer_holds(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    bundle = _bundle(make_repo, tmp_path)
    report, lines = _verify(bundle, tmp_path, ScriptedBackend(FAILING))
    assert not report.verified
    assert {c.name: c.detail for c in report.failed} == {
        "FAIL_TO_PASS": "recorded but not found now: tests::two",
    }
    assert report.flip is not None and report.flip.still_failing == ["tests::two"]
    document = json.loads((bundle / "verify" / "verify.json").read_text())
    assert document["verified"] is False
    assert "check     FAIL_TO_PASS: FAILED (recorded but not found now: tests::two)" in lines


def test_verify_reports_lists_that_differ_from_the_record(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    bundle = _bundle(make_repo, tmp_path)
    task_path = bundle / "task.json"
    task = json.loads(task_path.read_text())
    task["FAIL_TO_PASS"] = ["tests::two", "tests::three"]
    task["PASS_TO_PASS"] = []
    task_path.write_text(json.dumps(task))
    report, _ = _verify(bundle, tmp_path, ScriptedBackend(PASSING))
    assert not report.verified and report.flip is not None and report.flip.verified
    assert {c.name: c.detail for c in report.failed} == {
        "FAIL_TO_PASS": "recorded but not found now: tests::three",
        "PASS_TO_PASS": "found now but not recorded: tests::zero",
    }


@pytest.mark.parametrize(
    ("tamper", "check"),
    [
        ("fix.patch", "files"),
        ("Dockerfile", "dockerfile"),
        ("recipe.json", "recipe, dockerfile"),
    ],
)
def test_verify_refuses_an_inconsistent_bundle(
    make_repo: Callable[[str], GitRepo], tmp_path: Path, tamper: str, check: str
) -> None:
    bundle = _bundle(make_repo, tmp_path)
    task_path = bundle / "task.json"
    task = json.loads(task_path.read_text())
    target = bundle / tamper
    if tamper == "recipe.json":
        recipe = json.loads(target.read_text())
        recipe["toolchain"] = "1.71.0"
        text = json.dumps(recipe)
    else:
        text = target.read_text() + "# tampered\n"
    target.write_text(text)
    # Keep the manifest honest for the other checks, so each check fails on its own.
    task["files"][tamper] = sha256_file(target)
    task_path.write_text(json.dumps(task))
    if tamper == "fix.patch":  # the manifest cannot be kept: task.json is the record
        task["files"][tamper] = "0" * 64
        task_path.write_text(json.dumps(task))
    backend = ScriptedBackend(PASSING)
    with pytest.raises(BundleError, match=f"inconsistent \\({check}\\)"):
        _verify(bundle, tmp_path, backend)
    assert backend.builds == []


def test_verify_checks_the_base_tree_and_the_patches(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    bundle = _bundle(make_repo, tmp_path)
    task_path = bundle / "task.json"
    task = json.loads(task_path.read_text())
    wrong = dict(task, base_tree={**task["base_tree"], "tree": "3" * 40})
    task_path.write_text(json.dumps(wrong))
    with pytest.raises(BundleError, match="is not the base commit's tree"):
        _verify(bundle, tmp_path, ScriptedBackend(PASSING))
    missing = dict(task, base_tree={**task["base_tree"], "commit": "4" * 40})
    task_path.write_text(json.dumps(missing))
    with pytest.raises(BundleError, match=r"base\.bundle: unknown commit"):
        _verify(bundle, tmp_path, ScriptedBackend(PASSING))


def test_verify_replays_the_recorded_strsim_run(tmp_path: Path) -> None:
    """The transcript of the live run answers the verify's builds and runs too: the
    Dockerfile, the overlays and the scripts are byte-identical to the rewind's."""
    out = tmp_path / "demo"
    options = RewindOptions(str(DEMO / "strsim-rs.bundle"), DEMO_FIX, out, tmp_path / "work")
    rewind(options, SubprocessRunner(), ReplayBackend(DEMO / "transcript.json"), lambda _: None)
    report, lines = _verify(out, tmp_path, ReplayBackend(DEMO / "transcript.json"))
    assert report.verified
    assert report.flip is not None and len(report.flip.pass_to_pass) == 102
    assert "check     files: ok (17 file(s) match their sha256 in task.json)" in lines
    assert "check     dockerfile: ok (the bundle's Dockerfile is what recipe.json renders)" in lines
    assert "probe     in Docker: build passed, before passed, after passed" in lines


def test_verify_wraps_unreadable_or_unusable_bundle_files(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    bundle = _bundle(make_repo, tmp_path)
    recipe = bundle / "recipe.json"
    text = recipe.read_text()
    recipe.write_text("[]")
    with pytest.raises(BundleError, match=r"recipe\.json: not a JSON object"):
        _verify(bundle, tmp_path, ScriptedBackend(PASSING))
    recipe.write_text(text.replace('"schema": 1', '"schema": 9'))
    with pytest.raises(BundleError, match="unsupported recipe schema 9"):
        _verify(bundle, tmp_path, ScriptedBackend(PASSING))
    recipe.unlink()
    with pytest.raises(BundleError, match=r"recipe\.json: cannot read"):
        _verify(bundle, tmp_path, ScriptedBackend(PASSING))
    recipe.write_text(text)
    task_path = bundle / "task.json"
    task = json.loads(task_path.read_text())
    # The recipe greps for "two" but task.json claims the fix defines it: the host
    # check finds it defined in the before tree already and refuses to build.
    task["probes"]["identifiers"][0]["patch"] = "fix"
    task_path.write_text(json.dumps(task))
    backend = ScriptedBackend(PASSING)
    with pytest.raises(BundleError, match="sanity probe failed on the host"):
        _verify(bundle, tmp_path, backend)
    assert backend.builds == []
    # A test patch that no longer applies to the base tree.
    task["probes"]["identifiers"][0]["patch"] = "test"
    patch = bundle / "test.patch"
    patch.write_text(patch.read_text().replace("x * 3", "x * 4"))
    task["files"]["test.patch"] = sha256_file(patch)
    task_path.write_text(json.dumps(task))
    with pytest.raises(BundleError, match="patches do not apply to the base tree"):
        _verify(bundle, tmp_path, backend)


def test_verify_copies_a_bounded_lockfile_into_the_context(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    repo = make_repo("origin")
    _, fix = _home_crate(repo)
    session = CargoModelSession(FakeCargo(INDEX, {"demo": [("home", "0.5.4")]}))
    out = tmp_path / "bundle"
    options = RewindOptions(str(repo.path), fix, out, tmp_path / "work", index=INDEX, vendor=True)
    rewind(options, SubprocessRunner(), ScriptedBackend(PASSING, session), lambda _: None)
    backend = ScriptedBackend(PASSING)  # no session: verify never runs the pin loop
    report, _ = _verify(out, tmp_path, backend)
    assert report.verified
    assert [c.name for c in report.checks][:5] == [
        "files",
        "recipe",
        "dockerfile",
        "lockfile",
        "probes",
    ]
    assert backend.builds == [(report.task.image_tag, None, True)]  # Cargo.lock in the context
    assert backend.commands["after"][-1] == "--offline"
    lock = out / "Cargo.lock"
    lock.write_text(lock.read_text() + "\n# edited\n")
    task_path = out / "task.json"
    task = json.loads(task_path.read_text())
    task["files"]["Cargo.lock"] = sha256_file(lock)
    task_path.write_text(json.dumps(task))
    with pytest.raises(BundleError, match=r"inconsistent \(lockfile\)"):
        _verify(out, tmp_path, ScriptedBackend(PASSING))

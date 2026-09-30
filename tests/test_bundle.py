from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from cargorewind.bundle import (
    BUNDLE_FILES,
    TASK_SCHEMA,
    BundleError,
    Task,
    check_manifest,
    manifest,
    read_task,
    read_test_list,
    schema,
    sha256_file,
    validate,
    write_task,
    write_test_lists,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def sample_task(**changes: Any) -> dict[str, Any]:
    """A minimal valid task.json document (a crate with one flipped unit test)."""
    document: dict[str, Any] = {
        "schema_version": TASK_SCHEMA,
        "generator": "cargorewind 0.1.0",
        "repo": "examples/demo.bundle",
        "base_commit": "b" * 40,
        "fix_commit": "f" * 40,
        "commit_date": "2024-01-11T12:00:00+00:00",
        "toolchain": {
            "version": "1.70.0",
            "source": "rust-toolchain",
            "reason": "pinned",
            "image_version": "1.70.0",
            "rustup_install": False,
            "components": [],
            "targets": [],
            "profile": None,
            "toolchain_file": "rust-toolchain",
            "decisions": [{"step": "file", "outcome": "1.70", "reason": "rust-toolchain"}],
        },
        "image": "rust:1.70.0-slim@sha256:" + "d" * 64,
        "image_source": {"source": "offline-table", "reason": "offline digest table"},
        "lockfile": "committed",
        "lock_report": "lock.json",
        "vendored": False,
        "test_command": "cargo test --no-fail-fast",
        "recipe": {"hash": "e" * 64, "report": "recipe.json"},
        "image_tag": "cargorewind/demo:" + "e" * 16,
        "build_cache": {"status": "off", "reason": "no build cache"},
        "probes": {
            "identifiers": [
                {"kind": "fn", "name": "two", "patch": "test", "path": "src/lib.rs", "line": 10}
            ],
            "skipped": 0,
            "container_checks": {"build": "passed", "before": "passed", "after": "passed"},
            "ok": True,
            "report": "probes.json",
        },
        "split": {
            "test_files": ["src/lib.rs"],
            "fix_files": ["src/lib.rs"],
            "shared_files": ["src/lib.rs"],
            "cfg_test_hunks": [
                {
                    "path": "src/lib.rs",
                    "old_start": 1,
                    "new_start": 1,
                    "test_lines": 5,
                    "fix_lines": 2,
                }
            ],
            "notes": [],
            "report": "split.json",
        },
        "base_tree": {"file": "base.bundle", "commit": "1" * 40, "tree": "2" * 40},
        "toolchain_report": "toolchain.json",
        "runs": {
            "base": {
                "exit_code": 0,
                "timed_out": False,
                "state": "ran",
                "passed": 1,
                "failed": 0,
                "ignored": 0,
            },
            "before": {
                "exit_code": 101,
                "timed_out": False,
                "state": "ran",
                "passed": 1,
                "failed": 1,
                "ignored": 0,
            },
            "after": {
                "exit_code": 0,
                "timed_out": False,
                "state": "ran",
                "passed": 2,
                "failed": 0,
                "ignored": 0,
            },
        },
        "reruns": {
            "rounds": 3,
            "test_timeout": 300,
            "stages": {
                "after": {
                    "tests": 2,
                    "exit_code": 0,
                    "timed_out": False,
                    "changed": 0,
                    "log": "logs/rerun-after.log",
                }
            },
        },
        "tests": {
            "tests::two": {
                "target": "lib",
                "name": "tests::two",
                "command": "cargo test --lib -- --exact tests::two",
                "statuses": {"base": "missing", "before": "failed", "after": "passed"},
                "reruns": {"after": ["passed", "passed", "passed"]},
            }
        },
        "FAIL_TO_PASS": ["tests::two"],
        "PASS_TO_PASS": ["tests::zero"],
        "regressions": [],
        "still_failing": [],
        "flaky": [],
        "verified": True,
        "files": {name: "0" * 64 for name in BUNDLE_FILES},
    }
    document.update(changes)
    return document


def test_the_committed_schema_is_a_valid_2020_12_schema() -> None:
    document = schema()
    jsonschema.Draft202012Validator.check_schema(document)
    assert document["properties"]["schema_version"] == {"const": TASK_SCHEMA}
    committed = REPO_ROOT / "src" / "cargorewind" / "schemas" / "task.schema.json"
    assert json.loads(committed.read_text()) == document


def test_a_task_round_trips_through_the_dataclasses_and_the_schema(tmp_path: Path) -> None:
    document = sample_task()
    validate(document)
    task = Task.from_dict(document)
    assert task.fail_to_pass == ["tests::two"] and task.probes.identifiers[0].line == 10
    assert task.tests["tests::two"].statuses["before"] == "failed"
    assert task.runs["before"].failed == 1 and task.reruns.stages["after"].changed == 0
    assert task.to_dict() == document
    path = tmp_path / "task.json"
    assert write_task(path, task) == document
    assert read_task(path) == task
    assert json.loads(path.read_text())["FAIL_TO_PASS"] == ["tests::two"]


@pytest.mark.parametrize(
    ("change", "where"),
    [
        ({"schema_version": 1}, "schema_version"),
        ({"FAIL_TO_PASS": ["a", "a"]}, "FAIL_TO_PASS: ['a', 'a'] has non-unique elements"),
        ({"FAIL_TO_PASS": "tests::two"}, "FAIL_TO_PASS"),
        ({"base_commit": "abc"}, "base_commit"),
        ({"lockfile": "sometimes"}, "lockfile"),
        ({"verified": "yes"}, "verified"),
        ({"unexpected": 1}, "(root)"),
        ({"runs": {"base": {"exit_code": 0}}}, "runs"),
        ({"reruns": {"rounds": -1, "test_timeout": 300, "stages": {}}}, "reruns/rounds"),
        ({"recipe": {"hash": "short", "report": "recipe.json"}}, "recipe/hash"),
        ({"files": {"Dockerfile": "0" * 64}}, "files"),
    ],
)
def test_schema_violations_are_refused_on_write_and_read(
    tmp_path: Path, change: dict[str, Any], where: str
) -> None:
    document = sample_task(**change)
    with pytest.raises(BundleError, match="does not match task schema 2") as excinfo:
        validate(document)
    assert where in str(excinfo.value)
    path = tmp_path / "task.json"
    path.write_text(json.dumps(document))
    with pytest.raises(BundleError):
        read_task(path)
    assert not (tmp_path / "written.json").exists()


def test_tests_statuses_must_be_known_statuses() -> None:
    row = sample_task()["tests"]["tests::two"]
    bad = {**row, "statuses": {**row["statuses"], "after": "green"}}
    with pytest.raises(BundleError, match="tests/tests::two/statuses/after"):
        validate(sample_task(tests={"tests::two": bad}))


def test_read_task_reports_unreadable_files_and_old_schemas(tmp_path: Path) -> None:
    with pytest.raises(BundleError, match="cannot read"):
        read_task(tmp_path / "missing.json")
    broken = tmp_path / "broken.json"
    broken.write_text("{")
    with pytest.raises(BundleError, match="cannot read"):
        read_task(broken)
    listed = tmp_path / "list.json"
    listed.write_text("[]")
    with pytest.raises(BundleError, match="not a JSON object"):
        read_task(listed)
    old = tmp_path / "old.json"
    old.write_text(json.dumps({"schema_version": 1, "repo": "x"}))
    with pytest.raises(BundleError, match="schema_version 1 is not 2"):
        read_task(old)


def test_many_violations_are_truncated() -> None:
    broken = {"exit_code": "x", "timed_out": 1, "state": 2, "passed": -1, "failed": -1}
    document = sample_task(runs={stage: broken for stage in ("base", "before", "after")})
    with pytest.raises(BundleError, match=r"and \d+ more") as excinfo:
        validate(document)
    assert str(excinfo.value).count("\n") == 8


def test_test_lists_and_the_manifest(tmp_path: Path) -> None:
    write_test_lists(tmp_path, ["a::b", "c"], [])
    assert (tmp_path / "fail_to_pass.txt").read_text() == "a::b\nc\n"
    assert (tmp_path / "pass_to_pass.txt").read_text() == ""
    assert read_test_list(tmp_path / "fail_to_pass.txt") == ["a::b", "c"]
    assert read_test_list(tmp_path / "pass_to_pass.txt") == []
    (tmp_path / "Dockerfile").write_text("FROM x\n")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "after.log").write_text("ok\n")
    (tmp_path / "logs" / "notes.txt").write_text("not a log\n")
    (tmp_path / "task.json").write_text("{}")
    files = manifest(tmp_path)
    assert sorted(files) == ["Dockerfile", "fail_to_pass.txt", "logs/after.log", "pass_to_pass.txt"]
    assert files["Dockerfile"] == sha256_file(tmp_path / "Dockerfile")
    assert check_manifest(tmp_path, files) == []
    (tmp_path / "Dockerfile").write_text("FROM y\n")
    (tmp_path / "logs" / "after.log").unlink()
    assert check_manifest(tmp_path, files) == [
        "Dockerfile: sha256 differs from task.json",
        "logs/after.log: missing",
    ]

"""The task bundle: a versioned ``task.json`` and the files that make it self-contained.

``task.json`` is typed twice. The dataclasses here are what the pipeline fills and what
``verify`` reads back; ``schemas/task.schema.json`` (JSON Schema 2020-12, committed and
packaged) is what any other tool can check a bundle against. Both are enforced: the
document is validated against the schema before it is written and after it is read,
so a bundle that does not match its declared schema is refused, never half-read.

Bundle layout (``BUNDLE_FILES`` plus the run logs)::

    task.json          this document; its ``files`` map is the sha256 of every other file
    Dockerfile         the environment (rendered from recipe.json)
    test.patch         the test side of the fix commit
    fix.patch          the code side
    fail_to_pass.txt   one test id per line, the FAIL_TO_PASS list
    pass_to_pass.txt   one test id per line, the PASS_TO_PASS list
    base.bundle        a git bundle with one root commit: the tree of the base commit
    split.json, toolchain.json, lock.json, probes.json, recipe.json
    Cargo.lock         only when cargorewind bounded the lockfile by the commit date
    logs/*.log         the build, the three stage runs and the reruns

``base.bundle`` is what makes ``cargorewind verify`` work from the bundle alone: the
root commit has exactly the base commit's tree (the tree id is recorded and checked),
so no clone of the original repository is needed.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import types
import typing
from dataclasses import dataclass, field, fields
from importlib import resources
from pathlib import Path
from typing import Any

import jsonschema

from cargorewind import __version__

TASK_SCHEMA = 2
SCHEMA_FILE = "task.schema.json"
BUNDLE_FILES: tuple[str, ...] = (
    "Dockerfile",
    "test.patch",
    "fix.patch",
    "fail_to_pass.txt",
    "pass_to_pass.txt",
    "base.bundle",
    "split.json",
    "toolchain.json",
    "lock.json",
    "probes.json",
    "recipe.json",
)
BASE_BUNDLE = "base.bundle"
BUNDLE_REF = "refs/heads/cargorewind-bundle"


class BundleError(ValueError):
    """A bundle file is missing, does not match its schema or its recorded digest."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# The document, typed


@dataclass(frozen=True)
class ImageSource:
    source: str
    reason: str


@dataclass(frozen=True)
class RecipeRef:
    hash: str
    report: str = "recipe.json"


@dataclass(frozen=True)
class BuildCacheInfo:
    status: str  # hit, miss or off
    reason: str


@dataclass(frozen=True)
class ProbeIdentifier:
    kind: str
    name: str
    patch: str
    path: str
    line: int


@dataclass(frozen=True)
class ProbeSummary:
    identifiers: list[ProbeIdentifier]
    skipped: int
    container_checks: dict[str, str]
    ok: bool
    report: str = "probes.json"


@dataclass(frozen=True)
class CfgTestHunk:
    path: str
    old_start: int
    new_start: int
    test_lines: int
    fix_lines: int


@dataclass(frozen=True)
class SplitSummary:
    test_files: list[str]
    fix_files: list[str]
    shared_files: list[str]
    cfg_test_hunks: list[CfgTestHunk]
    notes: list[str]
    report: str = "split.json"


@dataclass(frozen=True)
class BaseTree:
    """Where the base commit's tree is in the bundle and how to check it."""

    file: str  # base.bundle
    commit: str  # the root commit inside the bundle
    tree: str  # its tree id, equal to the base commit's tree id


@dataclass(frozen=True)
class RunSummary:
    exit_code: int
    timed_out: bool
    state: str
    passed: int
    failed: int
    ignored: int


@dataclass(frozen=True)
class RerunSummary:
    tests: int
    exit_code: int
    timed_out: bool
    changed: int
    log: str


@dataclass(frozen=True)
class RerunSection:
    rounds: int
    test_timeout: int
    stages: dict[str, RerunSummary]


@dataclass(frozen=True)
class TestRow:
    target: str
    name: str
    command: str
    statuses: dict[str, str]  # stage -> status
    reruns: dict[str, list[str]]  # stage -> the status of every round


@dataclass(frozen=True)
class FlakyTest:
    id: str
    reason: str


@dataclass(frozen=True)
class Task:
    """``task.json``: what the environment is, what ran and what the flip is."""

    repo: str
    base_commit: str
    fix_commit: str
    commit_date: str
    toolchain: dict[str, Any]
    image: str
    image_source: ImageSource
    lockfile: str
    vendored: bool
    test_command: str
    recipe: RecipeRef
    image_tag: str
    build_cache: BuildCacheInfo
    probes: ProbeSummary
    split: SplitSummary
    base_tree: BaseTree
    runs: dict[str, RunSummary]
    reruns: RerunSection
    tests: dict[str, TestRow]
    fail_to_pass: list[str] = field(metadata={"key": "FAIL_TO_PASS"})
    pass_to_pass: list[str] = field(metadata={"key": "PASS_TO_PASS"})
    regressions: list[str]
    still_failing: list[str]
    flaky: list[FlakyTest]
    verified: bool
    files: dict[str, str]  # bundle path -> sha256
    schema_version: int = TASK_SCHEMA
    generator: str = f"cargorewind {__version__}"
    lock_report: str = "lock.json"
    toolchain_report: str = "toolchain.json"

    def to_dict(self) -> dict[str, Any]:
        document: dict[str, Any] = _dump(self)
        return document

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Task:
        """Read a validated document; ``validate`` first, or ``read_task`` does both."""
        return _load(cls, data)


# dataclass <-> JSON, driven by the field types


def _key(spec: dataclasses.Field[Any]) -> str:
    return str(spec.metadata.get("key", spec.name))


def _dump(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {_key(spec): _dump(getattr(value, spec.name)) for spec in fields(value)}
    if isinstance(value, dict):
        return {k: _dump(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_dump(v) for v in value]
    return value


def _load[T](cls: type[T], data: Any) -> T:
    hints = typing.get_type_hints(cls)
    values: dict[str, Any] = {}
    for spec in fields(cls):  # type: ignore[arg-type]
        key = _key(spec)
        if key in data:
            values[spec.name] = _convert(hints[spec.name], data[key])
    return cls(**values)


def _convert(hint: Any, value: Any) -> Any:
    if dataclasses.is_dataclass(hint) and isinstance(hint, type):
        return _load(hint, value)
    origin = typing.get_origin(hint)
    if origin is types.UnionType:  # ``X | None`` fields
        return value
    if origin is list:
        (item,) = typing.get_args(hint)
        return [_convert(item, v) for v in value]
    if origin is dict:
        _, item = typing.get_args(hint)
        return {k: _convert(item, v) for k, v in value.items()}
    return value


# The schema


def schema() -> dict[str, Any]:
    text = resources.files("cargorewind").joinpath("schemas", SCHEMA_FILE).read_text()
    data: dict[str, Any] = json.loads(text)
    return data


def validate(document: dict[str, Any], what: str = "task.json") -> None:
    """Raise ``BundleError`` with every schema violation when ``document`` is not a task."""
    validator = jsonschema.Draft202012Validator(schema())
    errors = sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path))
    if errors:
        where = [
            f"{'/'.join(str(p) for p in e.absolute_path) or '(root)'}: {e.message}"
            for e in errors[:8]
        ]
        more = f" and {len(errors) - 8} more" if len(errors) > 8 else ""
        listed = "\n  ".join(where)
        raise BundleError(f"{what} does not match task schema {TASK_SCHEMA}{more}:\n  {listed}")


def write_task(path: Path, task: Task) -> dict[str, Any]:
    """Validate ``task`` against the schema and write it as ``path``."""
    document = task.to_dict()
    validate(document, str(path))
    path.write_text(json.dumps(document, indent=2) + "\n")
    return document


def read_task(path: Path) -> Task:
    """Read and validate a ``task.json``."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise BundleError(f"{path}: cannot read: {exc}") from exc
    if not isinstance(data, dict):
        raise BundleError(f"{path}: not a JSON object")
    if data.get("schema_version") != TASK_SCHEMA:
        raise BundleError(
            f"{path}: schema_version {data.get('schema_version')!r} is not {TASK_SCHEMA} "
            f"(this cargorewind reads task schema {TASK_SCHEMA} only)"
        )
    validate(data, str(path))
    return Task.from_dict(data)


# The files next to task.json


def write_test_lists(out: Path, fail_to_pass: list[str], pass_to_pass: list[str]) -> None:
    for name, ids in (("fail_to_pass.txt", fail_to_pass), ("pass_to_pass.txt", pass_to_pass)):
        (out / name).write_text("".join(f"{test_id}\n" for test_id in ids))


def read_test_list(path: Path) -> list[str]:
    return [line for line in path.read_text().splitlines() if line]


def clear_bundle(out: Path) -> None:
    """Remove every file a bundle may hold (and an earlier verify run) from ``out``.

    Bundles are written in place and ``manifest`` hashes what exists, so a directory
    reused by another task (a batch retry after an error, a second run into the same
    ``--out``) must not keep that task's ``Cargo.lock``, logs or verify result. Other
    files in ``out`` are left alone.
    """
    for name in (*BUNDLE_FILES, "Cargo.lock", "task.json", "verify/verify.json"):
        (out / name).unlink(missing_ok=True)
    for logs in (out / "logs", out / "verify" / "logs"):
        if logs.is_dir():
            for path in logs.iterdir():
                if path.suffix == ".log" and path.is_file():
                    path.unlink()


def manifest(out: Path) -> dict[str, str]:
    """sha256 of every bundle file that exists (``task.json`` itself excluded)."""
    names = [*BUNDLE_FILES, "Cargo.lock"]
    logs = out / "logs"
    if logs.is_dir():
        names += sorted(f"logs/{p.name}" for p in logs.iterdir() if p.suffix == ".log")
    return {name: sha256_file(out / name) for name in names if (out / name).is_file()}


def check_manifest(out: Path, files: dict[str, str]) -> list[str]:
    """The bundle files whose digest differs from ``files`` or which are missing."""
    problems = []
    for name, digest in sorted(files.items()):
        path = out / name
        if not path.is_file():
            problems.append(f"{name}: missing")
        elif sha256_file(path) != digest:
            problems.append(f"{name}: sha256 differs from task.json")
    return problems

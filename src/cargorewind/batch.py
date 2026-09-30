"""``cargorewind batch``: several rewinds from one TOML file, deduplicated and summarized.

The recipes file lists tasks (``[[task]]`` tables: ``repo`` and ``fix``, optionally
``base``, ``name``, ``vendor``, ``image``, ``index_dir``, ``replay``, ``reruns`` and
``test_timeout``) and ``[defaults]`` that every task inherits. Local paths are relative
to the file's directory; URLs are used as they are. Two tasks are the same task when
their repository slug and resolved fix commit agree, whatever the spelling of the
repository or the length of the SHA: once one of them has run to a verdict, the others
are skipped and reported as duplicates (after an error, the next spelling runs). Every
task gets its own bundle directory under the batch output directory (its ``name``, or
``<slug>-<fix12>``; a name that a task with a verdict holds is an error), and the
batch writes ``summary.json`` and ``summary.md`` next to them.
"""

from __future__ import annotations

import hashlib
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from cargorewind import __version__
from cargorewind.backend import Backend
from cargorewind.buildcache import BuildCache
from cargorewind.crateindex import CrateIndex, DirectoryIndex
from cargorewind.gitops import open_checkout, repo_slug
from cargorewind.registry import ImageResolver
from cargorewind.rewind import (
    DEFAULT_RERUNS,
    DEFAULT_TEST_TIMEOUT,
    RewindOptions,
    RewindReport,
    rewind,
)
from cargorewind.runner import Runner

SUMMARY_SCHEMA = 1
Log = Callable[[str], None]
BackendFactory = Callable[["BatchTask"], Backend]

_TASK_KEYS = frozenset(
    {"repo", "fix", "base", "name", "vendor", "image", "index_dir", "replay", "reruns"}
    | {"test_timeout"}
)
_DEFAULT_KEYS = frozenset({"vendor", "reruns", "test_timeout", "index_dir", "image"})


class BatchError(ValueError):
    """The recipes file cannot be used."""


@dataclass(frozen=True)
class BatchTask:
    repo: str
    fix: str
    base: str | None = None
    name: str | None = None  # bundle directory name; default <slug>-<fix12>
    vendor: bool = False
    image: str | None = None
    index_dir: Path | None = None
    replay: Path | None = None
    reruns: int | None = None
    test_timeout: int | None = None

    @property
    def slug(self) -> str:
        return repo_slug(self.repo)

    @property
    def checkout(self) -> str:
        """Checkout directory name: the slug and a digest of the source, so two
        repositories that share a name (forks) never fetch into one checkout."""
        return f"{self.slug}-{hashlib.sha256(self.repo.encode()).hexdigest()[:8]}"


@dataclass(frozen=True)
class BatchOptions:
    out: Path
    workdir: Path  # one checkout per repository source below it (BatchTask.checkout)
    reruns: int = DEFAULT_RERUNS
    test_timeout: int = DEFAULT_TEST_TIMEOUT
    resolver: ImageResolver | None = None
    cache: BuildCache | None = None
    rebuild: bool = False
    cache_dir: Path | None = None
    # Exceptions that end one task with status "error" and let the batch go on; any
    # other exception stops the batch.
    failures: tuple[type[Exception], ...] = (Exception,)


@dataclass
class BatchResult:
    task: BatchTask
    status: str  # verified, not-verified, error or duplicate
    seconds: float = 0.0
    out: Path | None = None
    base_commit: str = ""
    fix_commit: str = ""
    toolchain: str = ""
    lockfile: str = ""  # committed, generated or date-bounded
    fail_to_pass: int = 0
    pass_to_pass: int = 0
    flaky: int = 0
    detail: str = ""
    lines: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return self.task.name or f"{self.task.slug}-{(self.fix_commit or self.task.fix)[:12]}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.label,
            "repo": self.task.repo,
            "fix": self.task.fix,
            "base_commit": self.base_commit,
            "fix_commit": self.fix_commit,
            "status": self.status,
            "seconds": self.seconds,
            "bundle": str(self.out) if self.out else None,
            "toolchain": self.toolchain,
            "lockfile": self.lockfile,
            "FAIL_TO_PASS": self.fail_to_pass,
            "PASS_TO_PASS": self.pass_to_pass,
            "flaky": self.flaky,
            "detail": self.detail,
        }


def _local(base: Path, value: str) -> Path:
    """``value`` relative to the recipes file's directory, spelled relative to the working
    directory when it lies below it (so logs, task.json and the summary stay portable)."""
    path = Path(value) if Path(value).is_absolute() else base / value
    try:
        return path.relative_to(Path.cwd())
    except ValueError:
        return path


def _path(base: Path, value: Any, key: str) -> Path:
    if not isinstance(value, str) or not value:
        raise BatchError(f"{key} must be a non-empty string")
    return _local(base, value)


def _source(base: Path, value: str) -> str:
    """A URL stays a URL; a local path is relative to the recipes file."""
    if "://" in value or value.startswith("git@"):
        return value
    return str(_local(base, value))


def _int(value: Any, key: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise BatchError(f"{key} must be an integer >= {minimum}")
    return value


def _task(base: Path, index: int, raw: dict[str, Any]) -> BatchTask:
    unknown = sorted(set(raw) - _TASK_KEYS)
    if unknown:
        raise BatchError(f"task {index}: unknown key(s) {', '.join(unknown)}")
    for key in ("repo", "fix"):
        if not isinstance(raw.get(key), str) or not raw[key]:
            raise BatchError(f"task {index}: {key} must be a non-empty string")
    for key in ("base", "name", "image"):
        if key in raw and (not isinstance(raw[key], str) or not raw[key]):
            raise BatchError(f"task {index}: {key} must be a non-empty string")
    name = raw.get("name")
    if name is not None and (Path(name).name != name or name in (".", "..")):
        raise BatchError(f"task {index}: name {name!r} must be a plain directory name")
    if "vendor" in raw and not isinstance(raw["vendor"], bool):
        raise BatchError(f"task {index}: vendor must be true or false")
    return BatchTask(
        repo=_source(base, raw["repo"]),
        fix=raw["fix"],
        base=raw.get("base"),
        name=name,
        vendor=bool(raw.get("vendor", False)),
        image=raw.get("image"),
        index_dir=_path(base, raw["index_dir"], f"task {index}: index_dir")
        if "index_dir" in raw
        else None,
        replay=_path(base, raw["replay"], f"task {index}: replay") if "replay" in raw else None,
        reruns=_int(raw["reruns"], f"task {index}: reruns", 0) if "reruns" in raw else None,
        test_timeout=_int(raw["test_timeout"], f"task {index}: test_timeout", 1)
        if "test_timeout" in raw
        else None,
    )


def load_recipes(path: Path) -> list[BatchTask]:
    """Parse a recipes file into tasks, with ``[defaults]`` folded into each task."""
    try:
        data = tomllib.loads(path.read_text())
    except OSError as exc:
        raise BatchError(f"{path}: cannot read: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise BatchError(f"{path}: not valid TOML: {exc}") from exc
    defaults = data.get("defaults", {})
    if not isinstance(defaults, dict):
        raise BatchError(f"{path}: [defaults] must be a table")
    unknown = sorted(set(defaults) - _DEFAULT_KEYS)
    if unknown:
        raise BatchError(f"{path}: unknown default(s) {', '.join(unknown)}")
    tasks = data.get("task")
    if not isinstance(tasks, list) or not tasks:
        raise BatchError(f"{path}: no [[task]] table")
    base = path.resolve().parent
    return [
        _task(base, n, {**defaults, **raw}) if isinstance(raw, dict) else _bad(path, n)
        for n, raw in enumerate(tasks, 1)
    ]


def _bad(path: Path, index: int) -> BatchTask:
    raise BatchError(f"{path}: task {index} is not a table")


def task_options(task: BatchTask, options: BatchOptions, out: Path) -> RewindOptions:
    index: CrateIndex | None = DirectoryIndex(task.index_dir) if task.index_dir else None
    replaying = task.replay is not None
    return RewindOptions(
        source=task.repo,
        fix=task.fix,
        out=out,
        workdir=options.workdir / task.checkout,
        base=task.base,
        image=task.image,
        resolver=options.resolver,
        vendor=task.vendor,
        index=index,
        cache_dir=options.cache_dir,
        cache=None if replaying else options.cache,
        rebuild=options.rebuild,
        reruns=options.reruns if task.reruns is None else task.reruns,
        test_timeout=options.test_timeout if task.test_timeout is None else task.test_timeout,
    )


def _resolve(task: BatchTask, options: BatchOptions, runner: Runner) -> str:
    git = open_checkout(runner, task.repo, options.workdir / task.checkout)
    return git.rev_parse(task.fix)


def _record(result: BatchResult, report: RewindReport) -> None:
    assert report.flip is not None
    result.status = "verified" if report.verified else "not-verified"
    result.base_commit = report.base
    result.toolchain = report.toolchain.version
    result.lockfile = report.lock.strategy.value
    result.fail_to_pass = len(report.flip.fail_to_pass)
    result.pass_to_pass = len(report.flip.pass_to_pass)
    result.flaky = len(report.flip.flaky)
    if report.flip.regressions:
        result.detail = f"regressions: {', '.join(report.flip.regressions)}"
    elif not report.flip.fail_to_pass:
        result.detail = "no FAIL_TO_PASS test"
    elif not report.probes.ok:
        result.detail = "a sanity probe failed"


def run_batch(
    tasks: list[BatchTask],
    options: BatchOptions,
    runner: Runner,
    backends: BackendFactory,
    log: Log,
) -> list[BatchResult]:
    """Run every task in order; an error in one task (one of ``options.failures``) is
    recorded and the batch goes on."""
    results: list[BatchResult] = []
    seen: dict[tuple[str, str], BatchResult] = {}
    bundles: dict[str, BatchResult] = {}  # bundle directory name -> the task that owns it
    options.out.mkdir(parents=True, exist_ok=True)
    for number, task in enumerate(tasks, 1):
        result = BatchResult(task, "error")
        started = time.monotonic()
        log(f"task      {number}/{len(tasks)} {task.repo} --fix {task.fix}")
        try:
            result.fix_commit = _resolve(task, options, runner)
            key = (task.slug, result.fix_commit)
            first = seen.get(key)
            owner = bundles.get(result.label)
            if first is not None:
                result.status = "duplicate"
                result.detail = f"same repository and fix commit as {first.label}"
                log(f"skip      {result.detail}")
            elif owner is not None:
                result.detail = (
                    f"bundle directory {result.label} is taken by task "
                    f"{owner.task.repo} --fix {owner.task.fix}; give this task another name"
                )
                log(f"error     {result.detail}")
            else:
                bundles[result.label] = result
                result.out = options.out / result.label
                report = rewind(
                    task_options(task, options, result.out), runner, backends(task), log
                )
                _record(result, report)
                # Only a task that ran to a verdict makes later spellings duplicates; one
                # that ended with an error left nothing to reuse, so the next one runs.
                seen[key] = result
        except options.failures as exc:
            result.status = "error"
            if bundles.get(result.label) is result:
                del bundles[result.label]  # nothing usable there: a retry may take it
            result.detail = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
            log(f"error     {result.detail}")
        result.seconds = round(time.monotonic() - started, 1)
        log(f"result    {result.label}: {result.status} in {result.seconds:g} s")
        results.append(result)
    return results


# The summary


def _rows(results: list[BatchResult]) -> list[list[str]]:
    rows = []
    for r in results:
        rows.append(
            [
                r.label,
                (r.fix_commit or r.task.fix)[:12],
                r.toolchain or "-",
                r.lockfile or "-",
                str(r.fail_to_pass) if r.status in ("verified", "not-verified") else "-",
                str(r.pass_to_pass) if r.status in ("verified", "not-verified") else "-",
                str(r.flaky) if r.status in ("verified", "not-verified") else "-",
                r.status,
                f"{r.seconds:g}",
                r.detail,
            ]
        )
    return rows


HEADERS = [
    "task",
    "fix",
    "toolchain",
    "lockfile",
    "F2P",
    "P2P",
    "flaky",
    "status",
    "seconds",
    "detail",
]


def summary_table(results: list[BatchResult]) -> str:
    """The aligned text table printed after a batch."""
    rows = [HEADERS, *_rows(results)]
    widths = [max(len(row[i]) for row in rows) for i in range(len(HEADERS))]
    lines = []
    for row in rows:
        cells = [cell.ljust(widths[i]) for i, cell in enumerate(row)]
        lines.append("  ".join(cells).rstrip())
    return "\n".join(lines)


def summary_markdown(results: list[BatchResult]) -> str:
    rows = [HEADERS, ["---"] * len(HEADERS), *_rows(results)]
    return "\n".join("| " + " | ".join(row) + " |" for row in rows) + "\n"


def counts(results: list[BatchResult]) -> dict[str, int]:
    statuses = ("verified", "not-verified", "error", "duplicate")
    return {status: sum(1 for r in results if r.status == status) for status in statuses}


def summary_document(recipes: Path, results: list[BatchResult]) -> dict[str, Any]:
    return {
        "schema_version": SUMMARY_SCHEMA,
        "generator": f"cargorewind {__version__}",
        "recipes": str(recipes),
        "tasks": len(results),
        "counts": counts(results),
        "seconds": round(sum(r.seconds for r in results), 1),
        "results": [r.as_dict() for r in results],
    }


def exit_code(results: list[BatchResult]) -> int:
    """1 when any task errored, 2 when any was not verified, else 0."""
    if any(r.status == "error" for r in results):
        return 1
    if any(r.status == "not-verified" for r in results):
        return 2
    return 0


def with_defaults(tasks: list[BatchTask], **changes: Any) -> list[BatchTask]:
    """Copies of ``tasks`` with the given fields replaced (``batch --live`` drops the
    replay transcripts this way)."""
    return [replace(task, **changes) for task in tasks]

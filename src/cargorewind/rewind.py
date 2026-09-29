"""The end-to-end rewind: checkout, split, toolchain, Dockerfile, three runs, bundle."""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cargorewind import __version__
from cargorewind.backend import Backend, Overlay, RunResult
from cargorewind.dockerfile import TEST_COMMAND, Recipe, render_dockerfile
from cargorewind.gitops import Git, GitError, GitTree, open_checkout, repo_slug
from cargorewind.libtest import Flip, Outcome, compute_flip, parse_libtest, summarize
from cargorewind.patchsplit import FileDiff, SplitResult, parse_diff, split_diff
from cargorewind.runner import Runner
from cargorewind.toolchain import Toolchain, base_image, resolve_toolchain

TASK_SCHEMA = 1
STAGES = ("base", "before", "after")

Log = Callable[[str], None]


@dataclass(frozen=True)
class RewindOptions:
    source: str
    fix: str
    out: Path
    workdir: Path
    base: str | None = None
    image: str | None = None


@dataclass
class StageRun:
    stage: str
    result: RunResult
    outcomes: dict[str, Outcome]


@dataclass
class RewindReport:
    source: str
    base: str
    fix: str
    commit_date: str
    toolchain: Toolchain
    image: str
    image_id: str
    has_lockfile: bool
    split: SplitResult
    runs: dict[str, StageRun] = field(default_factory=dict)
    flip: Flip | None = None

    def task(self) -> dict[str, Any]:
        """The task.json document."""
        assert self.flip is not None
        return {
            "schema_version": TASK_SCHEMA,
            "generator": f"cargorewind {__version__}",
            "repo": self.source,
            "base_commit": self.base,
            "fix_commit": self.fix,
            "commit_date": self.commit_date,
            "toolchain": {
                "version": self.toolchain.version,
                "source": self.toolchain.source,
                "reason": self.toolchain.reason,
            },
            "image": self.image,
            "lockfile": "committed" if self.has_lockfile else "generated",
            "test_command": " ".join(TEST_COMMAND),
            "split": {
                "test_files": sorted(d.path for d in self.split.test_files),
                "fix_files": sorted(d.path for d in self.split.fix_files),
                "shared_files": self.split.shared_files,
                "cfg_test_hunks": [
                    {
                        "path": h.path,
                        "old_start": h.old_start,
                        "new_start": h.new_start,
                        "test_lines": h.test_lines,
                        "fix_lines": h.fix_lines,
                    }
                    for h in self.split.hunks
                    if h.test_lines
                ],
                "notes": self.split.notes,
            },
            "runs": {
                name: {
                    "exit_code": run.result.exit_code,
                    "timed_out": run.result.timed_out,
                    **summarize(run.outcomes),
                }
                for name, run in self.runs.items()
            },
            "FAIL_TO_PASS": self.flip.fail_to_pass,
            "PASS_TO_PASS": self.flip.pass_to_pass,
            "regressions": self.flip.regressions,
            "still_failing": self.flip.still_failing,
            "verified": self.flip.verified,
        }


def _touched(files: list[FileDiff]) -> list[str]:
    return sorted({path for diff in files for path in diff.paths})


def _snapshot(git: Git, paths: list[str]) -> Overlay:
    """Current working-tree state of ``paths`` as an overlay over the base checkout."""
    files: dict[str, bytes] = {}
    modes: dict[str, int] = {}
    deleted: list[str] = []
    for path in paths:
        target = git.repo / path
        if target.is_file():
            files[path] = target.read_bytes()
            modes[path] = 0o755 if target.stat().st_mode & 0o111 else 0o644
        else:
            deleted.append(path)
    return Overlay(files, modes, tuple(deleted))


def build_overlays(
    git: Git, base: str, fix: str, split: SplitResult, patch_dir: Path
) -> dict[str, Overlay]:
    """Apply the patches on the host checkout and capture each stage's files.

    Also proves that test.patch applies at base, that fix.patch applies on top, and
    that together they reproduce the fix commit for every touched path.
    """
    test_paths = _touched(split.test_files)
    all_paths = _touched(split.test_files + split.fix_files)
    git.checkout_clean(base)
    try:
        if split.test_files:
            git.apply(patch_dir / "test.patch")
        before = _snapshot(git, test_paths)
        if split.fix_files:
            git.apply(patch_dir / "fix.patch")
        if not git.matches(fix, all_paths):
            raise GitError("test.patch + fix.patch do not reproduce the fix commit")
        after = _snapshot(git, all_paths)
    finally:
        git.checkout_clean(base)
    return {"base": Overlay(), "before": before, "after": after}


def rewind(options: RewindOptions, runner: Runner, backend: Backend, log: Log) -> RewindReport:
    out, workdir = options.out, options.workdir
    git = open_checkout(runner, options.source, workdir)
    fix = git.rev_parse(options.fix)
    base = git.rev_parse(options.base) if options.base else git.first_parent(fix)
    commit_time = git.commit_date(fix)
    log(f"base      {base[:12]}{'' if options.base else ' (first parent of fix)'}")
    log(f"fix       {fix[:12]}  committed {commit_time.isoformat()}")

    files = parse_diff(git.diff(base, fix))
    split = split_diff(files, GitTree(git, base), GitTree(git, fix))
    log(
        f"split     test.patch {len(split.test_files)} file(s), "
        f"fix.patch {len(split.fix_files)} file(s)"
    )
    for hunk in split.shared_hunks:
        log(
            f"shared    {hunk.path} {hunk.header}: "
            f"{hunk.test_lines} test line(s), {hunk.fix_lines} fix line(s)"
        )
    for note in split.notes:
        log(f"note      {note}")

    toolchain = resolve_toolchain(lambda name: git.show_file(base, name), commit_time)
    image = options.image or base_image(toolchain.version)
    has_lockfile = git.show_file(base, "Cargo.lock") is not None
    log(f"toolchain {toolchain.version}: {toolchain.reason}")
    log(f"image     {image}")
    log(f"lockfile  {'committed: cargo fetch --locked' if has_lockfile else 'none: generated'}")

    out.mkdir(parents=True, exist_ok=True)
    (out / "test.patch").write_text(split.test_patch)
    (out / "fix.patch").write_text(split.fix_patch)
    dockerfile = render_dockerfile(Recipe(image, toolchain.version, base, has_lockfile))
    (out / "Dockerfile").write_text(dockerfile)

    overlays = build_overlays(git, base, fix, split, out)

    context = workdir / "context"
    if context.exists():
        shutil.rmtree(context)
    git.archive(base, context / "repo")
    (context / "Dockerfile").write_text(dockerfile)

    tag = f"cargorewind/{repo_slug(options.source)}:{base[:12]}"
    log(f"build     {tag}")
    built = backend.build(context, tag)
    logs = out / "logs"
    logs.mkdir(exist_ok=True)
    (logs / "build.log").write_text(built.log)

    report = RewindReport(
        options.source,
        base,
        fix,
        commit_time.isoformat(),
        toolchain,
        image,
        built.image_id,
        has_lockfile,
        split,
    )
    for stage in STAGES:
        result = backend.run_tests(tag, stage, overlays[stage])
        (logs / f"{stage}.log").write_text(result.output)
        outcomes = parse_libtest(result.output)
        report.runs[stage] = StageRun(stage, result, outcomes)
        counts = summarize(outcomes)
        log(
            f"run       {stage:<6} exit {result.exit_code:>3}  "
            f"{counts['passed']} passed, {counts['failed']} failed, {counts['ignored']} ignored"
        )

    report.flip = compute_flip(*(report.runs[s].outcomes for s in STAGES))
    (out / "task.json").write_text(json.dumps(report.task(), indent=2) + "\n")
    return report

"""The end-to-end rewind: checkout, split, toolchain, Dockerfile, three runs, bundle."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cargorewind import __version__
from cargorewind.backend import Backend, Overlay, RunResult
from cargorewind.crateindex import CrateIndex
from cargorewind.dockerfile import LockStrategy, stage_test_command
from cargorewind.gitops import Git, GitError, GitTree, open_checkout
from cargorewind.libtest import Flip, Outcome, compute_flip, parse_libtest, summarize
from cargorewind.lockstage import (
    LockPlan,
    build_context,
    default_index,
    dockerfile_for,
    image_tag,
    plan_lock,
    run_pin_loop,
    write_lock_report,
)
from cargorewind.patchsplit import SplitResult
from cargorewind.registry import ImageChoice, ImageResolver
from cargorewind.runner import Runner
from cargorewind.splitreport import require, resolve_commits, split_commit, touched
from cargorewind.toolchain import Toolchain
from cargorewind.toolchainreport import choose_image, infer_toolchain, toolchain_document

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
    resolver: ImageResolver | None = None  # default: the offline digest table
    vendor: bool = False
    index: CrateIndex | None = None  # default: the live sparse index behind the cache


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
    image: ImageChoice
    image_id: str
    lock: LockPlan
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
            "toolchain": self.toolchain.as_dict(),
            "image": self.image.reference,
            "image_source": {"source": self.image.source, "reason": self.image.reason},
            "lockfile": self.lock.strategy.value,
            "lock_report": "lock.json",
            "vendored": self.lock.vendor,
            "test_command": " ".join(stage_test_command(self.lock.vendor)),
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
                "report": "split.json",
            },
            "toolchain_report": "toolchain.json",
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

    ``check_split`` has already proved that they apply and reproduce the fix commit; the
    blob comparison is repeated on the tree the overlays are captured from, under the
    checkout lock, so a parallel run can never leak into a bundle.
    """
    test_paths = touched(split.test_files)
    all_paths = touched(split.test_files + split.fix_files)
    with git.lock:
        git.checkout_clean(base)
        try:
            if split.test_files:
                git.apply(patch_dir / "test.patch")
            before = _snapshot(git, test_paths)
            if split.fix_files:
                git.apply(patch_dir / "fix.patch")
            if not git.matches(fix, all_paths):
                raise GitError("the patched checkout does not match the fix commit")
            after = _snapshot(git, all_paths)
        finally:
            git.checkout_clean(base)
    return {"base": Overlay(), "before": before, "after": after}


def rewind(options: RewindOptions, runner: Runner, backend: Backend, log: Log) -> RewindReport:
    out, workdir = options.out, options.workdir
    git = open_checkout(runner, options.source, workdir)
    commits = resolve_commits(git, options.fix, options.base, log)
    base, fix, commit_time = commits.base, commits.fix, commits.commit_time
    split, checks = split_commit(commits, options.source, out, log)
    require(checks)

    toolchain = infer_toolchain(commits)
    plan = plan_lock(GitTree(git, base), toolchain, commit_time, options.vendor)
    choice = choose_image(toolchain, options.image, options.resolver)
    image = choice.reference
    document = toolchain_document(options.source, commits, toolchain, choice)
    (out / "toolchain.json").write_text(json.dumps(document, indent=2) + "\n")
    log(f"toolchain {toolchain.version}: {toolchain.reason}")
    log(f"image     {image}")
    for line in plan.lines():
        log(line)

    dockerfile = dockerfile_for(image, toolchain, base, plan)
    (out / "Dockerfile").write_text(dockerfile)

    overlays = build_overlays(git, base, fix, split, out)

    # A build context of its own: parallel runs of one repository share the work directory.
    with build_context(git, base, workdir, dockerfile) as context:
        lockfile = ""
        if plan.strategy is LockStrategy.BOUNDED:
            index = options.index or default_index(commit_time)
            lockfile = run_pin_loop(plan, backend, context, index, log).lockfile
        write_lock_report(out, options.source, commits, toolchain, plan)
        tag = image_tag(options.source, base, dockerfile, lockfile)
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
        choice,
        built.image_id,
        plan,
        split,
    )
    command = stage_test_command(plan.vendor)
    for stage in STAGES:
        result = backend.run_tests(tag, stage, overlays[stage], command)
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

"""The end-to-end rewind: checkout, split, toolchain, Dockerfile, three runs, bundle."""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cargorewind import __version__
from cargorewind.backend import Backend, Overlay, RunResult
from cargorewind.dockerfile import TEST_COMMAND, Recipe, render_dockerfile
from cargorewind.gitops import Git, GitError, open_checkout, repo_slug
from cargorewind.libtest import Flip, Outcome, compute_flip, parse_libtest, summarize
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
            "toolchain": self.toolchain.as_dict(),
            "image": self.image.reference,
            "image_source": {"source": self.image.source, "reason": self.image.reason},
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


def recipe_for(image: str, toolchain: Toolchain, base: str, has_lockfile: bool) -> Recipe:
    """The Dockerfile recipe of a resolved toolchain."""
    return Recipe(
        image,
        toolchain.version,
        base,
        has_lockfile,
        install_toolchain=toolchain.install,
        components=toolchain.components,
        targets=toolchain.targets,
        profile=toolchain.profile,
        pin_toolchain=toolchain.toolchain_file is not None,
    )


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
    choice = choose_image(toolchain, options.image, options.resolver)
    image = choice.reference
    document = toolchain_document(options.source, commits, toolchain, choice)
    (out / "toolchain.json").write_text(json.dumps(document, indent=2) + "\n")
    has_lockfile = git.show_file(base, "Cargo.lock") is not None
    log(f"toolchain {toolchain.version}: {toolchain.reason}")
    log(f"image     {image}")
    log(f"lockfile  {'committed: cargo fetch --locked' if has_lockfile else 'none: generated'}")

    dockerfile = render_dockerfile(recipe_for(image, toolchain, base, has_lockfile))
    (out / "Dockerfile").write_text(dockerfile)

    overlays = build_overlays(git, base, fix, split, out)

    # A build context of its own: parallel runs of one repository share the work directory.
    context = Path(tempfile.mkdtemp(prefix="context-", dir=workdir))
    try:
        git.archive(base, context / "repo")
        (context / "Dockerfile").write_text(dockerfile)
        tag = f"cargorewind/{repo_slug(options.source)}:{base[:12]}"
        log(f"build     {tag}")
        built = backend.build(context, tag)
    finally:
        shutil.rmtree(context, ignore_errors=True)
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

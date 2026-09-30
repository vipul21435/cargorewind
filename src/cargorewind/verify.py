"""``cargorewind verify``: rebuild a task from its bundle alone and re-check the flip.

Nothing outside the bundle directory is read. The base tree comes from ``base.bundle``
(a git bundle with one root commit whose tree id must equal the one ``task.json``
records), the environment from the bundle's ``Dockerfile`` (which must be exactly what
``recipe.json`` renders, so the recipe hash still names the image), the stage trees
from ``test.patch`` and ``fix.patch``, and the probes and expected test lists from
``task.json``. The three stage runs and the reruns are the ones ``rewind`` performs;
the task is verified when every consistency check holds, the flip holds again, and
FAIL_TO_PASS and PASS_TO_PASS are exactly the recorded lists.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cargorewind import __version__
from cargorewind.backend import Backend
from cargorewind.buildcache import BuildCache, BuildRequest
from cargorewind.bundle import BundleError, Task, check_manifest, read_task, sha256_file
from cargorewind.dockerfile import PROBE_GLOB, LockStrategy, Recipe, RecipeError, render_dockerfile
from cargorewind.flip import Flip
from cargorewind.gitops import Git, GitError, GitTree, open_checkout
from cargorewind.layout import Layout
from cargorewind.lockstage import build_context
from cargorewind.patchsplit import PatchError, SplitResult, parse_diff
from cargorewind.probes import Probe, ProbeReport, host_checks
from cargorewind.rewind import (
    DEFAULT_TEST_TIMEOUT,
    BuildOutcome,
    BuildPolicy,
    RerunRun,
    Runtime,
    StageRun,
    build_image,
    build_overlays,
    execute,
    rerun_section,
    run_summaries,
    targets_of,
    tests_table,
)
from cargorewind.runner import Runner

VERIFY_SCHEMA = 1

Log = Callable[[str], None]


@dataclass(frozen=True)
class VerifyOptions:
    bundle: Path
    out: Path  # verify.json and the logs of this run
    workdir: Path
    cache: BuildCache | None = None
    rebuild: bool = False
    reruns: int | None = None  # None: as many rounds as the bundle records
    test_timeout: int | None = None  # None: the bundle's timeout


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass
class VerifyReport:
    bundle: Path
    task: Task
    recipe: Recipe
    probes: ProbeReport
    checks: list[Check] = field(default_factory=list)
    build: BuildOutcome | None = None
    runs: dict[str, StageRun] = field(default_factory=dict)
    reruns: dict[str, RerunRun] = field(default_factory=dict)
    rerun_rounds: int = 0
    test_timeout: int = DEFAULT_TEST_TIMEOUT
    flip: Flip | None = None

    @property
    def tag(self) -> str:
        return self.build.tag if self.build is not None else self.task.image_tag

    @property
    def failed(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    @property
    def verified(self) -> bool:
        """Every check holds, the flip holds again and the probes passed."""
        flipped = self.flip is not None and self.flip.verified
        return not self.failed and flipped and self.probes.ok

    def document(self) -> dict[str, Any]:
        """The verify.json document."""
        flip = self.flip
        return {
            "schema_version": VERIFY_SCHEMA,
            "generator": f"cargorewind {__version__}",
            "bundle": str(self.bundle),
            "task": {
                "repo": self.task.repo,
                "base_commit": self.task.base_commit,
                "fix_commit": self.task.fix_commit,
                "recipe": self.recipe.hash,
                "generator": self.task.generator,
            },
            "checks": [c.as_dict() for c in self.checks],
            "image_tag": self.tag,
            "build_cache": (
                {"status": self.build.cache, "reason": self.build.reason} if self.build else None
            ),
            "runs": {k: v.__dict__ for k, v in run_summaries(self.runs).items()},
            "reruns": {
                "rounds": self.rerun_rounds,
                "test_timeout": self.test_timeout,
                "stages": {k: v.__dict__ for k, v in rerun_section(self).stages.items()},
            },
            "tests": (
                {k: {**v.__dict__} for k, v in tests_table(self, flip).items()} if flip else {}
            ),
            "expected": {
                "FAIL_TO_PASS": self.task.fail_to_pass,
                "PASS_TO_PASS": self.task.pass_to_pass,
            },
            "FAIL_TO_PASS": flip.fail_to_pass if flip else [],
            "PASS_TO_PASS": flip.pass_to_pass if flip else [],
            "regressions": flip.regressions if flip else [],
            "still_failing": flip.still_failing if flip else [],
            "flaky": [{"id": f.id, "reason": f.reason} for f in (flip.flaky if flip else [])],
            "probes": self.probes.container,
            "verified": self.verified,
        }


def _read(bundle: Path, name: str) -> str:
    try:
        return (bundle / name).read_text()
    except OSError as exc:
        raise BundleError(f"{bundle / name}: cannot read: {exc}") from exc


def load_recipe(bundle: Path) -> Recipe:
    try:
        data = json.loads(_read(bundle, "recipe.json"))
        if not isinstance(data, dict):
            raise RecipeError("not a JSON object")
        return Recipe.from_dict(data)
    except (ValueError, RecipeError) as exc:
        raise BundleError(f"{bundle / 'recipe.json'}: {exc}") from exc


def consistency_checks(bundle: Path, task: Task, recipe: Recipe) -> list[Check]:
    """The bundle agrees with itself: digests, recipe hash, Dockerfile, lockfile, probes."""
    problems = check_manifest(bundle, task.files)
    checks = [
        Check(
            "files",
            not problems,
            "; ".join(problems) or f"{len(task.files)} file(s) match their sha256 in task.json",
        ),
        Check(
            "recipe",
            recipe.hash == task.recipe.hash,
            f"recipe.json hashes to {recipe.hash[:16]}, task.json names {task.recipe.hash[:16]}",
        ),
        Check(
            "dockerfile",
            render_dockerfile(recipe) == _read(bundle, "Dockerfile"),
            "the bundle's Dockerfile is what recipe.json renders",
        ),
    ]
    if recipe.lock is LockStrategy.BOUNDED:
        lock = bundle / "Cargo.lock"
        digest = sha256_file(lock) if lock.is_file() else "missing"
        checks.append(
            Check(
                "lockfile",
                digest == recipe.lockfile_sha256,
                f"Cargo.lock sha256 {digest[:16]}, recipe expects {recipe.lockfile_sha256[:16]}",
            )
        )
    names = tuple(p.name for p in task.probes.identifiers)
    checks.append(
        Check(
            "probes",
            recipe.probes == names,
            f"{len(names)} probe identifier(s) in task.json, the recipe greps for "
            f"{len(recipe.probes)}",
        )
    )
    return checks


def _log_checks(checks: list[Check], log: Log) -> None:
    for check in checks:
        mark = "ok" if check.ok else "FAILED"
        log(f"check     {check.name}: {mark} ({check.detail})")


def base_checkout(bundle: Path, task: Task, workdir: Path, runner: Runner) -> tuple[Git, str, str]:
    """Clone ``base.bundle``, check its tree id, apply both patches and commit the
    result, so the verify run has a base and a fix commit like a rewind has."""
    git = open_checkout(runner, str(bundle / task.base_tree.file), workdir)
    try:
        root = git.rev_parse(task.base_tree.commit)
    except GitError as exc:
        raise BundleError(f"{task.base_tree.file}: {exc}") from exc
    tree = git.tree_id(root)
    if tree != task.base_tree.tree:
        raise BundleError(
            f"{task.base_tree.file}: tree {tree[:12]} is not the base commit's tree "
            f"{task.base_tree.tree[:12]} recorded in task.json"
        )
    with git.lock:
        git.checkout_clean(root)
        try:
            for name in ("test.patch", "fix.patch"):
                if (bundle / name).stat().st_size:
                    git.apply(bundle / name)
            fix = git.commit_all(f"cargorewind: {task.fix_commit} rebuilt from the bundle")
        except GitError as exc:
            raise BundleError(f"the patches do not apply to the base tree: {exc}") from exc
        finally:
            git.checkout_clean(root)
    return git, root, fix


def _split(bundle: Path, git: Git, fix: str) -> SplitResult:
    try:
        test_files = parse_diff(_read(bundle, "test.patch"))
        fix_files = parse_diff(_read(bundle, "fix.patch"))
    except PatchError as exc:
        raise BundleError(f"{bundle}: {exc}") from exc
    layout = Layout(GitTree(git, fix), ())
    return SplitResult(test_files=test_files, fix_files=fix_files, packages=layout.packages)


def _lists_check(name: str, expected: list[str], actual: list[str]) -> Check:
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    detail = f"{len(actual)} test(s), as recorded"
    if missing or extra:
        parts = []
        if missing:
            parts.append(f"recorded but not found now: {', '.join(missing)}")
        if extra:
            parts.append(f"found now but not recorded: {', '.join(extra)}")
        detail = "; ".join(parts)
    return Check(name, not missing and not extra, detail)


def verify(options: VerifyOptions, runner: Runner, backend: Backend, log: Log) -> VerifyReport:
    bundle = options.bundle
    task = read_task(bundle / "task.json")
    log(f"bundle    {bundle} ({task.generator}, task schema {task.schema_version})")
    log(f"task      {task.repo} base {task.base_commit[:12]} fix {task.fix_commit[:12]}")
    recipe = load_recipe(bundle)
    probes = [Probe(i.kind, i.name, i.patch, i.path, i.line) for i in task.probes.identifiers]
    report = VerifyReport(bundle, task, recipe, ProbeReport(probes))
    report.checks = consistency_checks(bundle, task, recipe)
    _log_checks(report.checks, log)
    if report.failed:
        raise BundleError(
            f"the bundle is inconsistent ({', '.join(c.name for c in report.failed)}); "
            "nothing was built"
        )

    git, root, fix = base_checkout(bundle, task, options.workdir, runner)
    log(f"base      {task.base_tree.file}: tree {task.base_tree.tree[:12]} checked")
    split = _split(bundle, git, fix)
    try:
        overlays = build_overlays(git, root, fix, split, bundle)
    except GitError as exc:
        raise BundleError(f"the patches do not rebuild the fix: {exc}") from exc
    if probes:
        occurrences = git.grep_words(root, sorted({p.name for p in probes}), PROBE_GLOB)
        before, after = overlays["before"], overlays["after"]
        report.probes.checks = host_checks(probes, before, after, occurrences)
    for line in report.probes.lines():
        log(line)
    if not report.probes.ok:
        raise BundleError("a sanity probe failed on the host; nothing was built")

    options.out.mkdir(parents=True, exist_ok=True)
    dockerfile = _read(bundle, "Dockerfile")
    with build_context(git, root, options.workdir, dockerfile) as context:
        if recipe.lock is LockStrategy.BOUNDED:
            shutil.copyfile(bundle / "Cargo.lock", context / "Cargo.lock")
        request = BuildRequest(
            task.image_tag, recipe.hash, task.repo, task.base_commit, recipe.toolchain
        )
        policy = BuildPolicy(options.cache, options.rebuild)
        report.build = build_image(policy, backend, context, request, log)
    build = report.build
    log(f"build     {build.tag} (cache {build.cache}: {build.reason})")
    report.probes.container["build"] = "passed" if probes else "no probe"
    logs = options.out / "logs"
    logs.mkdir(exist_ok=True)
    (logs / "build.log").write_text(build.result.log)

    rounds = task.reruns.rounds if options.reruns is None else options.reruns
    timeout = task.reruns.test_timeout if options.test_timeout is None else options.test_timeout
    rt = Runtime(backend, overlays, targets_of(git, root, fix), logs, log)
    report.flip = execute(report, rt, rounds, timeout)
    report.checks += [
        _lists_check("FAIL_TO_PASS", task.fail_to_pass, report.flip.fail_to_pass),
        _lists_check("PASS_TO_PASS", task.pass_to_pass, report.flip.pass_to_pass),
    ]
    _log_checks(report.checks[-2:], log)
    document = report.document()
    (options.out / "verify.json").write_text(json.dumps(document, indent=2) + "\n")
    return report

"""The end-to-end rewind: checkout, split, toolchain, probes, recipe, cached build, three
runs, reruns by exact name, bundle."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Protocol

from cargorewind.backend import Backend, BuildResult, Overlay, RunResult
from cargorewind.buildcache import BuildCache, BuildRequest
from cargorewind.bundle import (
    BASE_BUNDLE,
    BaseTree,
    BuildCacheInfo,
    CfgTestHunk,
    FlakyTest,
    ImageSource,
    ProbeIdentifier,
    ProbeSummary,
    RecipeRef,
    RerunSection,
    RerunSummary,
    RunSummary,
    SplitSummary,
    Task,
    TestRow,
    clear_bundle,
    manifest,
    write_task,
    write_test_lists,
)
from cargorewind.crateindex import CrateIndex
from cargorewind.dockerfile import (
    PROBE_EXIT_CODE,
    PROBE_GLOB,
    PROBE_MARKER,
    LockStrategy,
    Recipe,
    render_dockerfile,
)
from cargorewind.flip import (
    STAGES,
    Flip,
    Rerun,
    StageState,
    StageTests,
    TestKey,
    apply_reruns,
    compute_flip,
    parse_reruns,
    rerun_plan,
    rerun_script,
    stage_tests,
)
from cargorewind.gitops import Git, GitError, GitTree, open_checkout
from cargorewind.layout import Layout
from cargorewind.libtest import Status
from cargorewind.lockstage import (
    LockPlan,
    build_context,
    default_index,
    first_dockerfile,
    plan_lock,
    recipe_for,
    recipe_tag,
    run_pin_loop,
    sha256_hex,
    write_lock_report,
)
from cargorewind.patchsplit import SplitResult
from cargorewind.probes import ProbeReport, plan_probes
from cargorewind.registry import ImageChoice, ImageResolver
from cargorewind.runner import Runner
from cargorewind.splitreport import Commits, require, resolve_commits, split_commit, touched
from cargorewind.testtargets import TargetMap, rerun_command
from cargorewind.toolchain import TOOLCHAIN_FILES, Decision, Toolchain
from cargorewind.toolchainreport import choose_image, infer_toolchain, toolchain_document

DEFAULT_RERUNS = 3
DEFAULT_TEST_TIMEOUT = 300  # seconds per rerun command

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
    cache_dir: Path | None = None  # index cache (default: $CARGOREWIND_CACHE_DIR, ~/.cache)
    cache: BuildCache | None = None  # None: always build (Docker's layer cache still applies)
    rebuild: bool = False  # skip the cache lookup and build with --no-cache
    reruns: int = DEFAULT_RERUNS  # reruns by exact name per candidate test (0: none)
    test_timeout: int = DEFAULT_TEST_TIMEOUT  # seconds allowed per rerun command


class ProbeError(RuntimeError):
    """A host-side sanity probe failed; nothing was built."""


@dataclass(frozen=True)
class BuildOutcome:
    tag: str
    result: BuildResult
    cache: str  # hit, miss or off
    reason: str


@dataclass
class StageRun:
    stage: str
    result: RunResult
    tests: StageTests


@dataclass
class RerunRun:
    stage: str
    result: RunResult
    items: list[Rerun]
    changed: int  # tests whose outcome differed from the stage run in some round


class Execution(Protocol):
    """What the three stage runs and the reruns fill in: shared by ``rewind`` (a fresh
    task) and ``verify`` (a task rebuilt from its bundle)."""

    recipe: Recipe
    probes: ProbeReport
    runs: dict[str, StageRun]
    reruns: dict[str, RerunRun]
    rerun_rounds: int
    test_timeout: int

    @property
    def tag(self) -> str: ...


def run_summaries(runs: dict[str, StageRun]) -> dict[str, RunSummary]:
    return {
        name: RunSummary(
            run.result.exit_code, run.result.timed_out, run.tests.state, **run.tests.counts()
        )
        for name, run in runs.items()
    }


def rerun_section(state: Execution) -> RerunSection:
    return RerunSection(
        state.rerun_rounds,
        state.test_timeout,
        {
            name: RerunSummary(
                len(run.items),
                run.result.exit_code,
                run.result.timed_out,
                run.changed,
                f"logs/rerun-{name}.log",
            )
            for name, run in state.reruns.items()
        },
    )


def tests_table(state: Execution, flip: Flip) -> dict[str, TestRow]:
    """Every test seen in any run: its target, the command that reruns it, its status
    per stage and the rerun outcomes."""
    table: dict[str, TestRow] = {}
    for test_id, key in sorted(flip.keys.items()):
        target, name = key
        raw = next(
            (r.tests.results[key].raw for r in state.runs.values() if key in r.tests.results),
            name,
        )
        table[test_id] = TestRow(
            target.label,
            name,
            " ".join(rerun_command(target, raw, state.recipe.test_command)),
            {stage: run.tests.status(key).value for stage, run in state.runs.items()},
            {
                stage: [s.value for s in seen]
                for stage, seen in flip.reruns.get(test_id, {}).items()
            },
        )
    return table


@dataclass
class RewindReport:
    source: str
    base: str
    fix: str
    commit_date: str
    toolchain: Toolchain
    image: ImageChoice
    lock: LockPlan
    split: SplitResult
    recipe: Recipe
    build: BuildOutcome
    probes: ProbeReport
    base_tree: BaseTree
    runs: dict[str, StageRun] = field(default_factory=dict)
    reruns: dict[str, RerunRun] = field(default_factory=dict)
    rerun_rounds: int = 0
    test_timeout: int = DEFAULT_TEST_TIMEOUT
    flip: Flip | None = None

    @property
    def tag(self) -> str:
        return self.build.tag

    @property
    def image_id(self) -> str:
        return self.build.result.image_id

    @property
    def verified(self) -> bool:
        """The flip holds and every sanity probe passed."""
        return self.flip is not None and self.flip.verified and self.probes.ok

    def task(self, files: dict[str, str]) -> Task:
        """The task.json document; ``files`` is the bundle manifest."""
        assert self.flip is not None
        return Task(
            repo=self.source,
            base_commit=self.base,
            fix_commit=self.fix,
            commit_date=self.commit_date,
            toolchain=self.toolchain.as_dict(),
            image=self.image.reference,
            image_source=ImageSource(self.image.source, self.image.reason),
            lockfile=self.lock.strategy.value,
            vendored=self.lock.vendor,
            test_command=" ".join(self.recipe.test_command),
            recipe=RecipeRef(self.recipe.hash),
            image_tag=self.build.tag,
            build_cache=BuildCacheInfo(self.build.cache, self.build.reason),
            probes=ProbeSummary(
                [ProbeIdentifier(**p.as_dict()) for p in self.probes.probes],
                len(self.probes.skipped),
                dict(self.probes.container),
                self.probes.ok,
            ),
            split=SplitSummary(
                sorted(d.path for d in self.split.test_files),
                sorted(d.path for d in self.split.fix_files),
                self.split.shared_files,
                [
                    CfgTestHunk(h.path, h.old_start, h.new_start, h.test_lines, h.fix_lines)
                    for h in self.split.hunks
                    if h.test_lines
                ],
                list(self.split.notes),
            ),
            base_tree=self.base_tree,
            runs=run_summaries(self.runs),
            reruns=rerun_section(self),
            tests=tests_table(self, self.flip),
            fail_to_pass=self.flip.fail_to_pass,
            pass_to_pass=self.flip.pass_to_pass,
            regressions=self.flip.regressions,
            still_failing=self.flip.still_failing,
            flaky=[FlakyTest(f.id, f.reason) for f in self.flip.flaky],
            verified=self.verified,
            files=files,
        )


def pin_patched_toolchain(toolchain: Toolchain, patched: tuple[str, ...]) -> Toolchain:
    """Record that the patches touch a toolchain file, which the stages must not obey."""
    reason = (
        f"the patches add or change {', '.join(patched)}; every stage keeps "
        f"{toolchain.version} through RUSTUP_TOOLCHAIN (rustup cannot install another "
        "channel under --network none)"
    )
    decision = Decision("pin", toolchain.version, reason)
    return replace(toolchain, decisions=(*toolchain.decisions, decision))


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


@dataclass(frozen=True)
class BuildPolicy:
    """Whether builds go through the cache, and whether a cached image is ignored."""

    cache: BuildCache | None = None  # None: always build (Docker's layer cache applies)
    rebuild: bool = False  # skip the cache lookup and build with --no-cache


def build_image(
    policy: BuildPolicy, backend: Backend, context: Path, request: BuildRequest, log: Log
) -> BuildOutcome:
    """Reuse or build the image through the cache, or build it directly without one."""
    cache, rebuild = policy.cache, policy.rebuild
    if cache is None:
        result = backend.build(context, request.tag, no_cache=rebuild)
        reason = "no build cache (replay, record or --no-build-cache)"
        return BuildOutcome(request.tag, result, "off", reason)
    cached = cache.build(backend, context, request, rebuild=rebuild, log=log)
    status = "hit" if cached.hit else "miss"
    return BuildOutcome(cached.tag, cached.result, status, cached.reason)


def _environment(
    options: RewindOptions, commits: Commits, split: SplitResult, log: Log
) -> tuple[Toolchain, LockPlan, ImageChoice, tuple[str, ...]]:
    """Toolchain, dependency plan and base image; writes toolchain.json."""
    toolchain = infer_toolchain(commits)
    paths = touched(split.test_files + split.fix_files)
    patched = tuple(p for p in paths if p in TOOLCHAIN_FILES)
    if patched:
        toolchain = pin_patched_toolchain(toolchain, patched)
    tree = GitTree(commits.git, commits.base, follow_links=True)
    plan = plan_lock(tree, toolchain, commits.commit_time, options.vendor)
    choice = choose_image(toolchain, options.image, options.resolver)
    document = toolchain_document(options.source, commits, toolchain, choice)
    (options.out / "toolchain.json").write_text(json.dumps(document, indent=2) + "\n")
    log(f"toolchain {toolchain.version}: {toolchain.reason}")
    log(f"image     {choice.reference}")
    for line in plan.lines():
        log(line)
    return toolchain, plan, choice, patched


@dataclass(frozen=True)
class Runtime:
    """What the stage runs and the reruns share."""

    backend: Backend
    overlays: dict[str, Overlay]
    targets: TargetMap
    logs: Path
    log: Log


def run_stages(state: Execution, rt: Runtime) -> None:
    """The base, before and after runs of the whole suite, recorded into ``state``."""
    probes = state.probes
    for stage in STAGES:
        required = probes.required(stage)
        command = state.recipe.test_command
        result = rt.backend.run_tests(state.tag, stage, rt.overlays[stage], command, required)
        probes.record_stage(stage, result.output)
        (rt.logs / f"{stage}.log").write_text(result.output)
        probe_failed = result.exit_code == PROBE_EXIT_CODE and PROBE_MARKER in result.output
        tests = stage_tests(stage, result, rt.targets, probe_failed=probe_failed)
        state.runs[stage] = StageRun(stage, result, tests)
        counts = tests.counts()
        stage_state = "" if tests.state == StageState.RAN else f"  ({tests.state})"
        rt.log(
            f"run       {stage:<6} exit {result.exit_code:>3}  "
            f"{counts['passed']} passed, {counts['failed']} failed, "
            f"{counts['ignored']} ignored{stage_state}"
        )
    if probes.words:
        checked = ", ".join(f"{stage} {status}" for stage, status in probes.container.items())
        rt.log(f"probe     in Docker: {checked}")


def rerun_candidates(
    state: Execution, rt: Runtime, flip: Flip, rounds: int, test_timeout: int
) -> Flip:
    """Rerun every FAIL_TO_PASS and PASS_TO_PASS candidate by exact name, N times per
    stage, and take the tests whose outcome changes out of the lists."""
    stages = {name: run.tests for name, run in state.runs.items()}
    plan = rerun_plan(flip, stages, state.recipe.test_command)
    state.rerun_rounds = rounds
    state.test_timeout = test_timeout
    seen: dict[str, dict[TestKey, list[Status]]] = {}
    for stage, items in plan.items():
        script = rerun_script(items, rounds, test_timeout)
        run = f"rerun-{stage}"
        result = rt.backend.run_script(state.tag, run, rt.overlays[stage], script)
        (rt.logs / f"{run}.log").write_text(result.output)
        statuses = parse_reruns(
            result.output, items, rounds, rt.targets, timed_out=result.timed_out
        )
        seen[stage] = statuses
        changed = sum(
            1 for item in items if set(statuses[item.key]) != {stages[stage].status(item.key)}
        )
        state.reruns[stage] = RerunRun(stage, result, items, changed)
        rt.log(
            f"rerun     {stage:<6} exit {result.exit_code:>3}  {rounds} x "
            f"{len(items)} test(s) by exact name, {changed} changed outcome"
        )
    final = apply_reruns(flip, stages, seen)
    for flaky in final.flaky:
        rt.log(f"flaky     {flaky.id}: {flaky.reason}")
    return final


def execute(state: Execution, rt: Runtime, rounds: int, test_timeout: int) -> Flip:
    """The three stage runs, the flip, and the reruns when there is a candidate."""
    run_stages(state, rt)
    flip = compute_flip(*(state.runs[s].tests for s in STAGES))
    if rounds > 0 and (flip.fail_to_pass or flip.pass_to_pass):
        flip = rerun_candidates(state, rt, flip, rounds, test_timeout)
    return flip


def targets_of(git: Git, base: str, fix: str) -> TargetMap:
    """cargo targets of the fix commit and of the base commit."""
    packages = [*Layout(GitTree(git, fix), ()).packages, *Layout(GitTree(git, base), ()).packages]
    return TargetMap(t for package in packages for t in package.targets)


def rewind(options: RewindOptions, runner: Runner, backend: Backend, log: Log) -> RewindReport:
    out, workdir = options.out, options.workdir
    git = open_checkout(runner, options.source, workdir)
    commits = resolve_commits(git, options.fix, options.base, log)
    base, fix = commits.base, commits.fix
    clear_bundle(out)  # a reused directory must not keep another task's files
    split, checks = split_commit(commits, options.source, out, log)
    require(checks)
    toolchain, plan, choice, patched = _environment(options, commits, split, log)

    overlays = build_overlays(git, base, fix, split, out)
    probes = plan_probes(
        split, overlays, lambda words: git.grep_words(base, words, PROBE_GLOB), checks.as_dict()
    )
    for line in probes.lines():
        log(line)
    if not probes.ok:
        _write_probes(out, options.source, base, fix, probes)
        raise ProbeError("a sanity probe failed on the host; see probes.json")

    base_recipe = recipe_for(choice.reference, toolchain, base, plan, patched)
    recipe = replace(base_recipe, probes=probes.words)
    # A build context of its own: parallel runs of one repository share the work directory.
    with build_context(git, base, workdir, first_dockerfile(recipe)) as context:
        if plan.strategy is LockStrategy.BOUNDED:
            index = options.index or default_index(commits.commit_time, options.cache_dir)
            bound = run_pin_loop(plan, backend, context, index, log)
            recipe = replace(recipe, lockfile_sha256=sha256_hex(bound.lockfile))
        dockerfile = render_dockerfile(recipe)
        (context / "Dockerfile").write_text(dockerfile)
        (out / "Dockerfile").write_text(dockerfile)
        (out / "recipe.json").write_text(json.dumps(recipe.document(), indent=2) + "\n")
        write_lock_report(out, options.source, commits, toolchain, plan)
        log(f"recipe    {recipe.hash}")
        tag = recipe_tag(options.source, recipe)
        request = BuildRequest(tag, recipe.hash, options.source, base, recipe.toolchain)
        policy = BuildPolicy(options.cache, options.rebuild)
        build = build_image(policy, backend, context, request, log)
    log(f"build     {build.tag} (cache {build.cache}: {build.reason})")
    probes.container["build"] = "passed" if probes.words else "no probe"
    logs = out / "logs"
    logs.mkdir(exist_ok=True)
    (logs / "build.log").write_text(build.result.log)
    with git.lock:
        root = git.bundle_tree(base, out / BASE_BUNDLE)
    base_tree = BaseTree(BASE_BUNDLE, root, git.tree_id(base))

    report = RewindReport(
        options.source,
        base,
        fix,
        commits.commit_time.isoformat(),
        toolchain,
        choice,
        plan,
        split,
        recipe,
        build,
        probes,
        base_tree,
    )
    rt = Runtime(backend, overlays, targets_of(git, base, fix), logs, log)
    report.flip = execute(report, rt, options.reruns, options.test_timeout)
    _write_probes(out, options.source, base, fix, probes)
    write_test_lists(out, report.flip.fail_to_pass, report.flip.pass_to_pass)
    write_task(out / "task.json", report.task(manifest(out)))
    return report


def _write_probes(out: Path, source: str, base: str, fix: str, probes: ProbeReport) -> None:
    document = probes.document(source, base, fix)
    (out / "probes.json").write_text(json.dumps(document, indent=2) + "\n")

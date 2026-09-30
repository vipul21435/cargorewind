"""Typer command line interface for CargoRewind."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Annotated

import typer

from cargorewind import __version__
from cargorewind.backend import (
    Backend,
    DockerBackend,
    RecordingBackend,
    ReplayBackend,
    ReplayError,
)
from cargorewind.buildcache import BuildCache, BuildCacheError
from cargorewind.crateindex import CrateIndex, DirectoryIndex
from cargorewind.deps import LockError
from cargorewind.dockerfile import LockStrategy, RecipeError
from cargorewind.gitops import GitError, GitTree, open_checkout, repo_slug
from cargorewind.lockfile import LockfileError
from cargorewind.lockstage import (
    build_context,
    default_index,
    first_dockerfile,
    plan_lock,
    recipe_for,
    run_pin_loop,
    write_lock_report,
)
from cargorewind.patchsplit import PatchError
from cargorewind.registry import default_cache_dir, make_resolver
from cargorewind.rewind import (
    DEFAULT_RERUNS,
    DEFAULT_TEST_TIMEOUT,
    ProbeError,
    RewindOptions,
    RewindReport,
    rewind,
)
from cargorewind.runner import CommandError, SubprocessRunner
from cargorewind.splitreport import resolve_commits, split_commit
from cargorewind.toolchain import ToolchainError
from cargorewind.toolchainreport import (
    choose_image,
    decision_lines,
    infer_toolchain,
    toolchain_document,
)

app = typer.Typer(
    name="cargorewind",
    help=(
        "Rebuild a Rust crate or workspace at a historical commit inside a "
        "digest-pinned Docker image and verify a fail-to-pass flip."
    ),
    no_args_is_help=True,
    add_completion=False,
)

# Host tools the pipeline drives. cargo is deliberately absent: it only runs in containers.
REQUIRED_TOOLS: tuple[str, ...] = ("git", "docker")


def check_tools(tools: tuple[str, ...] = REQUIRED_TOOLS) -> dict[str, str | None]:
    """Return the resolved path of each host tool, or None when it is not on PATH."""
    return {tool: shutil.which(tool) for tool in tools}


@app.command()
def version() -> None:
    """Print the CargoRewind version."""
    typer.echo(__version__)


@app.command()
def doctor() -> None:
    """Check that the host tools CargoRewind drives (git, docker) are installed."""
    missing = False
    for tool, path in check_tools().items():
        status = path if path is not None else "MISSING"
        typer.echo(f"{tool:<8} {status}")
        missing = missing or path is None
    if missing:
        raise typer.Exit(code=1)


EXIT_NOT_VERIFIED = 2

REGISTRY_OPTION = typer.Option(
    "--registry/--offline",
    help=(
        "Look up rust:<version>-slim digests in the Docker Hub registry (cached), with the "
        "offline table as fallback. Default: offline table only."
    ),
)
CACHE_DIR_OPTION = typer.Option(
    "--cache-dir",
    help=(
        "Cache directory for registry digests and crates.io index files "
        "(default: $CARGOREWIND_CACHE_DIR or ~/.cache/cargorewind)."
    ),
)
INDEX_DIR_OPTION = typer.Option(
    "--index-dir",
    help=(
        "Read crates.io index files from this directory (sparse index layout, e.g. "
        "recorded fixtures) instead of https://index.crates.io."
    ),
)
RECORD_OPTION = typer.Option("--record", help="Write a transcript of the Docker runs.")
REPLAY_OPTION = typer.Option("--replay", help="Replay Docker runs from a transcript (offline).")
TIMEOUT_OPTION = typer.Option("--timeout", help="Seconds allowed per docker build or run.")
# Errors that end a command with exit code 1 and a one-line message.
FAILURES = (
    BuildCacheError,
    CommandError,
    GitError,
    LockError,
    LockfileError,
    PatchError,
    ProbeError,
    RecipeError,
    ReplayError,
    ToolchainError,
)


def _backend(record: Path | None, replay: Path | None, timeout: float) -> Backend:
    if record is not None and replay is not None:
        raise typer.BadParameter("--record and --replay are mutually exclusive")
    try:
        if replay is not None:
            typer.echo(f"mode      replay of {replay} (no Docker)")
            return ReplayBackend(replay)
    except ReplayError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    backend: Backend = DockerBackend(SubprocessRunner(), timeout=timeout)
    return RecordingBackend(backend, record) if record is not None else backend


def _index(index_dir: Path | None) -> CrateIndex | None:
    return DirectoryIndex(index_dir) if index_dir is not None else None


def _print_summary(report: RewindReport, out: Path) -> None:
    flip = report.flip
    assert flip is not None
    typer.echo(f"FAIL_TO_PASS  {len(flip.fail_to_pass)}")
    for name in flip.fail_to_pass:
        typer.echo(f"  {name}")
    typer.echo(f"PASS_TO_PASS  {len(flip.pass_to_pass)}")
    if flip.regressions:
        typer.echo(f"regressions   {len(flip.regressions)}: {', '.join(flip.regressions)}")
    if flip.still_failing:
        typer.echo(f"still failing {len(flip.still_failing)}: {', '.join(flip.still_failing)}")
    if report.reruns:
        rerun = ", ".join(f"{len(r.items)} in {s}" for s, r in report.reruns.items())
        typer.echo(f"reruns        {report.rerun_rounds} x by exact name ({rerun})")
    if flip.flaky:
        typer.echo(f"flaky         {len(flip.flaky)}: {', '.join(f.id for f in flip.flaky)}")
    probes = report.probes
    if probes.words:
        state = "passed" if probes.ok else "FAILED"
        typer.echo(f"probes        {len(probes.words)} identifier(s) {state}")
    else:
        typer.echo("probes        none (no new identifier to probe)")
    verdict = "VERIFIED" if report.verified else "NOT VERIFIED"
    typer.echo(f"verdict       {verdict} fail-to-pass flip")
    files = (
        "task.json, split.json, toolchain.json, lock.json, probes.json, recipe.json, "
        "Dockerfile, patches, logs/"
    )
    if report.lock.bound is not None:
        files = files.replace("lock.json", "lock.json, Cargo.lock")
    typer.echo(f"bundle        {out}/ ({files})")


@app.command("split")
def split_command(
    source: Annotated[str, typer.Argument(help="Git URL, local repository or git bundle.")],
    fix: Annotated[str, typer.Option("--fix", help="The fix commit (full or short SHA).")],
    base: Annotated[
        str | None, typer.Option("--base", help="Base commit (default: first parent of fix).")
    ] = None,
    out: Annotated[
        Path, typer.Option("--out", help="Directory for test.patch, fix.patch, split.json.")
    ] = Path("out/split"),
    workdir: Annotated[
        Path | None,
        typer.Option("--workdir", help="Checkout directory (default: .cargorewind/)."),
    ] = None,
) -> None:
    """Split a fix commit into test.patch and fix.patch by Cargo layout role (no Docker)."""
    try:
        workdir = workdir or Path(".cargorewind") / repo_slug(source)
        git = open_checkout(SubprocessRunner(), source, workdir)
        commits = resolve_commits(git, fix, base, typer.echo)
        _, checks = split_commit(commits, source, out, typer.echo, verbose=True)
    except (CommandError, GitError, PatchError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"wrote     {out}/ (test.patch, fix.patch, split.json)")
    if not checks.ok:
        typer.echo(f"error: {checks.error()}", err=True)
        raise typer.Exit(code=1)


@app.command("toolchain")
def toolchain_command(
    source: Annotated[str, typer.Argument(help="Git URL, local repository or git bundle.")],
    sha: Annotated[
        str,
        typer.Argument(
            help="The fix commit: files are read at its base, the date rule uses its date."
        ),
    ],
    base: Annotated[
        str | None, typer.Option("--base", help="Base commit (default: first parent of fix).")
    ] = None,
    json_out: Annotated[
        Path | None, typer.Option("--json", help="Also write the report as toolchain.json.")
    ] = None,
    workdir: Annotated[
        Path | None,
        typer.Option("--workdir", help="Checkout directory (default: .cargorewind/)."),
    ] = None,
    registry: Annotated[bool, REGISTRY_OPTION] = False,
    cache_dir: Annotated[Path | None, CACHE_DIR_OPTION] = None,
) -> None:
    """Infer the toolchain and base image for a fix commit and print every decision."""
    try:
        workdir = workdir or Path(".cargorewind") / repo_slug(source)
        git = open_checkout(SubprocessRunner(), source, workdir)
        commits = resolve_commits(git, sha, base, typer.echo)
        toolchain = infer_toolchain(commits)
        for line in decision_lines(toolchain):
            typer.echo(line)
        typer.echo(f"toolchain {toolchain.version} ({toolchain.source}): {toolchain.reason}")
        image = choose_image(toolchain, None, make_resolver(registry, cache_dir))
    except (CommandError, GitError, ToolchainError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"image     {image.reference}")
    typer.echo(f"          {image.source}: {image.reason}")
    if json_out is not None:
        document = toolchain_document(source, commits, toolchain, image)
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(document, indent=2) + "\n")
        typer.echo(f"wrote     {json_out}")


@app.command("rewind")
def rewind_command(
    source: Annotated[str, typer.Argument(help="Git URL, local repository or git bundle.")],
    fix: Annotated[str, typer.Option("--fix", help="The fix commit (full or short SHA).")],
    base: Annotated[
        str | None, typer.Option("--base", help="Base commit (default: first parent of fix).")
    ] = None,
    out: Annotated[Path, typer.Option("--out", help="Directory for the task bundle.")] = Path(
        "out"
    ),
    workdir: Annotated[
        Path | None,
        typer.Option("--workdir", help="Checkout and build context (default: .cargorewind/)."),
    ] = None,
    image: Annotated[
        str | None,
        typer.Option("--image", help="Override the base image (must be digest-pinned)."),
    ] = None,
    record: Annotated[Path | None, RECORD_OPTION] = None,
    replay: Annotated[Path | None, REPLAY_OPTION] = None,
    timeout: Annotated[float, TIMEOUT_OPTION] = 3600.0,
    registry: Annotated[bool, REGISTRY_OPTION] = False,
    cache_dir: Annotated[Path | None, CACHE_DIR_OPTION] = None,
    vendor: Annotated[
        bool,
        typer.Option(
            "--vendor",
            help="cargo vendor in the image, source replacement, tests with --offline.",
        ),
    ] = False,
    index_dir: Annotated[Path | None, INDEX_DIR_OPTION] = None,
    build_cache: Annotated[
        bool,
        typer.Option(
            "--build-cache/--no-build-cache",
            help=(
                "Reuse the image of an identical recipe (index in the cache directory). "
                "Always off with --record and --replay."
            ),
        ),
    ] = True,
    rebuild: Annotated[
        bool,
        typer.Option(
            "--rebuild",
            help="Ignore a cached image and build with docker build --no-cache.",
        ),
    ] = False,
    reruns: Annotated[
        int,
        typer.Option(
            "--reruns",
            min=0,
            help=(
                "Rerun every FAIL_TO_PASS and PASS_TO_PASS candidate by exact name this many "
                "times per stage; a test whose outcome changes is flaky and leaves both lists."
            ),
        ),
    ] = DEFAULT_RERUNS,
    test_timeout: Annotated[
        int,
        typer.Option("--test-timeout", min=1, help="Seconds allowed per rerun command."),
    ] = DEFAULT_TEST_TIMEOUT,
) -> None:
    """Rebuild SOURCE at the fix's base commit and verify the fail-to-pass flip."""
    if image is not None and "@sha256:" not in image:
        raise typer.BadParameter("--image must be pinned by digest (name@sha256:...)")
    backend = _backend(record, replay, timeout)
    cache = None
    if build_cache and record is None and replay is None:
        cache = BuildCache(cache_dir or default_cache_dir(), SubprocessRunner())
    options = RewindOptions(
        source=source,
        fix=fix,
        out=out,
        workdir=workdir or Path(".cargorewind") / repo_slug(source),
        base=base,
        image=image,
        resolver=make_resolver(registry, cache_dir),
        vendor=vendor,
        index=_index(index_dir),
        cache_dir=cache_dir,
        cache=cache,
        rebuild=rebuild,
        reruns=reruns,
        test_timeout=test_timeout,
    )
    try:
        report = rewind(options, SubprocessRunner(), backend, typer.echo)
    except FAILURES as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    _print_summary(report, out)
    if not report.verified:
        raise typer.Exit(code=EXIT_NOT_VERIFIED)


@app.command("lock")
def lock_command(
    source: Annotated[str, typer.Argument(help="Git URL, local repository or git bundle.")],
    sha: Annotated[
        str,
        typer.Argument(
            help="The fix commit: files are read at its base; its date bounds the versions."
        ),
    ],
    base: Annotated[
        str | None, typer.Option("--base", help="Base commit (default: first parent of fix).")
    ] = None,
    out: Annotated[
        Path, typer.Option("--out", help="Directory for lock.json and the generated Cargo.lock.")
    ] = Path("out/lock"),
    workdir: Annotated[
        Path | None,
        typer.Option("--workdir", help="Checkout and build context (default: .cargorewind/)."),
    ] = None,
    registry: Annotated[bool, REGISTRY_OPTION] = False,
    cache_dir: Annotated[Path | None, CACHE_DIR_OPTION] = None,
    index_dir: Annotated[Path | None, INDEX_DIR_OPTION] = None,
    record: Annotated[Path | None, RECORD_OPTION] = None,
    replay: Annotated[Path | None, REPLAY_OPTION] = None,
    timeout: Annotated[float, TIMEOUT_OPTION] = 3600.0,
) -> None:
    """Check the committed Cargo.lock, or write one bounded by the commit date (Docker)."""
    backend = _backend(record, replay, timeout)
    try:
        workdir = workdir or Path(".cargorewind") / repo_slug(source)
        git = open_checkout(SubprocessRunner(), source, workdir)
        commits = resolve_commits(git, sha, base, typer.echo)
        toolchain = infer_toolchain(commits)
        typer.echo(f"toolchain {toolchain.version} ({toolchain.source}): {toolchain.reason}")
        plan = plan_lock(
            GitTree(git, commits.base, follow_links=True), toolchain, commits.commit_time, False
        )
        for line in plan.lines():
            typer.echo(line)
        if plan.strategy is LockStrategy.BOUNDED:
            image = choose_image(toolchain, None, make_resolver(registry, cache_dir))
            typer.echo(f"image     {image.reference}")
            recipe = recipe_for(image.reference, toolchain, commits.base, plan)
            dockerfile = first_dockerfile(recipe)
            index = _index(index_dir) or default_index(commits.commit_time, cache_dir)
            with build_context(git, commits.base, workdir, dockerfile) as context:
                run_pin_loop(plan, backend, context, index, typer.echo)
        write_lock_report(out, source, commits, toolchain, plan)
    except FAILURES as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    wrote = "lock.json, Cargo.lock" if plan.bound is not None else "lock.json"
    typer.echo(f"wrote     {out}/ ({wrote})")
    if plan.bound is not None and not plan.bound.bounded:
        raise typer.Exit(code=EXIT_NOT_VERIFIED)


cache_app = typer.Typer(
    name="cache",
    help="Inspect or prune the recipe-hash build cache.",
    no_args_is_help=True,
)
app.add_typer(cache_app)


@cache_app.command("list")
def cache_list(cache_dir: Annotated[Path | None, CACHE_DIR_OPTION] = None) -> None:
    """List the cached images by recipe hash (no Docker needed)."""
    cache = BuildCache(cache_dir or default_cache_dir(), SubprocessRunner())
    entries = cache.entries()
    if cache.note:
        typer.echo(f"note      {cache.note}")
    typer.echo(f"index     {cache.index_path} ({len(entries)} image(s))")
    for recipe, entry in sorted(entries.items(), key=lambda item: item[1].built_at):
        typer.echo(
            f"{recipe[:16]}  {entry.tag}  built {entry.built_at or '?'} "
            f"in {entry.build_seconds:g} s  {entry.toolchain}  base {entry.base_commit[:12]}"
        )


@cache_app.command("prune")
def cache_prune(cache_dir: Annotated[Path | None, CACHE_DIR_OPTION] = None) -> None:
    """Remove this project's dangling images and index entries whose image is gone."""
    cache = BuildCache(cache_dir or default_cache_dir(), SubprocessRunner())
    try:
        pruned = cache.prune()
    except CommandError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"docker    {pruned.docker_output or 'nothing to prune'}")
    for recipe in pruned.stale:
        typer.echo(f"stale     {recipe[:16]}: its image is gone; entry removed")
    typer.echo(f"index     {len(pruned.stale)} entry(ies) removed, {pruned.kept} kept")

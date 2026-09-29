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
from cargorewind.gitops import GitError, open_checkout, repo_slug
from cargorewind.patchsplit import PatchError
from cargorewind.registry import make_resolver
from cargorewind.rewind import RewindOptions, RewindReport, rewind
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
    help="Registry digest cache directory (default: $CARGOREWIND_CACHE_DIR or ~/.cache).",
)


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
    verdict = "VERIFIED" if flip.verified else "NOT VERIFIED"
    typer.echo(f"verdict       {verdict} fail-to-pass flip")
    typer.echo(
        f"bundle        {out}/ (task.json, split.json, Dockerfile, test.patch, fix.patch, logs/)"
    )


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
    record: Annotated[
        Path | None, typer.Option("--record", help="Write a transcript of the Docker runs.")
    ] = None,
    replay: Annotated[
        Path | None,
        typer.Option("--replay", help="Replay Docker runs from a transcript (offline)."),
    ] = None,
    timeout: Annotated[
        float, typer.Option("--timeout", help="Seconds allowed per docker build or run.")
    ] = 3600.0,
    registry: Annotated[bool, REGISTRY_OPTION] = False,
    cache_dir: Annotated[Path | None, CACHE_DIR_OPTION] = None,
) -> None:
    """Rebuild SOURCE at the fix's base commit and verify the fail-to-pass flip."""
    if record is not None and replay is not None:
        raise typer.BadParameter("--record and --replay are mutually exclusive")
    if image is not None and "@sha256:" not in image:
        raise typer.BadParameter("--image must be pinned by digest (name@sha256:...)")
    runner = SubprocessRunner()
    backend: Backend
    try:
        if replay is not None:
            backend = ReplayBackend(replay)
            typer.echo(f"mode      replay of {replay} (no Docker)")
        else:
            backend = DockerBackend(runner, timeout=timeout)
            if record is not None:
                backend = RecordingBackend(backend, record)
    except ReplayError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    options = RewindOptions(
        source=source,
        fix=fix,
        out=out,
        workdir=workdir or Path(".cargorewind") / repo_slug(source),
        base=base,
        image=image,
        resolver=make_resolver(registry, cache_dir),
    )
    try:
        report = rewind(options, runner, backend, typer.echo)
    except (CommandError, GitError, PatchError, ReplayError, ToolchainError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    _print_summary(report, out)
    if report.flip is None or not report.flip.verified:
        raise typer.Exit(code=EXIT_NOT_VERIFIED)

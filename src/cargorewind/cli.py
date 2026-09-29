"""Typer command line interface for CargoRewind."""

from __future__ import annotations

import shutil

import typer

from cargorewind import __version__

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

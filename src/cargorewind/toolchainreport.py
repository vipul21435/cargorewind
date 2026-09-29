"""The toolchain report shared by ``cargorewind toolchain`` and ``cargorewind rewind``."""

from __future__ import annotations

from typing import Any

from cargorewind import __version__
from cargorewind.gitops import GitTree
from cargorewind.registry import ImageChoice, ImageResolver
from cargorewind.splitreport import Commits
from cargorewind.toolchain import Toolchain, resolve_toolchain

TOOLCHAIN_SCHEMA = 1


def infer_toolchain(commits: Commits) -> Toolchain:
    """Toolchain for the base tree of ``commits``, dated by the fix commit."""
    return resolve_toolchain(GitTree(commits.git, commits.base), commits.commit_time)


def choose_image(
    toolchain: Toolchain, override: str | None, resolver: ImageResolver | None
) -> ImageChoice:
    """The ``--image`` override, else the resolver's answer (default: offline table)."""
    if override is not None:
        return ImageChoice(override, "override", "--image")
    return (resolver or ImageResolver()).resolve(toolchain.image_version)


def decision_lines(toolchain: Toolchain) -> list[str]:
    """One aligned line per decision, in the order they were made."""
    return [f"{d.step:<9} {d.outcome}: {d.reason}" for d in toolchain.decisions]


def toolchain_document(
    source: str, commits: Commits, toolchain: Toolchain, image: ImageChoice
) -> dict[str, Any]:
    """The toolchain.json document."""
    return {
        "schema_version": TOOLCHAIN_SCHEMA,
        "generator": f"cargorewind {__version__}",
        "repo": source,
        "base_commit": commits.base,
        "fix_commit": commits.fix,
        "commit_date": commits.commit_time.isoformat(),
        "toolchain": toolchain.as_dict(),
        "image": image.as_dict(),
    }

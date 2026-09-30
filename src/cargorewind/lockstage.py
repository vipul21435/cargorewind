"""The dependency step of an environment, shared by ``cargorewind lock`` and ``rewind``.

``plan_lock`` decides the strategy from the base tree: the committed Cargo.lock
(``cargo fetch --locked``), a lockfile cargo can write inside the image because there is
no crates.io dependency to bound, or a lockfile bounded by the commit date, which the
pin loop produces in a container of the Dockerfile's ``toolchain`` stage before the
final image is built.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from cargorewind import __version__
from cargorewind.backend import Backend
from cargorewind.crateindex import CrateIndex, SparseIndex
from cargorewind.deps import (
    BoundLock,
    CargoDriver,
    Requirement,
    bound_lockfile,
    manifest_requirements,
)
from cargorewind.dockerfile import (
    TOOLCHAIN_STAGE,
    LockStrategy,
    Recipe,
    render_dockerfile,
    render_toolchain_stage,
    stage_test_command,
)
from cargorewind.gitops import Git, repo_slug
from cargorewind.layout import SourceTree
from cargorewind.lockfile import READ_MINIMUM, parse_lockfile
from cargorewind.registry import UrllibClient, default_cache_dir
from cargorewind.splitreport import Commits
from cargorewind.toolchain import Toolchain, ToolchainError, channel_version

LOCK_SCHEMA = 1
VENDOR_MINIMUM = (1, 37, 0)  # cargo vendor became part of cargo
CONFIG_TOML_MINIMUM = (1, 39, 0)  # .cargo/config.toml (before: .cargo/config)

Log = Callable[[str], None]
Version = tuple[int, int, int]


def _fmt(version: Version) -> str:
    return ".".join(str(part) for part in version)


def cargo_version(toolchain: Toolchain) -> Version:
    """The cargo version of a toolchain (estimated for a dated nightly or beta)."""
    if toolchain.install:
        kind, day = toolchain.version.split("-", 1)
        return channel_version(kind, date.fromisoformat(day))
    major, minor, patch = (*(int(p) for p in toolchain.version.split(".")), 0, 0)[:3]
    return (major, minor, patch)


@dataclass
class LockPlan:
    strategy: LockStrategy
    cutoff: datetime
    cargo: Version
    vendor: bool = False
    format_version: int | None = None
    requirements: list[Requirement] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    bound: BoundLock | None = None
    opaque: list[str] = field(default_factory=list)  # git or unreadable path dependencies

    @property
    def cargo_config(self) -> str:
        return "config.toml" if self.cargo >= CONFIG_TOML_MINIMUM else "config"

    def lines(self) -> list[str]:
        """What was decided, one aligned line each."""
        if self.strategy is LockStrategy.COMMITTED:
            minimum = READ_MINIMUM.get(self.format_version or 1)
            reads = f"cargo {_fmt(minimum)}+ reads it" if minimum else "every cargo reads it"
            lines = [
                f"lockfile  committed Cargo.lock, format v{self.format_version} ({reads}); "
                "cargo fetch --locked"
            ]
        elif self.strategy is LockStrategy.GENERATED:
            lines = [
                "lockfile  none, and no crates.io dependencies: cargo generates it in the image"
            ]
        elif self.requirements:
            lines = [
                f"lockfile  none: {len(self.requirements)} crates.io requirement(s); "
                f"bounding every package to before {self.cutoff.isoformat()}"
            ]
        else:
            lines = [
                f"lockfile  none: no crates.io requirement in the manifests, but "
                f"{len(self.opaque)} dependency(ies) can bring some; bounding every package "
                f"to before {self.cutoff.isoformat()}"
            ]
        if self.vendor:
            config = f"~/.cargo/{self.cargo_config}"
            lines.append(f"vendor    cargo vendor, source replacement in {config}, tests --offline")
        return lines + [f"note      {note}" for note in self.notes]


def plan_lock(tree: SourceTree, toolchain: Toolchain, cutoff: datetime, vendor: bool) -> LockPlan:
    """Choose how the environment gets its dependencies."""
    cargo = cargo_version(toolchain)
    if vendor and cargo < VENDOR_MINIMUM:
        raise ToolchainError(
            f"--vendor needs cargo {_fmt(VENDOR_MINIMUM)}+ (cargo vendor); "
            f"the toolchain is {toolchain.version}"
        )
    text = tree.read("Cargo.lock")
    if text is not None:
        version = parse_lockfile(text).format_version
        return LockPlan(LockStrategy.COMMITTED, cutoff, cargo, vendor, version)
    # Path dependencies are followed; a git dependency (or a path dependency the tree
    # does not hold) can still bring crates.io packages, which only the lockfile cargo
    # generates shows, so the pin loop runs for those too.
    scan = manifest_requirements(tree)
    strategy = LockStrategy.BOUNDED if scan.needs_bounding else LockStrategy.GENERATED
    return LockPlan(
        strategy, cutoff, cargo, vendor, None, scan.requirements, scan.notes, opaque=scan.opaque
    )


def recipe_for(
    image: str,
    toolchain: Toolchain,
    base: str,
    plan: LockPlan,
    patched_toolchain_files: tuple[str, ...] = (),
) -> Recipe:
    """The Dockerfile recipe of a resolved toolchain and lock plan.

    ``RUSTUP_TOOLCHAIN`` is pinned when the base checkout has a toolchain file, and also
    when a patch adds or changes one: the stages extract the patched file into the
    checkout, and under ``--network none`` rustup could not install its channel. The
    probes are added by the caller, and a date-bounded recipe gets its
    ``lockfile_sha256`` once the pin loop has run.
    """
    return Recipe(
        image,
        toolchain.version,
        base,
        plan.strategy,
        install_toolchain=toolchain.install,
        components=toolchain.components,
        targets=toolchain.targets,
        profile=toolchain.profile,
        pin_toolchain=toolchain.toolchain_file is not None or bool(patched_toolchain_files),
        cutoff=plan.cutoff.isoformat() if plan.strategy is LockStrategy.BOUNDED else "",
        vendor=plan.vendor,
        cargo_config=plan.cargo_config,
        test_command=stage_test_command(plan.vendor, json_format=nightly(toolchain)),
    )


def nightly(toolchain: Toolchain) -> bool:
    """A nightly channel installed with rustup, whose libtest takes ``--format json``."""
    return toolchain.install and toolchain.version.startswith("nightly")


def default_index(cutoff: datetime, cache_dir: Path | None = None) -> CrateIndex:
    """The live crates.io sparse index behind the cache (refetched when older than cutoff)."""
    return SparseIndex(
        UrllibClient(timeout=30.0), cache_dir or default_cache_dir(), fresh_after=cutoff
    )


@contextmanager
def build_context(git: Git, base: str, workdir: Path, dockerfile: str) -> Iterator[Path]:
    """A private build context (base tree plus Dockerfile), removed afterwards."""
    workdir.mkdir(parents=True, exist_ok=True)
    context = Path(tempfile.mkdtemp(prefix="context-", dir=workdir))
    try:
        git.archive(base, context / "repo")
        (context / "Dockerfile").write_text(dockerfile)
        yield context
    finally:
        shutil.rmtree(context, ignore_errors=True)


def digest(*parts: str) -> str:
    sha = hashlib.sha256()
    for part in parts:
        sha.update(part.encode())
        sha.update(b"\0")
    return sha.hexdigest()


def recipe_tag(source: str, recipe: Recipe) -> str:
    """``cargorewind/<repo>:<first 16 hex digits of the recipe hash>``."""
    return f"cargorewind/{repo_slug(source)}:{recipe.hash[:16]}"


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def run_pin_loop(
    plan: LockPlan, backend: Backend, context: Path, index: CrateIndex, log: Log
) -> BoundLock:
    """Build the toolchain stage, run the pin loop in a session of it, and put the
    resulting Cargo.lock into the build context."""
    stage_tag = f"cargorewind/toolchain-stage:{digest((context / 'Dockerfile').read_text())[:12]}"
    log(f"build     {stage_tag} (toolchain stage for the pin loop)")
    backend.build(context, stage_tag, TOOLCHAIN_STAGE)
    session = backend.open_session(stage_tag)
    try:
        bound = bound_lockfile(CargoDriver(session), index, plan.cutoff, plan.requirements, log)
    finally:
        session.close()
    plan.bound = bound
    plan.notes.extend(bound.notes)
    (context / "Cargo.lock").write_text(bound.lockfile)
    status = (
        "every crates.io package is bounded"
        if bound.bounded
        else (f"{len(bound.unbounded)} package(s) could not be bounded")
    )
    log(f"lock      {len(bound.pins())} pin(s) in {len(bound.rounds)} round(s); {status}")
    return bound


def lock_document(
    source: str, commits: Commits, toolchain: Toolchain, plan: LockPlan
) -> dict[str, Any]:
    """The lock.json document."""
    bound = plan.bound
    minimum = READ_MINIMUM.get(plan.format_version) if plan.format_version else None
    return {
        "schema_version": LOCK_SCHEMA,
        "generator": f"cargorewind {__version__}",
        "repo": source,
        "base_commit": commits.base,
        "fix_commit": commits.fix,
        "strategy": plan.strategy.value,
        "toolchain": toolchain.version,
        "cargo": _fmt(plan.cargo),
        "cutoff": plan.cutoff.isoformat(),
        "format_version": plan.format_version,
        "readable_from": _fmt(minimum) if minimum else None,
        "vendor": plan.vendor,
        "cargo_config": plan.cargo_config if plan.vendor else None,
        "requirements": [
            {
                "member": r.member,
                "name": r.name,
                "req": r.req,
                "kind": r.kind,
                "target": r.target,
                "optional": r.optional,
            }
            for r in plan.requirements
        ],
        "registry_packages": bound.registry_packages if bound else None,
        "late_at_start": bound.late_at_start if bound else None,
        "rounds": [
            {
                "round": r.number,
                "pins": [
                    {
                        "name": p.name,
                        "from": p.from_version,
                        "to": p.to_version,
                        "reason": p.reason,
                        "error": r.failed.get(p.spec),
                    }
                    for p in r.pins
                ],
            }
            for r in (bound.rounds if bound else [])
        ],
        "unbounded": [
            {"name": u.name, "version": u.version, "reason": u.reason}
            for u in (bound.unbounded if bound else [])
        ],
        "bounded": bound.bounded if bound else None,
        "notes": plan.notes,
    }


def write_lock_report(
    out: Path, source: str, commits: Commits, toolchain: Toolchain, plan: LockPlan
) -> None:
    out.mkdir(parents=True, exist_ok=True)
    document = lock_document(source, commits, toolchain, plan)
    (out / "lock.json").write_text(json.dumps(document, indent=2) + "\n")
    if plan.bound is not None:
        (out / "Cargo.lock").write_text(plan.bound.lockfile)


def first_dockerfile(recipe: Recipe) -> str:
    """The Dockerfile to put into the build context before any build: the toolchain
    stage alone for a date-bounded recipe (the pin loop has not written the lockfile
    yet), the whole file otherwise."""
    if recipe.lock is LockStrategy.BOUNDED:
        return render_toolchain_stage(recipe)
    return render_dockerfile(recipe)

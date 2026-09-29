"""Split a fix commit, prove the patches with ``git apply --check``, write split.json.

Shared by ``cargorewind split`` (the split alone) and ``cargorewind rewind`` (which
continues into the Docker runs).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from cargorewind import __version__
from cargorewind.gitops import Git, GitError, GitTree
from cargorewind.patchsplit import FileDiff, SplitResult, parse_diff, split_diff

SPLIT_SCHEMA = 1

Log = Callable[[str], None]


@dataclass(frozen=True)
class Commits:
    git: Git
    base: str
    fix: str
    commit_time: datetime


@dataclass(frozen=True)
class SplitChecks:
    """Results of applying the patches to a clean checkout of the base commit.

    ``None`` means the patch is empty, so there was nothing to apply.
    """

    test_patch_applies: bool | None
    fix_patch_applies: bool | None
    reproduces_fix: bool
    detail: str = ""

    @property
    def ok(self) -> bool:
        applied = self.test_patch_applies is not False and self.fix_patch_applies is not False
        return applied and self.reproduces_fix

    def as_dict(self) -> dict[str, bool | None]:
        return {
            "test_patch_applies_at_base": self.test_patch_applies,
            "fix_patch_applies_after_test_patch": self.fix_patch_applies,
            "patches_reproduce_fix": self.reproduces_fix,
        }

    def error(self) -> str:
        if self.test_patch_applies is False:
            return f"test.patch does not apply at base\n{self.detail}".rstrip()
        if self.fix_patch_applies is False:
            return f"fix.patch does not apply after test.patch\n{self.detail}".rstrip()
        return "test.patch + fix.patch do not reproduce the fix commit"


def touched(files: list[FileDiff]) -> list[str]:
    return sorted({path for diff in files for path in diff.paths})


def resolve_commits(git: Git, fix: str, base: str | None, log: Log) -> Commits:
    """Resolve the fix commit and its base (by default the fix's first parent)."""
    fix_sha = git.rev_parse(fix)
    base_sha = git.rev_parse(base) if base else git.first_parent(fix_sha)
    commit_time = git.commit_date(fix_sha)
    log(f"base      {base_sha[:12]}{'' if base else ' (first parent of fix)'}")
    log(f"fix       {fix_sha[:12]}  committed {commit_time.isoformat()}")
    return Commits(git, base_sha, fix_sha, commit_time)


def _apply_checked(git: Git, patch: Path) -> tuple[bool, str]:
    ok, detail = git.apply_check(patch)
    if ok:
        git.apply(patch)
    return ok, detail


def check_split(git: Git, base: str, fix: str, split: SplitResult, patch_dir: Path) -> SplitChecks:
    """``git apply --check`` test.patch at base, then fix.patch on top, then compare blobs."""
    git.checkout_clean(base)
    test_ok: bool | None = None
    fix_ok: bool | None = None
    detail = ""
    try:
        if split.test_files:
            test_ok, detail = _apply_checked(git, patch_dir / "test.patch")
        if test_ok is not False and split.fix_files:
            fix_ok, detail = _apply_checked(git, patch_dir / "fix.patch")
        applied = test_ok is not False and fix_ok is not False
        paths = touched(split.test_files + split.fix_files)
        reproduces = applied and git.matches(fix, paths)
    finally:
        git.checkout_clean(base)
    return SplitChecks(test_ok, fix_ok, reproduces, detail)


def split_document(
    split: SplitResult, source: str, commits: Commits, checks: SplitChecks
) -> dict[str, Any]:
    """The split.json document."""
    touched_packages = {f.package for f in split.files}
    return {
        "schema_version": SPLIT_SCHEMA,
        "generator": f"cargorewind {__version__}",
        "repo": source,
        "base_commit": commits.base,
        "fix_commit": commits.fix,
        "summary": {
            "files": len(split.files),
            "test_patch_files": sorted(d.path for d in split.test_files),
            "fix_patch_files": sorted(d.path for d in split.fix_files),
            "shared_files": split.shared_files,
            "packages_in_tree": len(split.packages),
        },
        "packages": [
            {
                "root": p.display_root,
                "name": p.name,
                "workspace": None if p.workspace is None else (p.workspace or "."),
                "targets": [{"kind": t.kind, "name": t.name, "path": t.path} for t in p.targets],
            }
            for p in split.packages
            if p.display_root in touched_packages
        ],
        "files": [
            {
                "path": f.path,
                "status": f.status,
                "role": f.role.value,
                "package": f.package,
                "target": f.target,
                "patch": f.patch,
                "reason": f.reason,
            }
            for f in split.files
        ],
        "shared_hunks": [
            {
                "path": h.path,
                "hunk": h.header,
                "test_lines": h.test_lines,
                "fix_lines": h.fix_lines,
                "test_patch_hunk": h.test_header,
                "fix_patch_hunk": h.fix_header,
            }
            for h in split.shared_hunks
        ],
        "cfg_test_regions": [
            {
                "path": r.path,
                "revision": r.revision,
                "kind": r.region.kind,
                "name": r.region.name,
                "cfg": r.region.cfg,
                "start_line": r.region.start_line,
                "end_line": r.region.end_line,
            }
            for r in split.regions
        ],
        "notes": split.notes,
        "checks": checks.as_dict(),
    }


def _mark(value: bool | None) -> str:
    return {True: "ok", False: "FAILED", None: "skipped (empty patch)"}[value]


def log_checks(checks: SplitChecks, paths: int, log: Log) -> None:
    rows = (
        ("test.patch applies at base (git apply --check)", checks.test_patch_applies),
        ("fix.patch applies on top (git apply --check)", checks.fix_patch_applies),
        (f"both patches reproduce the fix ({paths} paths)", checks.reproduces_fix),
    )
    for label, value in rows:
        log(f"check     {label}: {_mark(value)}")


def split_commit(
    commits: Commits, source: str, out: Path, log: Log, *, verbose: bool = False
) -> tuple[SplitResult, SplitChecks]:
    """Split the fix, write test.patch, fix.patch and split.json into ``out``, check them."""
    git = commits.git
    files = parse_diff(git.diff(commits.base, commits.fix))
    split = split_diff(files, GitTree(git, commits.base), GitTree(git, commits.fix))
    if verbose:
        _log_files(split, log)
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
    out.mkdir(parents=True, exist_ok=True)
    (out / "test.patch").write_text(split.test_patch)
    (out / "fix.patch").write_text(split.fix_patch)
    checks = check_split(git, commits.base, commits.fix, split, out)
    log_checks(checks, len(touched(files)), log)
    document = split_document(split, source, commits, checks)
    (out / "split.json").write_text(json.dumps(document, indent=2) + "\n")
    return split, checks


def _log_files(split: SplitResult, log: Log) -> None:
    touched_roots = sorted({f.package for f in split.files if f.package is not None})
    names = {p.display_root: p.name for p in split.packages}
    listed = ", ".join(f"{root} ({names.get(root, '?')})" for root in touched_roots) or "none"
    log(f"packages  {len(split.packages)} in the tree; touched: {listed}")
    width = max((len(f.role.value) for f in split.files), default=0)
    for f in split.files:
        target = f" [{f.target}]" if f.target else ""
        log(f"file      {f.role.value:<{width}}  {f.patch:<4}  {f.path}{target}")
    for r in split.regions:
        region = r.region
        name = f" {region.name}" if region.name else ""
        log(
            f"region    {r.path}:{region.start_line}-{region.end_line} "
            f"{region.kind}{name}, cfg({region.cfg})"
        )


def require(checks: SplitChecks) -> None:
    """Raise ``GitError`` unless every split check passed."""
    if not checks.ok:
        raise GitError(checks.error())

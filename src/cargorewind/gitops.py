"""Git operations on the local work checkout (clone, resolve, diff, archive, apply)."""

from __future__ import annotations

import re
import shutil
import tarfile
from datetime import UTC, datetime
from pathlib import Path

from cargorewind.runner import CommandResult, Runner, checked

_SLUG_STRIP = re.compile(r"(\.git|\.bundle)$")
_SLUG_BAD = re.compile(r"[^a-z0-9._-]+")


class GitError(RuntimeError):
    """A git precondition failed (unknown commit, root commit, bad patch)."""


def repo_slug(source: str) -> str:
    """Short, docker-tag-safe name for a repository URL or path."""
    name = source.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    name = _SLUG_STRIP.sub("", name.lower())
    name = _SLUG_BAD.sub("-", name).strip("-._")
    return name or "repo"


class Git:
    """Thin typed wrapper over ``git -C <repo>`` driven through a ``Runner``."""

    def __init__(self, runner: Runner, repo: Path) -> None:
        self.runner = runner
        self.repo = repo

    def _run(self, *args: str) -> CommandResult:
        return self.runner.run(["git", "-C", str(self.repo), *args])

    def _out(self, *args: str) -> str:
        return checked(self._run(*args)).stdout

    def rev_parse(self, rev: str) -> str:
        result = self._run("rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}")
        if not result.ok:
            raise GitError(f"unknown commit: {rev}")
        return result.stdout.strip()

    def first_parent(self, sha: str) -> str:
        try:
            return self.rev_parse(f"{sha}^1")
        except GitError:
            raise GitError(f"{sha} has no parent; pass --base explicitly") from None

    def commit_date(self, sha: str) -> datetime:
        """Committer date of ``sha`` in UTC."""
        stamp = self._out("show", "-s", "--format=%cI", sha).strip()
        return datetime.fromisoformat(stamp).astimezone(UTC)

    def diff(self, base: str, fix: str) -> str:
        return self._out(
            "diff",
            "--no-color",
            "--no-ext-diff",
            "--no-renames",
            "--full-index",
            "--binary",
            base,
            fix,
        )

    def show_file(self, rev: str, path: str) -> str | None:
        """Content of ``path`` at ``rev``, or None when it does not exist there."""
        result = self._run("show", f"{rev}:{path}")
        return result.stdout if result.ok else None

    def archive(self, rev: str, dest: Path) -> None:
        """Extract the tree of ``rev`` (no .git) into ``dest``."""
        dest.mkdir(parents=True, exist_ok=True)
        tar_path = dest.parent / f".{dest.name}.tar"
        checked(self._run("archive", "--format=tar", f"--output={tar_path}", rev))
        try:
            with tarfile.open(tar_path) as tar:
                tar.extractall(dest, filter="data")
        finally:
            tar_path.unlink(missing_ok=True)

    def checkout_clean(self, rev: str) -> None:
        """Force the working tree to exactly ``rev`` (detached, untracked files removed)."""
        checked(self._run("checkout", "--quiet", "--force", "--detach", rev))
        checked(self._run("clean", "-fdxq"))

    def apply(self, patch: Path) -> None:
        result = self._run("apply", "--whitespace=nowarn", str(patch))
        if not result.ok:
            raise GitError(f"patch does not apply: {patch.name}\n{result.stderr.strip()}")

    def matches(self, rev: str, paths: list[str]) -> bool:
        """True when the working tree equals ``rev`` for every path in ``paths``.

        Blob ids are compared, so added and deleted files count as differences too.
        """
        if not paths:
            return True
        expected: dict[str, str] = {}
        for entry in self._out("ls-tree", "-r", "-z", rev, "--", *paths).split("\0"):
            if not entry:
                continue
            meta, path = entry.split("\t", 1)
            expected[path] = meta.split()[2]
        on_disk = [p for p in paths if (self.repo / p).is_file()]
        actual: dict[str, str] = {}
        if on_disk:
            hashes = self._out("hash-object", "--", *on_disk).split()
            actual = dict(zip(on_disk, hashes, strict=True))
        return actual == expected


def open_checkout(runner: Runner, source: str, workdir: Path) -> Git:
    """Clone ``source`` (URL, path or bundle) into ``workdir/repo``, or refresh it."""
    repo_dir = workdir / "repo"
    if (repo_dir / ".git").is_dir():
        checked(runner.run(["git", "-C", str(repo_dir), "fetch", "--quiet", "origin"]))
    else:
        if repo_dir.exists():
            shutil.rmtree(repo_dir)
        workdir.mkdir(parents=True, exist_ok=True)
        checked(runner.run(["git", "clone", "--quiet", source, str(repo_dir)]))
    return Git(runner, repo_dir)

"""Git operations on the local work checkout (clone, resolve, diff, archive, apply)."""

from __future__ import annotations

import fcntl
import os
import posixpath
import re
import shutil
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType

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


def lock_path(repo: Path) -> Path:
    """The lock file that guards the working tree of the checkout at ``repo``."""
    return repo.parent / f".{repo.name}.lock"


class CheckoutLock:
    """An exclusive ``flock`` on a work checkout, so parallel runs take turns.

    Several ``split`` or ``rewind`` runs may share one checkout (the default work
    directory is per repository). Every section that checks out, cleans, applies
    patches or reads the working tree holds this lock. It is reentrant within one
    object; separate objects (and processes) block each other.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None
        self._depth = 0

    def __enter__(self) -> CheckoutLock:
        if self._depth == 0:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except BaseException:
                os.close(fd)
                raise
            self._fd = fd
        self._depth += 1
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._depth -= 1
        if self._depth == 0 and self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


class Git:
    """Thin typed wrapper over ``git -C <repo>`` driven through a ``Runner``."""

    def __init__(self, runner: Runner, repo: Path) -> None:
        self.runner = runner
        self.repo = repo
        self.lock = CheckoutLock(lock_path(repo))

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

    def list_files(self, rev: str) -> list[str]:
        """Every file path in the tree of ``rev``."""
        return list(self.list_modes(rev))

    def list_modes(self, rev: str) -> dict[str, str]:
        """Every file path in the tree of ``rev`` with its mode (``120000`` is a symlink)."""
        modes: dict[str, str] = {}
        for entry in self._out("ls-tree", "-r", "-z", rev).split("\0"):
            if entry:
                meta, path = entry.split("\t", 1)
                modes[path] = meta.split()[0]
        return modes

    def grep_words(self, rev: str, words: list[str], pathspec: str) -> dict[str, list[str]]:
        """Files of ``rev`` matching ``pathspec`` that contain each of ``words`` as a whole
        word (``git grep -w -F``: letters, digits and underscores form words, as in GNU
        grep). Words that occur nowhere are absent from the result."""
        if not words:
            return {}
        patterns = [arg for word in words for arg in ("-e", word)]
        result = self._run("grep", "-I", "-o", "-w", "-F", "-z", *patterns, rev, "--", pathspec)
        if result.returncode == 1:  # no match anywhere
            return {}
        found: dict[str, set[str]] = {}
        for line in checked(result).stdout.split("\n"):
            name, sep, match = line.partition("\0")
            if sep:
                found.setdefault(match, set()).add(name.removeprefix(f"{rev}:"))
        return {word: sorted(paths) for word, paths in found.items()}

    def archive(self, rev: str, dest: Path) -> None:
        """Extract the tree of ``rev`` (no .git) into ``dest``."""
        dest.mkdir(parents=True, exist_ok=True)
        tar_path = (dest.parent / f".{dest.name}.tar").resolve()
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
        result = self._run("apply", "--whitespace=nowarn", str(patch.resolve()))
        if not result.ok:
            raise GitError(f"patch does not apply: {patch.name}\n{result.stderr.strip()}")

    def apply_check(self, patch: Path) -> tuple[bool, str]:
        """``git apply --check``: whether ``patch`` applies to the working tree, and why not."""
        result = self._run("apply", "--check", "--whitespace=nowarn", str(patch.resolve()))
        return result.ok, result.stderr.strip()

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


LINK_MODE = "120000"
MAX_LINK_HOPS = 8


class GitTree:
    """One commit of a repository as a read-only ``SourceTree``.

    ``git show rev:path`` prints a symlink's target path, not the file it points to.
    That is what a diff shows, so the split reads links as they are. Readers that stand
    in for rustup or cargo (toolchain inference, lock planning) pass ``follow_links``:
    a link is then resolved inside the tree, like the tools do in a checkout. A link
    that leaves the tree, dangles or loops reads as a missing file.
    """

    def __init__(self, git: Git, rev: str, *, follow_links: bool = False) -> None:
        self.git = git
        self.rev = rev
        self.follow_links = follow_links
        self._modes: dict[str, str] | None = None

    def _entries(self) -> dict[str, str]:
        if self._modes is None:
            self._modes = self.git.list_modes(self.rev)
        return self._modes

    def resolve(self, path: str) -> str | None:
        """The file ``path`` names after following symlinks, or None when there is none."""
        modes = self._entries()
        for _ in range(MAX_LINK_HOPS):
            if modes.get(path) != LINK_MODE:
                return path if path in modes else None
            target = self.git.show_file(self.rev, path) or ""
            joined = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
            if not target or target.startswith("/") or joined == ".." or joined.startswith("../"):
                return None
            path = joined
        return None

    def read(self, path: str) -> str | None:
        if self.follow_links:
            resolved = self.resolve(path)
            if resolved is None:
                return None
            path = resolved
        return self.git.show_file(self.rev, path)

    def paths(self) -> frozenset[str]:
        return frozenset(self._entries())


def open_checkout(runner: Runner, source: str, workdir: Path) -> Git:
    """Clone ``source`` (URL, path or bundle) into ``workdir/repo``, or refresh it.

    The clone or fetch holds the checkout lock, so parallel runs never clone into the
    same directory or update its refs at the same time.
    """
    repo_dir = workdir / "repo"
    git = Git(runner, repo_dir)
    with git.lock:
        if (repo_dir / ".git").is_dir():
            checked(runner.run(["git", "-C", str(repo_dir), "fetch", "--quiet", "origin"]))
        else:
            if repo_dir.exists():
                shutil.rmtree(repo_dir)
            workdir.mkdir(parents=True, exist_ok=True)
            checked(runner.run(["git", "clone", "--quiet", source, str(repo_dir)]))
    return git

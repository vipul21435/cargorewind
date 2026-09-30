"""Build cache: reuse the image of a recipe instead of building it again.

The index is one JSON file (``build-index.json`` in the cache directory) that maps a
recipe hash to the image built from it: tag, image id, when it was built and how long
the build took. A lookup is a hit only when Docker still has an image under the indexed
tag (or the requested one) whose ``cargorewind.recipe`` label equals the hash, so a
deleted or retagged image is rebuilt, never trusted.

Two locks keep parallel runs apart. Each recipe has its own lock file, held from the
lookup to the end of the build: a second run of the same recipe waits (up to a
timeout) and then reuses the image the first one built, instead of building it twice.
Index writes take a short lock of their own and replace the file atomically; an index
that cannot be written only costs the next lookup. ``rebuild`` skips the lookup and
builds with ``docker build --no-cache``; the image it replaces becomes dangling and is
pruned with the project label filter, so images of other projects are never touched.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any

from cargorewind.backend import Backend, BuildResult
from cargorewind.dockerfile import PROJECT_LABEL, RECIPE_LABEL
from cargorewind.runner import CommandError, Runner, checked

INDEX_FILE = "build-index.json"
INDEX_LOCK = "build-index.lock"
LOCK_DIR = "build-locks"
INDEX_SCHEMA = 1
DEFAULT_LOCK_TIMEOUT = 7200.0

Log = Callable[[str], None]


class BuildCacheError(RuntimeError):
    """The build lock could not be taken in time."""


class FileLock:
    """An exclusive ``flock`` that waits at most ``timeout`` seconds.

    ``on_wait`` is called once when the lock is busy, so the caller can say why it is
    waiting. Separate ``FileLock`` objects exclude each other, in one process too.
    """

    def __init__(
        self,
        path: Path,
        timeout: float,
        *,
        poll: float = 0.2,
        on_wait: Callable[[], None] | None = None,
    ) -> None:
        self.path = path
        self.timeout = timeout
        self.poll = poll
        self.on_wait = on_wait
        self.waited = False
        self._fd: int | None = None

    def __enter__(self) -> FileLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if not self.waited and self.on_wait is not None:
                    self.on_wait()
                self.waited = True
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise BuildCacheError(
                        f"{self.path} is still locked after {self.timeout:g} s: another "
                        "cargorewind run is building the same recipe"
                    ) from None
                time.sleep(self.poll)
        self._fd = fd
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


@dataclass(frozen=True)
class CacheEntry:
    recipe: str
    tag: str
    image_id: str
    built_at: str
    build_seconds: float
    repo: str
    base_commit: str
    toolchain: str

    @classmethod
    def from_dict(cls, data: Any) -> CacheEntry | None:
        """The entry ``data`` describes, or None when a field is missing or mistyped.

        The index is data other cargorewind versions (or a stray editor) may have
        written, so a wrong type is as unusable as a missing key: ``build_seconds`` must
        be a number and every other field a string, or the entry is ignored.
        """
        if not isinstance(data, dict):
            return None
        values: dict[str, Any] = {}
        for spec in fields(cls):
            value = data.get(spec.name)
            if spec.name == "build_seconds":
                if isinstance(value, bool) or not isinstance(value, int | float):
                    return None
                value = float(value)
            elif not isinstance(value, str):
                return None
            values[spec.name] = value
        return cls(**values)


@dataclass(frozen=True)
class BuildRequest:
    """One image to reuse or build: its tag, recipe hash and what the index records."""

    tag: str
    recipe: str
    repo: str = ""
    base_commit: str = ""
    toolchain: str = ""


@dataclass(frozen=True)
class CachedBuild:
    result: BuildResult
    tag: str
    hit: bool
    reason: str


@dataclass(frozen=True)
class ImageInfo:
    image_id: str
    recipe: str  # the cargorewind.recipe label, "" when the image has none


@dataclass(frozen=True)
class PruneReport:
    docker_output: str
    stale: list[str]  # recipe hashes whose image no longer exists
    kept: int


class BuildCache:
    def __init__(
        self,
        directory: Path,
        runner: Runner,
        *,
        lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
        poll: float = 0.2,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.directory = directory
        self.index_path = directory / INDEX_FILE
        self.runner = runner
        self.lock_timeout = lock_timeout
        self.poll = poll
        self.now = now
        self.note = ""

    # The index

    def entries(self) -> dict[str, CacheEntry]:
        """Every readable entry; an unreadable or foreign index counts as empty."""
        try:
            data = json.loads(self.index_path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            self.note = f"unreadable build index {self.index_path} ignored ({exc})"
            return {}
        if not isinstance(data, dict) or data.get("schema_version") != INDEX_SCHEMA:
            self.note = f"build index {self.index_path} has another schema; ignored"
            return {}
        images = data.get("images")
        found: dict[str, CacheEntry] = {}
        for key, value in (images if isinstance(images, dict) else {}).items():
            entry = CacheEntry.from_dict(value)
            if entry is not None and entry.recipe == key:
                found[key] = entry
        return found

    def _update(self, change: Callable[[dict[str, CacheEntry]], None]) -> bool:
        """Apply ``change`` to the index under the index lock; False when not written."""
        try:
            with FileLock(self.directory / INDEX_LOCK, self.lock_timeout, poll=self.poll):
                entries = self.entries()
                change(entries)
                document = {
                    "schema_version": INDEX_SCHEMA,
                    "images": {k: asdict(v) for k, v in sorted(entries.items())},
                }
                partial = self.index_path.with_suffix(f".{os.getpid()}.tmp")
                try:
                    partial.write_text(json.dumps(document, indent=2) + "\n")
                    os.replace(partial, self.index_path)
                finally:
                    with contextlib.suppress(OSError):
                        partial.unlink(missing_ok=True)
        except (OSError, BuildCacheError) as exc:
            self.note = f"build index {self.index_path} not written ({exc})"
            return False
        return True

    # Docker

    def inspect(self, tag: str) -> ImageInfo | None:
        """The id and recipe label of the image tagged ``tag``, or None without one."""
        template = f'{{{{.Id}}}} {{{{index .Config.Labels "{RECIPE_LABEL}"}}}}'
        result = self.runner.run(["docker", "image", "inspect", "--format", template, tag])
        if not result.ok:
            return None
        image_id, _, label = result.stdout.strip().partition(" ")
        if not image_id:
            return None
        return ImageInfo(image_id, "" if label.strip() == "<no value>" else label.strip())

    def prune_dangling(self) -> str:
        """``docker image prune`` limited to this project's dangling images."""
        argv = ["docker", "image", "prune", "-f", "--filter", f"label={PROJECT_LABEL}"]
        result = checked(self.runner.run([*argv, "--filter", "dangling=true"]))
        return result.stdout.strip()

    def _prune_after_rebuild(self) -> str:
        """The prune's last output line, or why it failed: a cleanup step that fails (for
        example Docker's "a prune operation is already running") must not fail a run
        whose image is built and indexed."""
        try:
            pruned = self.prune_dangling()
        except CommandError as exc:
            detail = str(exc).splitlines()[-1] if str(exc) else "no detail"
            return f"dangling images not pruned ({detail})"
        return pruned.splitlines()[-1] if pruned else ""

    # Lookup and build

    def lookup(self, tag: str, recipe: str) -> CachedBuild | None:
        """A usable image of ``recipe``: the indexed tag first, then ``tag``."""
        entry = self.entries().get(recipe)
        tags = [entry.tag] if entry is not None else []
        tags += [tag] if tag not in tags else []
        for candidate in tags:
            info = self.inspect(candidate)
            if info is None or info.recipe != recipe:
                continue
            if entry is None or (entry.tag, entry.image_id) != (candidate, info.image_id):
                fresh = CacheEntry(
                    recipe,
                    candidate,
                    info.image_id,
                    entry.built_at if entry else "",
                    entry.build_seconds if entry else 0.0,
                    entry.repo if entry else "",
                    entry.base_commit if entry else "",
                    entry.toolchain if entry else "",
                )
                self._put(fresh)
                how = "found in Docker and indexed again"
            else:
                how = f"built {entry.built_at} in {entry.build_seconds:g} s"
            result = BuildResult(info.image_id, f"reused {candidate} ({info.image_id})\n")
            return CachedBuild(result, candidate, True, f"image {candidate} {how}")
        return None

    def _put(self, entry: CacheEntry) -> bool:
        def change(entries: dict[str, CacheEntry]) -> None:
            entries[entry.recipe] = entry

        return self._update(change)

    def build(
        self,
        backend: Backend,
        context: Path,
        request: BuildRequest,
        *,
        rebuild: bool = False,
        log: Log = lambda _: None,
    ) -> CachedBuild:
        """Reuse the image of ``request.recipe``, or build it under the recipe's lock."""

        def waiting() -> None:
            log(f"cache     waiting for another run that builds recipe {request.recipe[:16]}")

        lock = FileLock(
            self.directory / LOCK_DIR / f"{request.recipe}.lock",
            self.lock_timeout,
            poll=self.poll,
            on_wait=waiting,
        )
        try:
            lock.__enter__()
        except OSError as exc:  # an unwritable cache directory: build without the cache
            self.note = f"build cache {self.directory} unavailable ({exc})"
            result = backend.build(context, request.tag, no_cache=rebuild)
            return CachedBuild(result, request.tag, False, f"{self.note}; built without it")
        try:
            return self._build_locked(backend, context, request, rebuild)
        finally:
            lock.__exit__(None, None, None)

    def _build_locked(
        self, backend: Backend, context: Path, request: BuildRequest, rebuild: bool
    ) -> CachedBuild:
        if not rebuild:
            hit = self.lookup(request.tag, request.recipe)
            if hit is not None:
                return hit
        started = time.monotonic()
        result = backend.build(context, request.tag, no_cache=rebuild)
        seconds = round(time.monotonic() - started, 1)
        entry = CacheEntry(
            request.recipe,
            request.tag,
            result.image_id,
            self.now().isoformat(timespec="seconds"),
            seconds,
            request.repo,
            request.base_commit,
            request.toolchain,
        )
        written = self._put(entry)
        reason = "--rebuild: built with --no-cache" if rebuild else "no image for this recipe"
        if rebuild and (pruned := self._prune_after_rebuild()):
            reason += f"; {pruned}"
        if not written:
            reason += f"; {self.note}"
        return CachedBuild(result, request.tag, False, f"{reason}; built in {seconds:g} s")

    def prune(self) -> PruneReport:
        """Prune this project's dangling images, then drop index entries without an image."""
        output = self.prune_dangling()
        stale = [
            recipe
            for recipe, entry in self.entries().items()
            if (info := self.inspect(entry.tag)) is None or info.recipe != recipe
        ]

        def drop(entries: dict[str, CacheEntry]) -> None:
            for recipe in stale:
                entries.pop(recipe, None)

        if stale:
            self._update(drop)
        return PruneReport(output, stale, len(self.entries()))

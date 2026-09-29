"""crates.io version metadata from the sparse index, for date-bounded resolution.

Every crate has one file on https://index.crates.io with a JSON object per published
version: ``vers``, ``deps`` (name, req, kind, optional, target, and ``package`` when the
dependency is renamed), ``yanked``, ``rust_version`` and ``pubtime``, the publish time
(backfilled for old versions). That is all the pin loop needs, in one request per crate.

``DirectoryIndex`` reads files laid out like the sparse index (``ho/me/home``,
``3/s/syn``); tests and offline demos use recorded copies. ``SparseIndex`` fetches from
the network and keeps each answer in a JSON cache, refetched only when it is older than
the commit being rebuilt.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from cargorewind import __version__
from cargorewind.registry import HttpClient, RegistryError
from cargorewind.semver import SemverError, Version, VersionReq

INDEX_URL = "https://index.crates.io"
USER_AGENT = f"cargorewind/{__version__} (+https://github.com/vipul21435/cargorewind)"
CACHE_SCHEMA = 1


class CrateIndexError(LookupError):
    """The index has no usable entry for a crate."""


@dataclass(frozen=True)
class IndexDep:
    name: str  # the crate's own name (the ``package`` field when renamed)
    req: str
    kind: str  # normal, dev or build
    optional: bool = False
    target: str | None = None


@dataclass(frozen=True)
class IndexVersion:
    name: str
    vers: str
    yanked: bool
    pubtime: datetime | None
    deps: tuple[IndexDep, ...] = ()
    rust_version: str | None = None

    def version(self) -> Version | None:
        try:
            return Version.parse(self.vers)
        except SemverError:
            return None

    def requirement_on(self, crate: str) -> list[str]:
        """Requirements this version places on ``crate`` (normal and build dependencies)."""
        return [d.req for d in self.deps if d.name == crate and d.kind != "dev"]


def index_path(name: str) -> str:
    """Path of a crate's file in the sparse index layout."""
    lower = name.lower()
    if len(lower) <= 2:
        return f"{len(lower)}/{lower}"
    if len(lower) == 3:
        return f"3/{lower[0]}/{lower}"
    return f"{lower[:2]}/{lower[2:4]}/{lower}"


def _pubtime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def _dep(raw: dict[str, Any]) -> IndexDep:
    package = raw.get("package")
    name = package if isinstance(package, str) else str(raw.get("name", ""))
    target = raw.get("target")
    return IndexDep(
        name,
        str(raw.get("req", "*")),
        str(raw.get("kind") or "normal"),
        bool(raw.get("optional", False)),
        target if isinstance(target, str) else None,
    )


def parse_index_file(text: str) -> list[IndexVersion]:
    """Entries of one sparse index file (one JSON object per line)."""
    versions = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except ValueError as exc:
            raise CrateIndexError(f"index line is not JSON: {line[:60]!r}") from exc
        deps = raw.get("deps")
        rust_version = raw.get("rust_version")
        versions.append(
            IndexVersion(
                str(raw.get("name", "")),
                str(raw.get("vers", "")),
                bool(raw.get("yanked", False)),
                _pubtime(raw.get("pubtime")),
                tuple(_dep(d) for d in deps if isinstance(d, dict))
                if isinstance(deps, list)
                else (),
                rust_version if isinstance(rust_version, str) else None,
            )
        )
    return versions


class CrateIndex(Protocol):
    def versions(self, name: str) -> list[IndexVersion]: ...


class DirectoryIndex:
    """Index files on disk in the sparse layout (recorded fixtures, offline demos)."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def versions(self, name: str) -> list[IndexVersion]:
        path = self.root / index_path(name)
        if not path.is_file():
            raise CrateIndexError(f"{name}: not in the recorded index at {self.root}")
        return parse_index_file(path.read_text())


class SparseIndex:
    """The live sparse index behind a JSON cache (one file per crate)."""

    def __init__(
        self,
        http: HttpClient,
        cache_dir: Path,
        *,
        fresh_after: datetime | None = None,
        url: str = INDEX_URL,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.http = http
        self.cache_dir = cache_dir
        self.fresh_after = fresh_after
        self.url = url.rstrip("/")
        self.clock = clock
        self.fetched: list[str] = []  # crates fetched from the network in this run

    def _cache_file(self, name: str) -> Path:
        return self.cache_dir / "crates-index" / f"{index_path(name)}.json"

    def _cached(self, name: str) -> str | None:
        try:
            data = json.loads(self._cache_file(name).read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict) or data.get("schema") != CACHE_SCHEMA:
            return None
        fetched = _pubtime(data.get("fetched_at"))
        body = data.get("body")
        if fetched is None or not isinstance(body, str):
            return None
        if self.fresh_after is not None and fetched <= self.fresh_after:
            return None  # fetched before the commit: may miss versions published since
        return body

    def _store(self, name: str, body: str) -> None:
        path = self._cache_file(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = self.clock().isoformat().replace("+00:00", "Z")
        document = {"schema": CACHE_SCHEMA, "fetched_at": stamp, "body": body}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(document) + "\n")
        tmp.replace(path)

    def versions(self, name: str) -> list[IndexVersion]:
        body = self._cached(name)
        if body is None:
            url = f"{self.url}/{index_path(name)}"
            try:
                resp = self.http.request("GET", url, {"User-Agent": USER_AGENT})
            except RegistryError as exc:
                raise CrateIndexError(f"{name}: {exc}") from exc
            if resp.status == 404:
                raise CrateIndexError(f"{name}: no such crate on crates.io")
            if resp.status != 200:
                raise CrateIndexError(f"{name}: HTTP {resp.status} from {self.url}")
            body = resp.body.decode("utf-8", errors="replace")
            self._store(name, body)
            self.fetched.append(name)
        return parse_index_file(body)


def candidates(
    versions: list[IndexVersion],
    reqs: list[VersionReq],
    cutoff: datetime,
    exclude: frozenset[str] = frozenset(),
) -> list[IndexVersion]:
    """Versions that match every requirement, are not yanked and were published before
    ``cutoff``, newest first. Pre-releases need a requirement that names one."""
    reqs = reqs or [VersionReq(())]
    found: list[tuple[Version, IndexVersion]] = []
    for entry in versions:
        parsed = entry.version()
        if parsed is None or entry.yanked or entry.vers in exclude:
            continue
        if entry.pubtime is None or entry.pubtime >= cutoff:
            continue
        if all(req.matches(parsed) for req in reqs):
            found.append((parsed, entry))
    found.sort(key=lambda pair: pair[0].key(), reverse=True)
    return [entry for _, entry in found]

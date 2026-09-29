"""Read ``Cargo.lock``: its format version, its packages and the edges between them.

Format versions (the release notes of rust-lang/rust ``RELEASES.md`` name when each
became cargo's format; CargoRewind treats that release as the first that reads it):

- v1: no ``version`` key; checksums live in a ``[metadata]`` table and dependencies
  are written as ``"name version (source)"``. Every cargo reads it.
- v2: no ``version`` key; checksums move onto each ``[[package]]`` and dependencies
  are shortened to ``"name"`` when unambiguous. Cargo 1.41.
- v3: ``version = 3`` (git dependencies track the default branch correctly). Cargo 1.53.
- v4: ``version = 4`` (percent-encoded git URLs). Cargo 1.78.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from functools import cached_property
from typing import Any

CRATES_IO_SOURCES = frozenset(
    {
        "registry+https://github.com/rust-lang/crates.io-index",
        "sparse+https://index.crates.io/",
    }
)
# Oldest stable toolchain whose cargo reads each lockfile format version.
READ_MINIMUM: dict[int, tuple[int, int, int] | None] = {
    1: None,
    2: (1, 41, 0),
    3: (1, 53, 0),
    4: (1, 78, 0),
}


class LockfileError(ValueError):
    """The lockfile is not valid TOML or uses a format this tool does not know."""


@dataclass(frozen=True)
class LockedPackage:
    name: str
    version: str
    source: str | None  # None for workspace members and path dependencies
    dependencies: tuple[str, ...] = ()  # raw dependency strings, as written

    @property
    def key(self) -> tuple[str, str, str | None]:
        return (self.name, self.version, self.source)

    @property
    def from_crates_io(self) -> bool:
        return self.source in CRATES_IO_SOURCES

    @property
    def spec(self) -> str:
        """Package id spec for ``cargo update -p``; ``name:version`` works on every cargo."""
        return f"{self.name}:{self.version}"

    def __str__(self) -> str:
        return f"{self.name} {self.version}"


@dataclass(frozen=True)
class Lockfile:
    format_version: int
    packages: tuple[LockedPackage, ...]

    def registry_packages(self) -> list[LockedPackage]:
        return [p for p in self.packages if p.from_crates_io]

    @cached_property
    def _by_name(self) -> dict[str, list[LockedPackage]]:
        index: dict[str, list[LockedPackage]] = {}
        for package in self.packages:
            index.setdefault(package.name, []).append(package)
        return index

    @cached_property
    def _dependents(self) -> dict[tuple[str, str, str | None], list[LockedPackage]]:
        reverse: dict[tuple[str, str, str | None], list[LockedPackage]] = {}
        for package in self.packages:
            for dependency in package.dependencies:
                target = self.resolve(dependency)
                if target is not None:
                    reverse.setdefault(target.key, []).append(package)
        return reverse

    def resolve(self, dependency: str) -> LockedPackage | None:
        """The package a dependency string (``name [version] [(source)]``) points at."""
        source = None
        text = dependency.strip()
        if text.endswith(")") and " (" in text:
            text, source = text[:-1].split(" (", 1)
        parts = text.split()
        if not parts:
            return None
        found = self._by_name.get(parts[0], [])
        if len(parts) > 1:
            found = [p for p in found if p.version == parts[1]]
        if source is not None:
            found = [p for p in found if p.source == source]
        return found[0] if len(found) == 1 else None

    def dependents(self, package: LockedPackage) -> list[LockedPackage]:
        """Packages whose dependency lists point at ``package``."""
        return list(self._dependents.get(package.key, []))

    def find(self, name: str, version: str) -> LockedPackage | None:
        return next((p for p in self.packages if (p.name, p.version) == (name, version)), None)


def _detect_version(data: dict[str, Any], packages: list[dict[str, Any]]) -> int:
    version = data.get("version")
    if version is not None:
        if not isinstance(version, int) or version not in READ_MINIMUM:
            raise LockfileError(f"unknown Cargo.lock format version {version!r}")
        return version
    if any("checksum" in p for p in packages):
        return 2
    # [metadata] checksums or long-form dependencies are v1; a lockfile with neither has
    # no registry packages, which every cargo reads the same way.
    return 1


def parse_lockfile(text: str) -> Lockfile:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise LockfileError(f"Cargo.lock is not valid TOML: {exc}") from exc
    raw = data.get("package", [])
    tables = [p for p in raw if isinstance(p, dict)] if isinstance(raw, list) else []
    root = data.get("root")  # before cargo 1.22 the root package had its own table
    if isinstance(root, dict):
        tables.insert(0, root)
    packages = []
    for table in tables:
        name, version = table.get("name"), table.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            raise LockfileError("Cargo.lock has a package without a name or version")
        source = table.get("source")
        deps = table.get("dependencies", [])
        packages.append(
            LockedPackage(
                name,
                version,
                source if isinstance(source, str) else None,
                tuple(d for d in deps if isinstance(d, str)) if isinstance(deps, list) else (),
            )
        )
    return Lockfile(_detect_version(data, tables), tuple(packages))


def format_version(text: str) -> int:
    """The format version of a Cargo.lock text."""
    return parse_lockfile(text).format_version

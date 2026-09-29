"""Classify files by their role in a Cargo layout.

Packages come from every ``Cargo.toml`` with a ``[package]`` table; ``[workspace]
members`` globs (with ``exclude``) decide which workspace, if any, lists each one. A
package inside a ``tests/`` directory of an enclosing package or workspace that no
enclosing workspace lists is a fixture crate: test data of that package, not a package.
Targets come from ``[lib]``, ``[[bin]]``, ``[[test]]``, ``[[bench]]``, ``[[example]]``
and ``package.build`` (custom paths included) plus Cargo's auto-discovery
(``src/lib.rs``, ``src/main.rs``, ``src/bin/``, ``tests/``, ``benches/``, ``examples/``,
``build.rs``), honoring ``autotests = false`` and friends.

A file gets its role by walking the module tree from each target root and following
``mod name;`` declarations (``name.rs``, ``name/mod.rs`` or ``#[path]``) the way rustc
does. A module file reached only through ``#[cfg(test)]`` declarations, or starting with
``#![cfg(test)]``, is test code as a whole. Files the walk does not reach fall back to
path conventions. The walk only descends into directories that lead to a wanted file,
so a large workspace costs a handful of file reads.
"""

from __future__ import annotations

import posixpath
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from fnmatch import fnmatchcase
from typing import Any, Protocol

from cargorewind.rustscan import FileScan, ModuleDecl, TestRegion, scan_source


class Role(StrEnum):
    TEST = "test"
    SOURCE = "source"
    BENCH = "bench"
    EXAMPLE = "example"
    BUILD_SCRIPT = "build-script"
    MANIFEST = "manifest"
    LOCKFILE = "lockfile"
    OTHER = "other"


KIND_ROLES = {
    "lib": Role.SOURCE,
    "bin": Role.SOURCE,
    "build": Role.BUILD_SCRIPT,
    "test": Role.TEST,
    "bench": Role.BENCH,
    "example": Role.EXAMPLE,
}
# A file compiled by several targets reports the most "production" one.
ROLE_PRIORITY = (Role.SOURCE, Role.BUILD_SCRIPT, Role.BENCH, Role.EXAMPLE, Role.TEST)
# (kind, manifest table, auto-discovery flag, conventional directory)
_TARGET_TABLES = (
    ("bin", "bin", "autobins", "src/bin"),
    ("test", "test", "autotests", "tests"),
    ("bench", "bench", "autobenches", "benches"),
    ("example", "example", "autoexamples", "examples"),
)
_CONVENTION_DIRS = {
    "tests": Role.TEST,
    "benches": Role.BENCH,
    "examples": Role.EXAMPLE,
    "src": Role.SOURCE,
}


class SourceTree(Protocol):
    """Read access to one revision of a repository."""

    def read(self, path: str) -> str | None: ...

    def paths(self) -> frozenset[str]: ...


class MemoryTree:
    """A ``SourceTree`` over an in-memory mapping of path to text."""

    def __init__(self, files: Mapping[str, str]) -> None:
        self.files = dict(files)

    def read(self, path: str) -> str | None:
        return self.files.get(path)

    def paths(self) -> frozenset[str]:
        return frozenset(self.files)


@dataclass(frozen=True)
class Target:
    kind: str  # lib, bin, test, bench, example, build
    name: str
    path: str

    @property
    def label(self) -> str:
        return self.kind if self.kind in ("lib", "build") else f"{self.kind}:{self.name}"


@dataclass(frozen=True)
class Package:
    root: str  # repository-relative directory, "" for the repository root
    name: str
    workspace: str | None  # root of the workspace that lists it, None when standalone
    targets: tuple[Target, ...]

    @property
    def display_root(self) -> str:
        return self.root or "."


@dataclass(frozen=True)
class FileInfo:
    path: str
    role: Role
    package: str | None
    target: str | None
    test_code: bool  # the whole file only builds for tests
    reason: str


@dataclass(frozen=True)
class _Reach:
    role: Role
    package: str
    target: str
    test_only: bool
    reason: str


def _join(folder: str, rel: str) -> str:
    joined = posixpath.normpath(posixpath.join(folder, rel)) if folder else posixpath.normpath(rel)
    return "" if joined == "." else joined


def _stem(path: str) -> str:
    return posixpath.splitext(posixpath.basename(path))[0]


def _match_parts(pattern: list[str], parts: list[str]) -> bool:
    if not pattern:
        return not parts
    if pattern[0] == "**":
        return any(_match_parts(pattern[1:], parts[k:]) for k in range(len(parts) + 1))
    return (
        bool(parts) and fnmatchcase(parts[0], pattern[0]) and _match_parts(pattern[1:], parts[1:])
    )


def glob_match(pattern: str, path: str) -> bool:
    """Cargo-style member glob (``*``, ``?``, ``[..]``, ``**``) over whole path components."""
    pat = [p for p in pattern.split("/") if p not in ("", ".")]
    parts = [p for p in path.split("/") if p]
    return _match_parts(pat, parts)


def _relative(path: str, folder: str) -> str | None:
    """``path`` relative to ``folder``, or None when it lies outside it."""
    if not folder:
        return path
    if path == folder:
        return ""
    return path[len(folder) + 1 :] if path.startswith(folder + "/") else None


def _fixture_owner(root: str, manifest_dirs: list[str], workspace: str | None) -> str | None:
    """Manifest directory whose ``tests/`` holds the package at ``root``, if it is a fixture.

    A package that an enclosing workspace lists is a real member wherever it sits. One
    that only its own ``[workspace]`` (or none) lists, below a ``tests`` directory of an
    enclosing package or workspace, is test data (for example ``tests/fixtures/<name>``).
    """
    if workspace not in (None, root):
        return None
    for folder in sorted(manifest_dirs, key=len, reverse=True):
        rel = _relative(root, folder)
        if folder != root and rel is not None and "tests" in rel.split("/"):
            return folder
    return None


def _strings(value: Any) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _tables(value: Any) -> list[dict[str, Any]]:
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


class Layout:
    """Packages, targets and file roles of one repository revision."""

    def __init__(self, tree: SourceTree, wanted: Iterable[str] | None = None) -> None:
        self.tree = tree
        self.paths = tree.paths()
        self.notes: list[str] = []
        self.fixtures: dict[str, str] = {}  # fixture crate root -> the manifest dir it is in
        self._scans: dict[str, FileScan] = {}
        self._reached: dict[str, list[_Reach]] = {}
        # (declaring file, declaration, module file) for every resolved ``mod name;``
        self._decls: set[tuple[str, ModuleDecl, str]] = set()
        self._wanted = None if wanted is None else frozenset(wanted)
        self._wanted_dirs: set[str] = set()
        for path in self._wanted or ():
            folder = posixpath.dirname(path)
            while True:
                self._wanted_dirs.add(folder)
                if not folder:
                    break
                folder = posixpath.dirname(folder)
        self._subdirs: dict[str, set[str]] = {}
        self._files_in: dict[str, list[str]] = {}
        for path in sorted(self.paths):
            folder = posixpath.dirname(path)
            self._files_in.setdefault(folder, []).append(path)
            child = folder
            while child:
                parent = posixpath.dirname(child)
                self._subdirs.setdefault(parent, set()).add(child)
                child = parent
        self.packages = self._discover()
        self._target_dirs: dict[str, tuple[Role, str, str]] = {}
        for package in self.packages:
            for target in package.targets:
                self._note_target_dir(package, target)
                self._walk(package, target)

    # Manifests and targets

    def _discover(self) -> list[Package]:
        manifests: dict[str, dict[str, Any]] = {}
        for path in sorted(p for p in self.paths if posixpath.basename(p) == "Cargo.toml"):
            try:
                manifests[posixpath.dirname(path)] = tomllib.loads(self.tree.read(path) or "")
            except tomllib.TOMLDecodeError as exc:
                self.notes.append(f"{path}: not valid TOML ({exc}); using path conventions")
        package_roots = [root for root, data in manifests.items() if "package" in data]
        workspace_of: dict[str, str] = {}
        for ws_root, data in manifests.items():
            workspace = data.get("workspace")
            if not isinstance(workspace, dict):
                continue
            members = _strings(workspace.get("members"))
            excludes = [_join("", e) for e in _strings(workspace.get("exclude"))]
            for root in package_roots:
                rel = _relative(root, ws_root)
                if rel is None or root in workspace_of:
                    continue
                excluded = any(rel == e or rel.startswith(e + "/") for e in excludes if e)
                listed = rel == "" or any(glob_match(m, rel) for m in members)
                if listed and not excluded:
                    workspace_of[root] = ws_root
        packages: list[Package] = []
        for root in package_roots:
            owner = _fixture_owner(root, list(manifests), workspace_of.get(root))
            if owner is not None:
                self.fixtures[root] = owner
                continue
            packages.append(
                Package(
                    root,
                    self._package_name(root, manifests[root]),
                    workspace_of.get(root),
                    self._targets(root, manifests[root]),
                )
            )
        return packages

    @staticmethod
    def _package_name(root: str, data: dict[str, Any]) -> str:
        package = data.get("package")
        name = package.get("name") if isinstance(package, dict) else None
        return name if isinstance(name, str) else (posixpath.basename(root) or "package")

    def _auto(self, root: str, folder: str) -> list[tuple[str, str]]:
        """(name, path) of auto-discovered targets: ``folder/*.rs``, ``folder/*/main.rs``."""
        base = _join(root, folder)
        found = [(_stem(p), p) for p in self._files_in.get(base, []) if p.endswith(".rs")]
        for sub in sorted(self._subdirs.get(base, ())):
            main = f"{sub}/main.rs"
            if main in self.paths:
                found.append((posixpath.basename(sub), main))
        return found

    def _targets(self, root: str, data: dict[str, Any]) -> tuple[Target, ...]:
        package = data.get("package")
        meta: dict[str, Any] = package if isinstance(package, dict) else {}
        name = self._package_name(root, data)
        found: dict[str, Target] = {}

        def add(kind: str, target_name: str, rel: str) -> bool:
            path = _join(root, rel)
            if path not in self.paths:
                return False
            found.setdefault(path, Target(kind, target_name, path))
            return True

        lib = data.get("lib")
        lib_table: dict[str, Any] = lib if isinstance(lib, dict) else {}
        lib_path = lib_table.get("path")
        lib_name = lib_table.get("name")
        add(
            "lib",
            lib_name if isinstance(lib_name, str) else name.replace("-", "_"),
            lib_path if isinstance(lib_path, str) else "src/lib.rs",
        )
        build = meta.get("build")
        if isinstance(build, str):
            add("build", "build-script", build)
        elif build is not False:
            add("build", "build-script", "build.rs")
        for kind, table, auto_key, folder in _TARGET_TABLES:
            for entry in _tables(data.get(table)):
                entry_name = entry.get("name")
                entry_path = entry.get("path")
                if isinstance(entry_path, str):
                    add(
                        kind,
                        entry_name if isinstance(entry_name, str) else _stem(entry_path),
                        entry_path,
                    )
                elif isinstance(entry_name, str):
                    if not add(kind, entry_name, f"{folder}/{entry_name}.rs"):
                        add(kind, entry_name, f"{folder}/{entry_name}/main.rs")
                    if kind == "bin" and entry_name == name:
                        add(kind, entry_name, "src/main.rs")
            if meta.get(auto_key, True) is not False:
                if kind == "bin":
                    add("bin", name, "src/main.rs")
                for target_name, path in self._auto(root, folder):
                    add(kind, target_name, _relative(path, root) or path)
        return tuple(sorted(found.values(), key=lambda t: (t.kind, t.path)))

    def _note_target_dir(self, package: Package, target: Target) -> None:
        """Remember a custom target directory, so its data files share the role."""
        folder = posixpath.dirname(target.path)
        rel = _relative(folder, package.root)
        if rel in (None, "", "src") or (rel or "").split("/")[0] in _CONVENTION_DIRS:
            return
        self._target_dirs.setdefault(folder, (KIND_ROLES[target.kind], package.root, target.label))

    # Module tree walk

    def _scan(self, path: str) -> FileScan | None:
        if path not in self._scans:
            src = self.tree.read(path) if path in self.paths else None
            if src is None:
                return None
            self._scans[path] = scan_source(src)
        return self._scans[path]

    def _relevant(self, path: str, mod_rs: bool) -> bool:
        if self._wanted is None or path in self._wanted:
            return True
        folder = posixpath.dirname(path)
        return (folder if mod_rs else _join(folder, _stem(path))) in self._wanted_dirs

    def _resolve(self, parent: str, module_dir: str, decl: ModuleDecl) -> tuple[str, bool] | None:
        """File that ``decl`` in ``parent`` loads, and whether it acts as a mod.rs file."""
        inline = "/".join(decl.inline_path)
        if decl.path_attr is not None:
            anchor = _join(module_dir, inline) if inline else posixpath.dirname(parent)
            path = _join(anchor, decl.path_attr)
            return (path, True) if path in self.paths else None  # #[path] files act as mod.rs
        folder = _join(module_dir, inline)
        flat = _join(folder, f"{decl.name}.rs")
        if flat in self.paths:
            return flat, False
        nested = _join(folder, f"{decl.name}/mod.rs")
        return (nested, True) if nested in self.paths else None

    def _walk(self, package: Package, target: Target) -> None:
        role = KIND_ROLES[target.kind]
        stack = [(target.path, True, False, f"{target.label} target root")]
        seen: set[tuple[str, bool]] = set()
        while stack:
            path, mod_rs, test_only, reason = stack.pop()
            if (path, test_only) in seen or not self._relevant(path, mod_rs):
                continue
            seen.add((path, test_only))
            scan = self._scan(path)
            if scan is None:
                continue
            test_only = test_only or scan.file_cfg_test
            if scan.file_cfg_test:
                reason += ", file starts with #![cfg(test)]"
            reach = _Reach(role, package.root, target.label, test_only, reason)
            self._reached.setdefault(path, []).append(reach)
            folder = posixpath.dirname(path)
            module_dir = folder if mod_rs else _join(folder, _stem(path))
            for decl in scan.modules:
                resolved = self._resolve(path, module_dir, decl)
                if resolved is None:
                    continue
                child, child_mod_rs = resolved
                self._decls.add((path, decl, child))
                why = f"module `{decl.name}` of {target.label}, declared at {path}:{decl.line}"
                if decl.cfg_test:
                    why += " under cfg(test)"
                stack.append((child, child_mod_rs, test_only or decl.cfg_test, why))

    def test_module_decls(self, path: str) -> list[TestRegion]:
        """Plain ``mod name;`` declarations in ``path`` that load a test-only file.

        A module file that starts with ``#![cfg(test)]`` only builds for tests, so the
        declaration that loads it belongs with it in the test patch.
        """
        found: set[TestRegion] = set()
        for parent, decl, child in self._decls:
            if parent != path or decl.cfg_test:
                continue
            scan = self._scan(child)
            file_region = (
                next((r for r in scan.regions if r.kind == "file"), None) if scan else None
            )
            if file_region is not None:
                start, end = decl.span
                found.add(TestRegion(start, end, "module-decl", decl.name, file_region.cfg))
        return sorted(found, key=lambda r: (r.start_line, r.end_line))

    def declared_in(self, path: str) -> list[str]:
        """Files whose ``mod`` declarations load ``path`` (as far as the walk went)."""
        return sorted({parent for parent, _, child in self._decls if child == path})

    # Classification

    def fixture_of(self, path: str) -> str | None:
        """Root of the fixture crate that contains ``path``, if any."""
        found = [root for root in self.fixtures if _relative(path, root) is not None]
        return max(found, key=len) if found else None

    def package_of(self, path: str) -> Package | None:
        best: Package | None = None
        for package in self.packages:
            if _relative(path, package.root) is not None and (
                best is None or len(package.root) > len(best.root)
            ):
                best = package
        return best

    def classify(self, path: str) -> FileInfo:
        package = self.package_of(path)
        package_root = package.display_root if package else None
        name = posixpath.basename(path)
        fixture = self.fixture_of(path)
        if fixture is not None and path not in self._reached:
            owner = self.fixtures[fixture] or "."
            reason = f"in fixture crate {fixture} (under tests/ of {owner}, no workspace lists it)"
            return FileInfo(path, Role.TEST, package_root, None, True, reason)
        if name == "Cargo.lock":
            return FileInfo(path, Role.LOCKFILE, package_root, None, False, "Cargo.lock")
        if name == "Cargo.toml":
            return FileInfo(path, Role.MANIFEST, package_root, None, False, "Cargo.toml")
        reaches = self._reached.get(path)
        if reaches:
            best = min(reaches, key=lambda r: (ROLE_PRIORITY.index(r.role), r.test_only))
            test_code = all(r.test_only or r.role is Role.TEST for r in reaches)
            return FileInfo(
                path, best.role, best.package or ".", best.target, test_code, best.reason
            )
        return self._by_convention(path, package)

    def _by_convention(self, path: str, package: Package | None) -> FileInfo:
        root = package.display_root if package else None
        folder = posixpath.dirname(path)
        while folder:
            if folder in self._target_dirs:
                role, owner, label = self._target_dirs[folder]
                reason = f"in the directory of custom target {label}"
                return FileInfo(path, role, owner or ".", label, role is Role.TEST, reason)
            folder = posixpath.dirname(folder)
        rel = _relative(path, package.root) if package else path
        parts = (rel or path).split("/")
        dirs = parts[:-1]
        if "tests" in dirs[1:]:
            # Data next to test modules (src/tests/data/..., benches/tests/...): the module
            # walk cannot reach it, and only tests read it.
            return FileInfo(path, Role.TEST, root, None, True, "inside a tests/ directory")
        if dirs and dirs[0] in _CONVENTION_DIRS:
            role = _CONVENTION_DIRS[dirs[0]]
            return FileInfo(path, role, root, None, role is Role.TEST, f"under {dirs[0]}/")
        if parts == ["build.rs"]:
            return FileInfo(path, Role.BUILD_SCRIPT, root, None, False, "build.rs")
        return FileInfo(path, Role.OTHER, root, None, False, "not part of a Cargo target")

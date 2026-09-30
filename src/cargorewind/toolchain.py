"""Infer the Rust toolchain of a historical commit and explain every decision.

Order of precedence:

1. A toolchain file at the base commit. ``rust-toolchain.toml`` is TOML with a
   ``[toolchain]`` table (``channel``, ``components``, ``targets``, ``profile``); the legacy
   ``rust-toolchain`` file is one bare channel line or the same TOML. When both exist,
   rustup reads the legacy file (and warns), so CargoRewind does too. An exact version is
   used as is, ``stable-YYYY-MM-DD`` becomes the stable release of that day, and dated
   ``nightly-YYYY-MM-DD`` or ``beta-YYYY-MM-DD`` channels are installed with rustup on a
   pinned stable image. An undated ``nightly`` or ``beta`` becomes the channel of the day
   before the commit.
2. Otherwise (no file, or an undated ``stable``): the newest stable release published on
   an earlier UTC day than the fix commit.
3. Floors from the manifests of the root package and the root workspace's members: every
   ``package.rust-version`` (MSRV, including ``rust-version.workspace = true`` inherited
   from ``[workspace.package]``), the minimum release of every edition in use (2018 ->
   1.31, 2021 -> 1.56, 2024 -> 1.85) and 1.64 when a manifest inherits from the
   workspace. A stable result below the highest floor is raised to it; a dated channel
   is kept, with a warning, because raising it would change the channel.
"""

from __future__ import annotations

import posixpath
import re
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from cargorewind.layout import SourceTree, glob_match
from cargorewind.lockfile import READ_MINIMUM, LockfileError, parse_lockfile

STABLE_RELEASES: tuple[tuple[str, str], ...] = (
    ("1.0.0", "2015-05-15"),
    ("1.1.0", "2015-06-25"),
    ("1.2.0", "2015-08-07"),
    ("1.3.0", "2015-09-17"),
    ("1.4.0", "2015-10-29"),
    ("1.5.0", "2015-12-10"),
    ("1.6.0", "2016-01-21"),
    ("1.7.0", "2016-03-03"),
    ("1.8.0", "2016-04-14"),
    ("1.9.0", "2016-05-26"),
    ("1.10.0", "2016-07-07"),
    ("1.11.0", "2016-08-18"),
    ("1.12.0", "2016-09-29"),
    ("1.12.1", "2016-10-20"),
    ("1.13.0", "2016-11-10"),
    ("1.14.0", "2016-12-22"),
    ("1.15.0", "2017-02-02"),
    ("1.15.1", "2017-02-09"),
    ("1.16.0", "2017-03-16"),
    ("1.17.0", "2017-04-27"),
    ("1.18.0", "2017-06-08"),
    ("1.19.0", "2017-07-20"),
    ("1.20.0", "2017-08-31"),
    ("1.21.0", "2017-10-12"),
    ("1.22.0", "2017-11-22"),
    ("1.22.1", "2017-11-22"),
    ("1.23.0", "2018-01-04"),
    ("1.24.0", "2018-02-15"),
    ("1.24.1", "2018-03-01"),
    ("1.25.0", "2018-03-29"),
    ("1.26.0", "2018-05-10"),
    ("1.26.1", "2018-05-29"),
    ("1.26.2", "2018-06-05"),
    ("1.27.0", "2018-06-21"),
    ("1.27.1", "2018-07-10"),
    ("1.27.2", "2018-07-20"),
    ("1.28.0", "2018-08-02"),
    ("1.29.0", "2018-09-13"),
    ("1.29.1", "2018-09-25"),
    ("1.29.2", "2018-10-11"),
    ("1.30.0", "2018-10-25"),
    ("1.30.1", "2018-11-08"),
    ("1.31.0", "2018-12-06"),
    ("1.31.1", "2018-12-20"),
    ("1.32.0", "2019-01-17"),
    ("1.33.0", "2019-02-28"),
    ("1.34.0", "2019-04-11"),
    ("1.34.1", "2019-04-25"),
    ("1.34.2", "2019-05-14"),
    ("1.35.0", "2019-05-23"),
    ("1.36.0", "2019-07-04"),
    ("1.37.0", "2019-08-15"),
    ("1.38.0", "2019-09-26"),
    ("1.39.0", "2019-11-07"),
    ("1.40.0", "2019-12-19"),
    ("1.41.0", "2020-01-30"),
    ("1.41.1", "2020-02-27"),
    ("1.42.0", "2020-03-12"),
    ("1.43.0", "2020-04-23"),
    ("1.43.1", "2020-05-07"),
    ("1.44.0", "2020-06-04"),
    ("1.44.1", "2020-06-18"),
    ("1.45.0", "2020-07-16"),
    ("1.45.1", "2020-07-30"),
    ("1.45.2", "2020-08-03"),
    ("1.46.0", "2020-08-27"),
    ("1.47.0", "2020-10-08"),
    ("1.48.0", "2020-11-19"),
    ("1.49.0", "2020-12-31"),
    ("1.50.0", "2021-02-11"),
    ("1.51.0", "2021-03-25"),
    ("1.52.0", "2021-05-06"),
    ("1.52.1", "2021-05-10"),
    ("1.53.0", "2021-06-17"),
    ("1.54.0", "2021-07-29"),
    ("1.55.0", "2021-09-09"),
    ("1.56.0", "2021-10-21"),
    ("1.56.1", "2021-11-01"),
    ("1.57.0", "2021-12-02"),
    ("1.58.0", "2022-01-13"),
    ("1.58.1", "2022-01-20"),
    ("1.59.0", "2022-02-24"),
    ("1.60.0", "2022-04-07"),
    ("1.61.0", "2022-05-19"),
    ("1.62.0", "2022-06-30"),
    ("1.62.1", "2022-07-19"),
    ("1.63.0", "2022-08-11"),
    ("1.64.0", "2022-09-22"),
    ("1.65.0", "2022-11-03"),
    ("1.66.0", "2022-12-15"),
    ("1.66.1", "2023-01-10"),
    ("1.67.0", "2023-01-26"),
    ("1.67.1", "2023-02-09"),
    ("1.68.0", "2023-03-09"),
    ("1.68.1", "2023-03-23"),
    ("1.68.2", "2023-03-28"),
    ("1.69.0", "2023-04-20"),
    ("1.70.0", "2023-06-01"),
    ("1.71.0", "2023-07-13"),
    ("1.71.1", "2023-08-03"),
    ("1.72.0", "2023-08-24"),
    ("1.72.1", "2023-09-19"),
    ("1.73.0", "2023-10-05"),
    ("1.74.0", "2023-11-16"),
    ("1.74.1", "2023-12-07"),
    ("1.75.0", "2023-12-28"),
    ("1.76.0", "2024-02-08"),
    ("1.77.0", "2024-03-21"),
    ("1.77.1", "2024-03-28"),
    ("1.77.2", "2024-04-09"),
    ("1.78.0", "2024-05-02"),
    ("1.79.0", "2024-06-13"),
    ("1.80.0", "2024-07-25"),
    ("1.80.1", "2024-08-08"),
    ("1.81.0", "2024-09-05"),
    ("1.82.0", "2024-10-17"),
    ("1.83.0", "2024-11-28"),
    ("1.84.0", "2025-01-09"),
    ("1.84.1", "2025-01-30"),
    ("1.85.0", "2025-02-20"),
    ("1.85.1", "2025-03-18"),
    ("1.86.0", "2025-04-03"),
    ("1.87.0", "2025-05-15"),
    ("1.88.0", "2025-06-26"),
    ("1.89.0", "2025-08-07"),
    ("1.90.0", "2025-09-18"),
    ("1.91.0", "2025-10-30"),
    ("1.91.1", "2025-11-10"),
    ("1.92.0", "2025-12-11"),
    ("1.93.0", "2026-01-22"),
    ("1.93.1", "2026-02-12"),
    ("1.94.0", "2026-03-05"),
    ("1.94.1", "2026-03-26"),
    ("1.95.0", "2026-04-16"),
    ("1.96.0", "2026-05-28"),
    ("1.96.1", "2026-06-30"),
    ("1.97.0", "2026-07-09"),
    ("1.97.1", "2026-07-16"),
    ("1.98.0", "2026-08-20"),
    ("1.98.1", "2026-09-03"),
)

# Multi-arch index digests of the official rust:<version>-slim images, resolved with
# `docker buildx imagetools inspect rust:<version>-slim` on 2026-09-30.
IMAGE_DIGESTS: dict[str, str] = {
    "1.39.0": "sha256:b47dd7b5f59bea2bc19ac18e81cc6b5b3cfe6c4e40082cab09604b296bca2652",
    "1.56.1": "sha256:cc2b5c03d4acf19be7fa8155cae8abbc8c3aa893a5d177ceb4921a3cbbb190da",
    "1.68.0": "sha256:85099324ff518e0aa14b7b80529d1cdd934ff92a344bdd961b7a7feba1a6f3bf",
    "1.70.0": "sha256:d1f62de1372e7103b9973848c3f873abfb73a6668ce4b0af2fe57fd9e32178b8",
    "1.73.0": "sha256:666012b6779ebb6be2acb771b8627716662cf699502e734652c4799ae4199691",
    "1.75.0": "sha256:70c2a016184099262fd7cee46f3d35fec3568c45c62f87e37f7f665f766b1f74",
    "1.80.1": "sha256:907ff4b3ee7df57149ffee04f606e0a08b9b2ed3507f00a19cf3c9c0f74b7681",
    "1.85.1": "sha256:9f841bbe9e7d8e37ceb96ed907265a3a0df7f44e3737d0b100e7907a679acb36",
    "1.90.0": "sha256:7fa728f3678acf5980d5db70960cf8491aff9411976789086676bdf0c19db39e",
    "1.98.1": "sha256:4cd829461bd5c4d511c32e269da9cb8929223b666519d8004e35fc8d1d771ab7",
}

# Dated channels are installed with rustup on this pinned stable image (the newest in
# the offline digest table, so its rustup understands every channel manifest).
CHANNEL_HOST_VERSION = "1.98.1"

# First stable release of each edition.
EDITION_MINIMUM: dict[str, str] = {
    "2015": "1.0.0",
    "2018": "1.31.0",
    "2021": "1.56.0",
    "2024": "1.85.0",
}
# `key.workspace = true` in a member manifest needs cargo 1.64.
WORKSPACE_INHERITANCE_MINIMUM = "1.64.0"

TOOLCHAIN_FILES = ("rust-toolchain", "rust-toolchain.toml")
PROFILES = ("minimal", "default", "complete")
DEPENDENCY_TABLES = ("dependencies", "dev-dependencies", "build-dependencies")
TARGET_TABLES = ("bin", "test", "bench", "example")

# Every pattern below is applied with fullmatch: "$" would also accept a trailing
# newline, and these strings end up in Dockerfile lines.
_EXACT = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")
# A host triple starts with its architecture (x86_64, i686, aarch64, ...), never with a
# digit, so a date without zero padding ("nightly-2020-1-01") is not read as a host.
_CHANNEL = re.compile(
    r"(?P<name>stable|beta|nightly|\d+\.\d+(?:\.\d+)?)"
    r"(?:-(?P<date>\d{4}-\d{2}-\d{2}))?"
    r"(?:-(?P<host>[a-z][a-z0-9_]*(?:-[a-z0-9_.]+){1,3}))?"
)

_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")

Version = tuple[int, int, int]


class ToolchainError(ValueError):
    """The toolchain cannot be chosen or has no pinned image."""


@dataclass(frozen=True)
class Decision:
    """One step of the toolchain inference: what was decided and why."""

    step: str
    outcome: str
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"step": self.step, "outcome": self.outcome, "reason": self.reason}


@dataclass(frozen=True)
class ToolchainSpec:
    """What a toolchain file asks for."""

    channel: str | None
    components: tuple[str, ...] = ()
    targets: tuple[str, ...] = ()
    profile: str | None = None


@dataclass(frozen=True)
class Floor:
    """A minimum stable version required by a manifest."""

    step: str  # msrv, edition or manifest
    version: Version
    reason: str


@dataclass(frozen=True)
class Toolchain:
    """The chosen toolchain and the stable image it runs on."""

    version: str  # rustup toolchain name: "1.39.0" or "nightly-2020-01-01"
    source: str  # release-date, rust-toolchain, rust-toolchain.toml, rust-version, ...
    reason: str
    image_version: str = ""  # the rust:<image_version>-slim image; defaults to version
    install: bool = False  # a dated channel installed with rustup on top of the image
    components: tuple[str, ...] = ()
    targets: tuple[str, ...] = ()
    profile: str | None = None
    toolchain_file: str | None = None  # the file rustup would obey in the checkout
    decisions: tuple[Decision, ...] = field(default=(), compare=False)

    def __post_init__(self) -> None:
        if not self.image_version:
            object.__setattr__(self, "image_version", self.version)

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "source": self.source,
            "reason": self.reason,
            "image_version": self.image_version,
            "rustup_install": self.install,
            "components": list(self.components),
            "targets": list(self.targets),
            "profile": self.profile,
            "toolchain_file": self.toolchain_file,
            "decisions": [d.as_dict() for d in self.decisions],
        }


def _key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def _triple(version: str) -> Version:
    major, minor, patch = (*_key(version), 0, 0)[:3]
    return (major, minor, patch)


def _fmt(version: Version) -> str:
    return ".".join(str(part) for part in version)


def release_date(version: str) -> date:
    for name, day in STABLE_RELEASES:
        if name == version:
            return date.fromisoformat(day)
    raise ToolchainError(f"unknown stable release: {version}")


def newest_stable_before(when: date) -> str:
    """Newest stable release published strictly before ``when``."""
    candidates = [v for v, day in STABLE_RELEASES if date.fromisoformat(day) < when]
    if not candidates:
        raise ToolchainError(f"no stable Rust release before {when.isoformat()}")
    return max(candidates, key=_key)


def stable_on(when: date) -> str:
    """The ``stable`` channel as of ``when``: newest release published on or before it."""
    return newest_stable_before(when + timedelta(days=1))


def normalize_version(channel: str) -> str | None:
    """``1.39`` -> newest ``1.39.x``; ``1.39.0`` -> itself; anything else -> None."""
    match = _EXACT.fullmatch(channel)
    if match is None:
        return None
    if match.group(3) is not None:
        release_date(channel)
        return channel
    prefix = f"{match.group(1)}.{match.group(2)}."
    patch_releases = [v for v, _ in STABLE_RELEASES if v.startswith(prefix)]
    if not patch_releases:
        raise ToolchainError(f"unknown stable release: {channel}")
    return max(patch_releases, key=_key)


def parse_version_requirement(text: str) -> Version | None:
    """``package.rust-version`` (``1.70`` or ``1.70.1``) as a version tuple."""
    match = _EXACT.fullmatch(text.strip())
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3) or 0))


def channel_version(kind: str, day: date) -> Version:
    """Version a dated nightly or beta channel builds: stable minor + 2 or + 1."""
    stable = stable_on(day)
    major, minor = _key(stable)[:2]
    return (major, minor + (2 if kind == "nightly" else 1), 0)


def satisfying_release(floor: Version, day: date) -> str:
    """Lowest minor release that meets ``floor``, at its newest patch before ``day``."""
    candidates = sorted((v for v, _ in STABLE_RELEASES if _key(v) >= floor), key=_key)
    if not candidates:
        raise ToolchainError(f"no stable release in the table satisfies {_fmt(floor)}")
    minor = _key(candidates[0])[:2]
    group = [v for v in candidates if _key(v)[:2] == minor]
    before = [v for v in group if release_date(v) < day]
    return max(before, key=_key) if before else group[0]


# Toolchain files


def _string_list(value: object, what: str, name: str) -> tuple[str, ...]:
    """Component or target names; they end up in a Dockerfile RUN line, so no shell syntax."""
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ToolchainError(f"{name}: [toolchain] {what} must be a list of strings")
    for item in value:
        if _NAME.fullmatch(item) is None:
            raise ToolchainError(f"{name}: [toolchain] {what} has an invalid name {item!r}")
    return tuple(value)


def parse_toolchain_file(text: str, name: str = "rust-toolchain.toml") -> ToolchainSpec:
    """Parse a toolchain file with rustup's rules.

    A legacy ``rust-toolchain`` file with a single non-empty line that is not TOML is a
    bare channel name; everything else must be TOML with a ``[toolchain]`` table.
    """
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise ToolchainError(f"{name} is empty")
    legacy = name == "rust-toolchain"
    if legacy and len(lines) == 1 and "=" not in lines[0] and not lines[0].startswith("["):
        return ToolchainSpec(lines[0])
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ToolchainError(f"{name} is not valid TOML: {exc}") from exc
    table = data.get("toolchain")
    if not isinstance(table, dict) or not table:
        raise ToolchainError(f"{name} has no [toolchain] table")
    if "path" in table:
        raise ToolchainError(f"{name} names a custom toolchain path; that is not supported")
    channel = table.get("channel")
    if channel is not None and not isinstance(channel, str):
        raise ToolchainError(f"{name}: [toolchain] channel must be a string")
    profile = table.get("profile")
    if profile is not None and profile not in PROFILES:
        raise ToolchainError(f"{name}: unknown profile {profile!r}")
    return ToolchainSpec(
        channel.strip() if channel else None,
        _string_list(table.get("components"), "components", name),
        _string_list(table.get("targets"), "targets", name),
        profile,
    )


def read_toolchain_file(
    tree: SourceTree, decisions: list[Decision]
) -> tuple[str, ToolchainSpec] | None:
    """The toolchain file rustup would obey at the repository root, if any."""
    present = {name: text for name in TOOLCHAIN_FILES if (text := tree.read(name)) is not None}
    if not present:
        decisions.append(
            Decision("file", "none", "no rust-toolchain.toml or rust-toolchain at the root")
        )
        return None
    name = "rust-toolchain" if "rust-toolchain" in present else "rust-toolchain.toml"
    if len(present) == 2:
        reason = (
            "both rust-toolchain and rust-toolchain.toml exist; rustup reads the legacy "
            "rust-toolchain and warns, so this does too"
        )
        if present["rust-toolchain"] == present["rust-toolchain.toml"]:
            reason = (
                "rust-toolchain and rust-toolchain.toml have the same content (one may link "
                "to the other); rustup reads rust-toolchain"
            )
        decisions.append(Decision("file", name, reason))
    spec = parse_toolchain_file(present[name], name)
    parts = [f"channel {spec.channel}" if spec.channel else "no channel"]
    if spec.components:
        parts.append(f"components {', '.join(spec.components)}")
    if spec.targets:
        parts.append(f"targets {', '.join(spec.targets)}")
    if spec.profile:
        parts.append(f"profile {spec.profile}")
    decisions.append(Decision("file", name, "; ".join(parts)))
    return name, spec


# Manifests


def _load(tree: SourceTree, path: str, decisions: list[Decision]) -> dict[str, Any] | None:
    text = tree.read(path)
    if text is None:
        return None
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        decisions.append(Decision("warning", path, f"not valid TOML, ignored ({exc})"))
        return None


def _inherits(value: object) -> bool:
    return isinstance(value, dict) and value.get("workspace") is True


def _uses_inheritance(data: dict[str, Any]) -> bool:
    package = data.get("package")
    if isinstance(package, dict) and any(_inherits(v) for v in package.values()):
        return True
    tables = [data]
    target = data.get("target")
    if isinstance(target, dict):
        tables.extend(t for t in target.values() if isinstance(t, dict))
    for table in tables:
        for key in DEPENDENCY_TABLES:
            deps = table.get(key)
            if isinstance(deps, dict) and any(_inherits(v) for v in deps.values()):
                return True
    return False


def workspace_manifests(tree: SourceTree, root: dict[str, Any]) -> list[str]:
    """Member manifests of the root workspace (``members`` globs minus ``exclude``)."""
    workspace = root.get("workspace")
    if not isinstance(workspace, dict):
        return []
    members = [m for m in workspace.get("members", []) if isinstance(m, str)]
    excludes = [posixpath.normpath(e) for e in workspace.get("exclude", []) if isinstance(e, str)]
    found = []
    for path in sorted(tree.paths()):
        folder = posixpath.dirname(path)
        if posixpath.basename(path) != "Cargo.toml" or not folder:
            continue
        excluded = any(folder == e or folder.startswith(e + "/") for e in excludes)
        if not excluded and any(glob_match(m, folder) for m in members):
            found.append(path)
    return found


def _inherited(package: dict[str, Any], ws_package: dict[str, Any], key: str) -> tuple[Any, str]:
    """Value of ``package.<key>`` and how it was set, following ``key.workspace = true``."""
    value = package.get(key)
    if _inherits(value):
        return ws_package.get(key), f"inherits {key} from [workspace.package]"
    return value, f"sets {key}"


def _editions(
    where: str, data: dict[str, Any], package: dict[str, Any], ws_package: dict[str, Any]
) -> list[tuple[str, str]]:
    """(edition, reason) for the package and every target that overrides it."""
    edition, how = _inherited(package, ws_package, "edition")
    editions = []
    if edition is None:
        editions.append(("2015", f"{where} uses edition 2015 (no edition key)"))
    elif isinstance(edition, str):
        editions.append((edition, f"{where} {how} = {edition}"))
    targets = [data.get("lib")]
    for key in TARGET_TABLES:
        entries = data.get(key)
        targets.extend(entries if isinstance(entries, list) else [])
    for target in targets:
        if isinstance(target, dict) and isinstance(target.get("edition"), str):
            label = target.get("name", "lib")
            edition = target["edition"]
            editions.append((edition, f"{where} target {label} uses edition {edition}"))
    return editions


def _package_floors(
    path: str,
    data: dict[str, Any],
    package: dict[str, Any],
    ws_package: dict[str, Any],
    decisions: list[Decision],
) -> list[Floor]:
    name = package.get("name") if isinstance(package.get("name"), str) else path
    where = f"package {name} ({path})"
    floors: list[Floor] = []

    msrv, how = _inherited(package, ws_package, "rust-version")
    parsed = parse_version_requirement(msrv) if isinstance(msrv, str) else None
    if parsed is not None:
        floors.append(Floor("msrv", parsed, f"{where} {how} = {msrv}"))
    elif msrv is not None or how.startswith("inherits"):
        decisions.append(Decision("warning", path, f"rust-version {msrv!r} ignored"))

    for edition, reason in _editions(where, data, package, ws_package):
        minimum = EDITION_MINIMUM.get(edition)
        if minimum is None:
            raise ToolchainError(f"{path}: unknown edition {edition!r}")
        floors.append(Floor("edition", _triple(minimum), reason))

    if _uses_inheritance(data):
        floors.append(
            Floor(
                "manifest",
                _triple(WORKSPACE_INHERITANCE_MINIMUM),
                f"{where} inherits keys from the workspace (cargo 1.64+)",
            )
        )
    return floors


def manifest_floors(tree: SourceTree, decisions: list[Decision]) -> list[Floor]:
    """Floors from the root package and every member of the root workspace."""
    root = _load(tree, "Cargo.toml", decisions)
    if root is None:
        decisions.append(Decision("manifest", "none", "no readable Cargo.toml at the root"))
        return []
    workspace = root.get("workspace")
    ws_package: dict[str, Any] = {}
    if isinstance(workspace, dict) and isinstance(workspace.get("package"), dict):
        ws_package = workspace["package"]
    manifests = [("Cargo.toml", root)]
    for path in workspace_manifests(tree, root):
        data = _load(tree, path, decisions)
        if data is not None:
            manifests.append((path, data))
    packages = [(p, d, d["package"]) for p, d in manifests if isinstance(d.get("package"), dict)]
    decisions.append(
        Decision(
            "manifest",
            f"{len(packages)} package(s)",
            "root package and root workspace members"
            + (f" ({len(manifests) - 1} member manifest(s))" if len(manifests) > 1 else ""),
        )
    )
    floors: list[Floor] = []
    for path, data, package in packages:
        floors.extend(_package_floors(path, data, package, ws_package, decisions))
    decisions.extend(Decision(f.step, _fmt(f.version), f.reason) for f in floors)
    if packages and not any(f.step == "msrv" for f in floors):
        decisions.append(Decision("msrv", "none", "no package sets rust-version"))
    return floors


def lockfile_floors(tree: SourceTree, decisions: list[Decision]) -> list[Floor]:
    """The oldest cargo that reads the committed Cargo.lock, as a floor."""
    text = tree.read("Cargo.lock")
    if text is None:
        decisions.append(Decision("lockfile", "none", "no Cargo.lock at the base commit"))
        return []
    try:
        version = parse_lockfile(text).format_version
    except LockfileError as exc:
        raise ToolchainError(f"Cargo.lock: {exc}") from exc
    minimum = READ_MINIMUM[version]
    if minimum is None:
        decisions.append(Decision("lockfile", "v1", "Cargo.lock format v1: every cargo reads it"))
        return []
    reason = f"Cargo.lock format v{version} needs cargo {_fmt(minimum)}+"
    decisions.append(Decision("lockfile", f"v{version}", reason))
    return [Floor("lockfile", minimum, reason)]


# Resolution


def _from_channel(
    name: str, spec: ToolchainSpec, day: date, decisions: list[Decision]
) -> tuple[str, str, bool] | None:
    """(toolchain name, reason, is a dated rustup channel), or None for the date rule."""
    channel = spec.channel
    if channel is None:
        decisions.append(Decision("channel", "none", f"{name} names no channel"))
        return None
    match = _CHANNEL.fullmatch(channel)
    if match is None:
        raise ToolchainError(f"{name} asks for an unsupported channel {channel!r}")
    kind, dated, host = match.group("name"), match.group("date"), match.group("host")
    try:
        when = date.fromisoformat(dated) if dated else None
    except ValueError as exc:
        raise ToolchainError(f"{name} asks for a channel with an invalid date {channel!r}") from exc
    if host:
        decisions.append(
            Decision("channel", f"-{host} ignored", "the container's architecture decides")
        )
    if kind[0].isdigit():
        if dated:
            raise ToolchainError(f"{name} asks for an unsupported channel {channel!r}")
        exact = normalize_version(kind)
        assert exact is not None
        reason = f"{name} pins {kind}"
        if exact != kind:
            reason += f" (newest {kind}.x is {exact})"
        return exact, reason, False
    if kind == "stable":
        if when is None:
            decisions.append(
                Decision("channel", "stable", f"{name} follows stable; use the commit date")
            )
            return None
        version = stable_on(when)
        return version, f"{name} pins stable-{dated}, which was {version}", False
    if when is None:
        when = day - timedelta(days=1)
        reason = f"{name} follows {kind}; the {kind} of the day before the commit"
    else:
        reason = f"{name} pins a dated {kind} channel"
    return f"{kind}-{when.isoformat()}", reason, True


def resolve_toolchain(tree: SourceTree, commit_time: datetime) -> Toolchain:
    """Choose the toolchain for a base commit tree and a fix committed at ``commit_time``."""
    day = commit_time.date()
    decisions: list[Decision] = []
    found = read_toolchain_file(tree, decisions)
    file_name, spec = found if found else (None, ToolchainSpec(None))
    chosen = _from_channel(file_name, spec, day, decisions) if file_name else None
    if chosen is None:
        version = newest_stable_before(day)
        source = "release-date"
        reason = (
            f"newest stable before {day.isoformat()} "
            f"({version} released {release_date(version).isoformat()})"
        )
        dated = False
        decisions.append(Decision("date", version, reason))
    else:
        version, reason, dated = chosen
        source = file_name or ""
        decisions.append(Decision("channel", version, reason))

    floors = manifest_floors(tree, decisions) + lockfile_floors(tree, decisions)
    if floors:
        top = max(floors, key=lambda f: f.version)
        if dated:
            kind, when = version.split("-", 1)
            built = channel_version(kind, date.fromisoformat(when))
            if built < top.version:
                decisions.append(
                    Decision(
                        "warning",
                        f"{version} builds {_fmt(built)}-{kind}",
                        f"below the floor {_fmt(top.version)} ({top.reason}); a dated "
                        "channel is kept as pinned",
                    )
                )
            else:
                meets = f"{version} builds {_fmt(built)}-{kind}, which meets every floor"
                decisions.append(Decision("floor", _fmt(top.version), meets))
        elif _triple(version) < top.version:
            raised = satisfying_release(top.version, day)
            reason = f"raised from {version} to meet {_fmt(top.version)}: {top.reason}"
            decisions.append(Decision("raise", raised, reason))
            version = raised
            source = {"msrv": "rust-version", "edition": "edition", "lockfile": "Cargo.lock"}.get(
                top.step, "manifest"
            )
        else:
            decisions.append(Decision("floor", _fmt(top.version), f"{version} meets every floor"))

    image_version = CHANNEL_HOST_VERSION if dated else version
    if dated:
        decisions.append(
            Decision(
                "install",
                version,
                f"rustup toolchain install on the pinned rust:{image_version}-slim image",
            )
        )
    if (spec.components or spec.targets) and not dated:
        decisions.append(
            Decision(
                "install",
                ", ".join(spec.components + spec.targets),
                "rustup component and target add on the rust:*-slim image",
            )
        )
    if spec.profile and spec.profile != "minimal" and not dated:
        decisions.append(
            Decision(
                "warning",
                f"profile {spec.profile}",
                "rust:*-slim images carry the minimal profile; only listed components are added",
            )
        )
    return Toolchain(
        version,
        source,
        reason,
        image_version,
        dated,
        spec.components,
        spec.targets,
        spec.profile,
        file_name,
        tuple(decisions),
    )


def base_image(version: str, digests: dict[str, str] = IMAGE_DIGESTS) -> str:
    """Digest-pinned image reference for ``version`` from the offline table."""
    digest = digests.get(version)
    if digest is None:
        raise ToolchainError(
            f"no pinned digest for rust:{version}-slim; pass --registry to look it up "
            f"or --image rust:{version}-slim@sha256:..."
        )
    return f"rust:{version}-slim@{digest}"

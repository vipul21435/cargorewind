"""Dependency reproducibility: the committed lockfile, or one bounded by the commit date.

With a committed ``Cargo.lock`` the environment runs ``cargo fetch --locked`` (the
toolchain inference already raised the toolchain to one whose cargo reads the lockfile
format). Without one, cargo would resolve every requirement to today's newest versions,
which often no longer build with the toolchain of the commit date. The pin loop fixes
that:

1. ``cargo generate-lockfile`` in a container with the chosen toolchain;
2. every crates.io entry published at or after the cutoff (the fix commit's committer
   time) is late; for the late entries that no other late entry depends on, pick the
   newest non-yanked version published before the cutoff that satisfies every
   requirement on it (from the workspace manifests, or from the index entry of each
   dependent's locked version);
3. ``cargo update -p name:version --precise <picked>`` for each, read the lockfile again
   and repeat, because older versions bring older (or new) transitive dependencies;
4. stop when no entry is late, and report the ones that cannot be bounded.
"""

from __future__ import annotations

import posixpath
import re
import shlex
import tomllib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from cargorewind.backend import Session
from cargorewind.crateindex import CrateIndex, CrateIndexError, IndexVersion, candidates
from cargorewind.layout import SourceTree
from cargorewind.lockfile import LockedPackage, Lockfile, parse_lockfile
from cargorewind.semver import SemverError, VersionReq
from cargorewind.toolchain import workspace_manifests

Log = Callable[[str], None]

KINDS = {
    "dependencies": "normal",
    "dev-dependencies": "dev",
    "dev_dependencies": "dev",
    "build-dependencies": "build",
    "build_dependencies": "build",
}
MAX_ROUNDS = 30
LOCK_BEGIN = "--- cargorewind: Cargo.lock ---"
LOCK_END = "--- cargorewind: end ---"
_EXIT = re.compile(r"^--- cargorewind: exit (\d+) (\S+) ---$")


class LockError(RuntimeError):
    """cargo could not produce a lockfile."""


@dataclass(frozen=True)
class Requirement:
    """One crates.io dependency requirement of a workspace member."""

    member: str  # package name of the member that declares it
    name: str  # the crate's name on crates.io (``package = ...`` when renamed)
    req: str
    kind: str  # normal, dev or build
    target: str | None = None
    optional: bool = False


def _dependency_tables(data: dict[str, Any]) -> Iterator[tuple[str, str | None, dict[str, Any]]]:
    """(kind, target cfg, table) for every dependency table of a manifest."""
    for key, kind in KINDS.items():
        table = data.get(key)
        if isinstance(table, dict):
            yield kind, None, table
    targets = data.get("target")
    if isinstance(targets, dict):
        for cfg, section in targets.items():
            if not isinstance(section, dict):
                continue
            for key, kind in KINDS.items():
                table = section.get(key)
                if isinstance(table, dict):
                    yield kind, str(cfg), table


def _entry(key: str, value: Any, inherited: dict[str, Any]) -> tuple[str, str] | str | None:
    """(crate name, requirement) of a crates.io dependency, a reason to skip it, or None."""
    if isinstance(value, str):
        return key, value
    if not isinstance(value, dict):
        return None
    if value.get("workspace") is True:
        base = inherited.get(key)
        if base is None:
            return f"{key}: workspace = true, but [workspace.dependencies] has no {key}"
        value = {"version": base} if isinstance(base, str) else {**base, **value}
    if "path" in value or "git" in value:
        return None  # a local or git dependency: not on crates.io
    if "registry" in value or "registry-index" in value:
        return f"{key}: an alternate registry, not bounded by date"
    package = value.get("package")
    version = value.get("version")
    return (package if isinstance(package, str) else key), (
        version if isinstance(version, str) else "*"
    )


def _load(tree: SourceTree, path: str) -> dict[str, Any] | None:
    text = tree.read(path)
    if text is None:
        return None
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None


def manifest_requirements(tree: SourceTree) -> tuple[list[Requirement], list[str]]:
    """crates.io requirements of the root package and every root workspace member,
    plus notes about dependencies that cannot be bounded."""
    root = _load(tree, "Cargo.toml")
    if root is None:
        return [], ["no readable Cargo.toml at the root"]
    workspace = root.get("workspace")
    inherited: dict[str, Any] = {}
    if isinstance(workspace, dict) and isinstance(workspace.get("dependencies"), dict):
        inherited = workspace["dependencies"]
    manifests = [("Cargo.toml", root)]
    for path in workspace_manifests(tree, root):
        data = _load(tree, path)
        if data is not None:
            manifests.append((path, data))
    found: list[Requirement] = []
    notes: list[str] = []
    for path, data in manifests:
        package = data.get("package")
        if not isinstance(package, dict):
            continue
        member = package.get("name")
        member_name = member if isinstance(member, str) else posixpath.dirname(path) or "."
        for kind, target, table in _dependency_tables(data):
            for key, value in table.items():
                entry = _entry(key, value, inherited)
                if isinstance(entry, str):
                    notes.append(f"{path}: {entry}")
                elif entry is not None:
                    optional = isinstance(value, dict) and value.get("optional") is True
                    found.append(
                        Requirement(member_name, entry[0], entry[1], kind, target, optional)
                    )
    return found, notes


# The pin loop


@dataclass(frozen=True)
class Pin:
    name: str
    from_version: str
    to_version: str
    reason: str

    @property
    def spec(self) -> str:
        return f"{self.name}:{self.from_version}"


@dataclass(frozen=True)
class Unbounded:
    name: str
    version: str
    reason: str


@dataclass
class PinRound:
    number: int
    pins: list[Pin] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)  # spec -> cargo's error


@dataclass
class BoundLock:
    """The outcome of the pin loop."""

    lockfile: str
    cutoff: datetime
    registry_packages: int
    late_at_start: int
    rounds: list[PinRound] = field(default_factory=list)
    unbounded: list[Unbounded] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def bounded(self) -> bool:
        return not self.unbounded

    def pins(self) -> list[Pin]:
        failed = {spec for r in self.rounds for spec in r.failed}
        return [p for r in self.rounds for p in r.pins if p.spec not in failed]


class LockDriver(Protocol):
    """Runs cargo against the base checkout (in a container) and returns Cargo.lock."""

    def generate(self) -> str: ...

    def pin(self, pins: list[Pin]) -> tuple[str, dict[str, str]]: ...


def _stamp(when: datetime | None) -> str:
    return when.date().isoformat() if when is not None else "an unknown date"


class _Planner:
    def __init__(
        self,
        index: CrateIndex,
        cutoff: datetime,
        requirements: list[Requirement],
        notes: list[str],
    ) -> None:
        self.index = index
        self.cutoff = cutoff
        self.requirements = requirements
        self.notes = notes
        self.failed: dict[tuple[str, str], set[str]] = {}
        self._versions: dict[str, list[IndexVersion]] = {}

    def versions(self, name: str) -> list[IndexVersion]:
        if name not in self._versions:
            try:
                self._versions[name] = self.index.versions(name)
            except CrateIndexError as exc:
                self.notes.append(str(exc))
                self._versions[name] = []
        return self._versions[name]

    def entry(self, package: LockedPackage) -> IndexVersion | None:
        return next((v for v in self.versions(package.name) if v.vers == package.version), None)

    def is_late(self, package: LockedPackage) -> bool:
        entry = self.entry(package)
        return entry is None or entry.pubtime is None or entry.pubtime >= self.cutoff

    def reqs_on(self, lock: Lockfile, package: LockedPackage) -> list[tuple[str, str]]:
        """(requirement, who asks) for every lockfile edge that points at ``package``."""
        found: list[tuple[str, str]] = []
        for dependent in lock.dependents(package):
            if dependent.source is None:
                found += [
                    (r.req, dependent.name)
                    for r in self.requirements
                    if r.member == dependent.name and r.name == package.name
                ]
            elif dependent.from_crates_io and (entry := self.entry(dependent)) is not None:
                found += [(req, str(dependent)) for req in entry.requirement_on(package.name)]
        return found

    def plan(self, lock: Lockfile, package: LockedPackage) -> Pin | Unbounded:
        asks = self.reqs_on(lock, package)
        reqs: list[VersionReq] = []
        for text, who in asks:
            try:
                reqs.append(VersionReq.parse(text))
            except SemverError:
                self.notes.append(f"{who}: requirement {text!r} on {package.name} ignored")
        exclude = frozenset(self.failed.get((package.name, package.version), set()))
        entry = self.entry(package)
        published = f"published {_stamp(entry.pubtime if entry else None)}"
        asked = ", ".join(f"{text} ({who})" for text, who in asks) or "no known requirement"
        found = candidates(self.versions(package.name), reqs, self.cutoff, exclude)
        if not found:
            reason = (
                f"{package} {published}; no non-yanked version before "
                f"{self.cutoff.date().isoformat()} matches {asked}"
            )
            return Unbounded(package.name, package.version, reason)
        best = found[0]
        reason = (
            f"{package.version} {published}; {best.vers} ({_stamp(best.pubtime)}) matches {asked}"
        )
        return Pin(package.name, package.version, best.vers, reason)


def bound_lockfile(
    driver: LockDriver,
    index: CrateIndex,
    cutoff: datetime,
    requirements: list[Requirement],
    log: Log,
) -> BoundLock:
    """Generate a lockfile and pin every crates.io entry to a version published before
    ``cutoff``, round by round, from the top of the dependency graph down."""
    notes: list[str] = []
    planner = _Planner(index, cutoff, requirements, notes)
    text = driver.generate()
    lock = parse_lockfile(text)
    registry = lock.registry_packages()
    late = [p for p in registry if planner.is_late(p)]
    result = BoundLock(text, cutoff, len(registry), len(late), notes=notes)
    log(
        f"lock      generated: {len(registry)} crates.io package(s), "
        f"{len(late)} published at or after {cutoff.isoformat()}"
    )
    for number in range(1, MAX_ROUNDS + 1):
        late = [p for p in lock.registry_packages() if planner.is_late(p)]
        late_keys = {p.key for p in late}
        frontier = [p for p in late if not any(d.key in late_keys for d in lock.dependents(p))]
        rest = [p for p in late if p not in frontier]
        pins = [d for d in (planner.plan(lock, p) for p in frontier) if isinstance(d, Pin)]
        if not pins:  # nothing above can move: try the late entries below them
            pins = [d for d in (planner.plan(lock, p) for p in rest) if isinstance(d, Pin)]
        if not pins:
            break
        round_ = PinRound(number, pins)
        text, round_.failed = driver.pin(pins)
        result.rounds.append(round_)
        log(f"pin       round {number}: {len(pins)} package(s)")
        for pin in pins:
            error = round_.failed.get(pin.spec)
            status = "ok" if error is None else "FAILED: " + (error.splitlines() or [""])[-1]
            log(f"          {pin.name} {pin.from_version} -> {pin.to_version} ({status})")
            if error is not None:
                planner.failed.setdefault((pin.name, pin.from_version), set()).add(pin.to_version)
        lock = parse_lockfile(text)
    result.lockfile = text
    for package in lock.registry_packages():
        if not planner.is_late(package):
            continue
        decision = planner.plan(lock, package)
        if isinstance(decision, Pin):  # the loop ran out of rounds
            reason = f"still late after {MAX_ROUNDS} rounds: {decision.reason}"
            decision = Unbounded(decision.name, decision.from_version, reason)
        result.unbounded.append(decision)
        log(f"unbounded {decision.name} {decision.version}: {decision.reason}")
    return result


# cargo in a container session


def cargo_script(commands: list[tuple[str, str]]) -> str:
    """A script that runs each command, prints its exit status, then prints Cargo.lock."""
    lines = ["exec 2>&1"]
    for label, command in commands:
        lines.append(f'{command}; echo "--- cargorewind: exit $? {label} ---"')
    lines.append(f"echo '{LOCK_BEGIN}'; cat Cargo.lock; echo; echo '{LOCK_END}'")
    return "\n".join(lines) + "\n"


def parse_script_output(output: str) -> tuple[str | None, dict[str, tuple[int, str]]]:
    """(Cargo.lock text or None, {label: (exit code, output of that command)})."""
    statuses: dict[str, tuple[int, str]] = {}
    chunk: list[str] = []
    lock: str | None = None
    head, sep, tail = output.partition(LOCK_BEGIN + "\n")
    if sep:
        body, found, _ = tail.rpartition("\n" + LOCK_END)
        lock = body if found else None
    for line in head.splitlines():
        match = _EXIT.match(line)
        if match is None:
            chunk.append(line)
            continue
        statuses[match.group(2)] = (int(match.group(1)), "\n".join(chunk).strip())
        chunk = []
    return lock, statuses


class CargoDriver:
    """``LockDriver`` that runs cargo through a container session."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self.rounds = 0

    def generate(self) -> str:
        result = self.session.run(
            "generate-lockfile", cargo_script([("generate", "cargo generate-lockfile")])
        )
        lock, statuses = parse_script_output(result.output)
        code, detail = statuses.get("generate", (1, result.output.strip()))
        if code != 0 or not lock:
            raise LockError(f"cargo generate-lockfile failed:\n{detail[-2000:]}")
        return lock

    def pin(self, pins: list[Pin]) -> tuple[str, dict[str, str]]:
        self.rounds += 1
        commands = [
            (
                f"pin{n}",
                "cargo update -p "
                + shlex.quote(pin.spec)
                + " --precise "
                + shlex.quote(pin.to_version),
            )
            for n, pin in enumerate(pins)
        ]
        result = self.session.run(f"pin-round-{self.rounds}", cargo_script(commands))
        lock, statuses = parse_script_output(result.output)
        if lock is None:
            raise LockError(
                f"no Cargo.lock after pin round {self.rounds}:\n{result.output[-2000:]}"
            )
        failed = {}
        for n, pin in enumerate(pins):
            code, detail = statuses.get(f"pin{n}", (1, "no exit status"))
            if code != 0:
                failed[pin.spec] = detail or f"exit {code}"
        return lock, failed

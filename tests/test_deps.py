from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cargorewind import deps
from cargorewind.backend import RunResult
from cargorewind.crateindex import CrateIndex, DirectoryIndex, SparseIndex, candidates
from cargorewind.deps import (
    LOCK_BEGIN,
    LOCK_END,
    CargoDriver,
    LockError,
    Pin,
    Requirement,
    bound_lockfile,
    cargo_script,
    edge_requirements,
    manifest_requirements,
    parse_script_output,
)
from cargorewind.layout import MemoryTree
from cargorewind.lockfile import CRATES_IO_SOURCES, parse_lockfile
from cargorewind.registry import HttpResponse
from cargorewind.semver import Version, VersionReq
from tests.test_registry import FakeHttp

INDEX = DirectoryIndex(Path(__file__).parent / "fixtures" / "crates-index")
REGISTRY = sorted(CRATES_IO_SOURCES)[0]
FAR_FUTURE = datetime(2100, 1, 1, tzinfo=UTC)


def when(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


# Manifest requirements


def test_manifest_requirements_cover_every_table_and_member() -> None:
    tree = MemoryTree(
        {
            "Cargo.toml": """\
[package]
name = "app"

[dependencies]
home = "0.5.4"
rnd = { package = "rand", version = "0.8", optional = true }
local = { path = "crates/local" }
remote = { git = "https://example.com/remote" }
private = { version = "1", registry = "corp" }
shared = { workspace = true }
missing = { workspace = true }
anything = {}
weird = 7

[dev-dependencies]
tempfile = "3"

[build-dependencies]
cc = "1.0"

[target.'cfg(unix)'.dependencies]
libc = "0.2"

[target.'cfg(windows)'.dev-dependencies]
winapi = "0.3"

[target.bad]
dependencies = "nope"

[workspace]
members = ["crates/*"]

[workspace.dependencies]
shared = { version = "1.2", features = ["x"] }
""",
            "crates/local/Cargo.toml": '[package]\nname = "local"\n[dependencies]\neither = "1"\n',
            "crates/virtual/Cargo.toml": "[dependencies]\nignored = '1'\n",
            "crates/broken/Cargo.toml": "[package\n",
        }
    )
    scan = manifest_requirements(tree)
    found, notes = scan.requirements, scan.notes
    assert [(r.member, r.name, r.req, r.kind, r.target, r.optional) for r in found] == [
        ("app", "home", "0.5.4", "normal", None, False),
        ("app", "rand", "0.8", "normal", None, True),
        ("app", "shared", "1.2", "normal", None, False),
        ("app", "anything", "*", "normal", None, False),
        ("app", "tempfile", "3", "dev", None, False),
        ("app", "cc", "1.0", "build", None, False),
        ("app", "libc", "0.2", "normal", "cfg(unix)", False),
        ("app", "winapi", "0.3", "dev", "cfg(windows)", False),
        ("local", "either", "1", "normal", None, False),
    ]
    assert notes == [
        "Cargo.toml: private: an alternate registry, not bounded by date",
        "Cargo.toml: missing: workspace = true, but [workspace.dependencies] has no missing",
        "Cargo.toml: remote: a git dependency; cargo resolves its dependencies",
    ]
    assert scan.opaque == [notes[-1]] and scan.needs_bounding


def test_manifest_requirements_without_a_manifest() -> None:
    scan = manifest_requirements(MemoryTree({}))
    assert (scan.requirements, scan.notes, scan.opaque) == (
        [],
        ["no readable Cargo.toml at the root"],
        [],
    )
    assert not scan.needs_bounding
    string_inherited = MemoryTree(
        {
            "Cargo.toml": '[package]\nname = "a"\n[dependencies]\nx = { workspace = true }\n'
            '[workspace.dependencies]\nx = "2"\n'
        }
    )
    assert [r.req for r in manifest_requirements(string_inherited).requirements] == ["2"]


def test_manifest_requirements_follow_path_dependencies() -> None:
    # Regression: crates.io dependencies reached only through a path dependency were
    # never read, so the crate counted as having none and cargo resolved them to today.
    tree = MemoryTree(
        {
            "Cargo.toml": '[package]\nname = "app"\n[dependencies]\n'
            'core-impl = { path = "core", version = "0.1" }\n'
            "shared = { workspace = true }\n"
            'up = { path = "../outside" }\n'
            'gone = { path = "missing" }\n'
            "bad = { path = 7 }\n"
            '[workspace.dependencies]\nshared = { path = "libs/shared" }\n',
            "core/Cargo.toml": '[package]\nname = "core-impl"\n[dependencies]\n'
            'regex = "1"\nserde = "1"\nback = { path = ".." }\n'
            "[dev-dependencies]\n"
            'again = { path = "../libs/shared" }\n',
            "libs/shared/Cargo.toml": '[package]\nname = "shared"\n[dependencies]\nlog = "0.4"\n',
        }
    )
    scan = manifest_requirements(tree)
    assert [(r.member, r.name, r.req, r.kind) for r in scan.requirements] == [
        ("core-impl", "regex", "1", "normal"),
        ("core-impl", "serde", "1", "normal"),
        ("shared", "log", "0.4", "normal"),
    ]
    assert scan.opaque == [
        "Cargo.toml: up: a path dependency outside the repository",
        "Cargo.toml: gone: a path dependency unreadable",
    ]


# The pin loop, against a small model of cargo's resolver


class FakeCargo:
    """Resolves like cargo does in the cases the pin loop meets, over a recorded index.

    ``generate`` picks the newest non-yanked match for every requirement (reusing a
    locked version that already matches), ``pin`` moves one entry to another version when
    every dependent's requirement allows it, re-resolves its dependencies and drops
    packages nothing depends on any more.
    """

    def __init__(
        self,
        index: CrateIndex,
        members: dict[str, list[tuple[str, str]]],
        refuse: frozenset[tuple[str, str]] = frozenset(),
    ) -> None:
        self.index = index
        self.members = members
        self.refuse = refuse
        self.packages: dict[tuple[str, str], list[tuple[str, str]]] = {}
        self.roots: dict[str, list[tuple[str, str]]] = {}
        self.pin_calls: list[list[str]] = []

    def _entry(self, name: str, vers: str) -> list[tuple[str, str]]:
        entry = next(v for v in self.index.versions(name) if v.vers == vers)
        return [(d.name, d.req) for d in entry.deps if d.kind != "dev" and not d.optional]

    def _ensure(self, name: str, req: str) -> tuple[str, str]:
        wanted = VersionReq.parse(req)
        locked = [v for (n, v) in self.packages if n == name and wanted.matches(Version.parse(v))]
        if locked:
            return name, max(locked, key=lambda v: Version.parse(v).key())
        vers = candidates(self.index.versions(name), [wanted], FAR_FUTURE)[0].vers
        self._add(name, vers)
        return name, vers

    def _add(self, name: str, vers: str) -> None:
        self.packages[(name, vers)] = []
        self.packages[(name, vers)] = [self._ensure(n, r) for n, r in self._entry(name, vers)]

    def _gc(self) -> None:
        seen: set[tuple[str, str]] = set()
        stack = [key for edges in self.roots.values() for key in edges]
        while stack:
            key = stack.pop()
            if key not in seen:
                seen.add(key)
                stack.extend(self.packages[key])
        self.packages = {k: v for k, v in self.packages.items() if k in seen}

    def render(self) -> str:
        lines = ["version = 3", ""]
        for member, edges in sorted(self.roots.items()):
            lines += ["[[package]]", f'name = "{member}"', 'version = "0.1.0"']
            lines += ["dependencies = ["] + [f' "{n} {v}",' for n, v in edges] + ["]", ""]
        for (name, vers), edges in sorted(self.packages.items()):
            lines += ["[[package]]", f'name = "{name}"', f'version = "{vers}"']
            lines += [f'source = "{REGISTRY}"']
            lines += ["dependencies = ["] + [f' "{n} {v}",' for n, v in edges] + ["]", ""]
        return "\n".join(lines)

    def generate(self) -> str:
        for member, reqs in self.members.items():
            self.roots[member] = [self._ensure(name, req) for name, req in reqs]
        return self.render()

    def _allowed(self, name: str, old: str, new: str) -> bool:
        """Every edge that points at ``name old`` must accept ``new``, each against the
        one requirement it was resolved from (like cargo, not against every requirement
        the dependent has on that crate)."""
        edge = (name, old)
        asks = [
            r
            for m, reqs in self.members.items()
            for (_, r), e in zip(reqs, self.roots[m], strict=True)
            if e == edge
        ]
        asks += [
            r
            for (dep, vers), edges in self.packages.items()
            for (_, r), e in zip(self._entry(dep, vers), edges, strict=True)
            if e == edge
        ]
        return all(VersionReq.parse(r).matches(Version.parse(new)) for r in asks)

    def pin(self, pins: list[Pin]) -> tuple[str, dict[str, str]]:
        self.pin_calls.append([f"{p.spec}={p.to_version}" for p in pins])
        failed: dict[str, str] = {}
        for pin in pins:
            old, new = (pin.name, pin.from_version), (pin.name, pin.to_version)
            if old not in self.packages or new in self.refuse:
                failed[pin.spec] = f"error: failed to select {pin.to_version}"
                continue
            if not self._allowed(pin.name, pin.from_version, pin.to_version):
                failed[pin.spec] = "error: a dependent does not accept it"
                continue
            del self.packages[old]
            swap = {old: new}
            self.roots = {m: [swap.get(e, e) for e in es] for m, es in self.roots.items()}
            self.packages = {k: [swap.get(e, e) for e in es] for k, es in self.packages.items()}
            if new not in self.packages:
                self._add(*new)
            self._gc()
        return self.render(), failed


def _versions(text: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for package in parse_lockfile(text).registry_packages():
        found.setdefault(package.name, []).append(package.version)
    return found


HOME_REQS = [Requirement("demo", "home", "0.5.4", "normal", "cfg(unix)")]


def test_pin_loop_bounds_every_entry_round_by_round() -> None:
    cargo = FakeCargo(INDEX, {"demo": [("home", "0.5.4")]})
    lines: list[str] = []
    result = bound_lockfile(cargo, INDEX, when("2024-03-01T00:00:00"), HOME_REQS, lines.append)

    assert result.bounded and result.unbounded == []
    assert result.late_at_start == 3  # home 0.5.12, windows-sys 0.61.2, windows-link 0.2.1
    assert [len(r.pins) for r in result.rounds] == [1, 1, 7]
    first = result.rounds[0].pins[0]
    assert (first.name, first.from_version, first.to_version) == ("home", "0.5.12", "0.5.9")
    assert "0.5.9 (2023-12-15) matches 0.5.4 (demo)" in first.reason
    targets = result.rounds[1].pins[0]
    assert (targets.name, targets.from_version, targets.to_version) == (
        "windows-targets",
        "0.52.6",
        "0.52.4",
    )
    assert "matches ^0.52.0 (windows-sys 0.52.0)" in targets.reason
    final = _versions(result.lockfile)
    assert final["home"] == ["0.5.9"] and final["windows-sys"] == ["0.52.0"]
    assert final["windows_x86_64_gnu"] == ["0.52.4"]
    assert "windows-link" not in final and "windows_i686_gnullvm" not in final
    assert len(result.pins()) == 9
    assert lines[0].startswith("lock      generated: 3 crates.io package(s), 3 published")
    assert "          home 0.5.12 -> 0.5.9 (ok)" in lines


def test_pin_loop_retries_with_the_next_version_when_cargo_refuses() -> None:
    cargo = FakeCargo(INDEX, {"demo": [("home", "0.5.4")]}, refuse=frozenset({("home", "0.5.9")}))
    lines: list[str] = []
    result = bound_lockfile(cargo, INDEX, when("2024-03-01T00:00:00"), HOME_REQS, lines.append)
    assert result.rounds[0].failed == {"home:0.5.12": "error: failed to select 0.5.9"}
    assert result.rounds[1].pins[0].to_version == "0.5.5"
    assert _versions(result.lockfile)["home"] == ["0.5.5"]
    assert _versions(result.lockfile)["windows-sys"] == ["0.48.0"]
    assert result.bounded
    assert "          home 0.5.12 -> 0.5.9 (FAILED: error: failed to select 0.5.9)" in lines
    # Regression: the retried pin has the same spec as the refused one and was dropped.
    homes = [(p.from_version, p.to_version) for p in result.pins() if p.name == "home"]
    assert homes == [("0.5.12", "0.5.5")]
    assert len(result.pins()) == sum(len(r.pins) - len(r.failed) for r in result.rounds)


def test_pin_loop_keeps_each_edge_to_its_own_requirement() -> None:
    # Regression: a renamed second version of the same crate put both requirements on
    # every edge, so no version matched "0.1 and 1.6" and either 1.18.0 stayed late.
    members = {"demo": [("either", "0.1"), ("either", "1.6")]}
    reqs = [
        Requirement("demo", "either", "0.1", "normal"),
        Requirement("demo", "either", "1.6", "normal"),
    ]
    lines: list[str] = []
    result = bound_lockfile(
        FakeCargo(INDEX, members), INDEX, when("2024-03-01T00:00:00"), reqs, lines.append
    )
    assert result.bounded, result.unbounded
    (pin,) = result.pins()
    assert (pin.name, pin.from_version, pin.to_version) == ("either", "1.18.0", "1.10.0")
    assert pin.reason.endswith("matches 1.6 (demo)")
    assert sorted(_versions(result.lockfile)["either"]) == ["0.1.7", "1.10.0"]


def _index_line(name: str, vers: str, pubtime: str, deps: list[dict[str, object]]) -> str:
    entry = {"name": name, "vers": vers, "deps": deps, "yanked": False, "pubtime": pubtime}
    return json.dumps(entry) + "\n"


def test_pin_loop_ignores_the_other_alias_of_a_registry_dependent(tmp_path: Path) -> None:
    # Regression: two optional aliases of different glam versions on one crates.io
    # dependent were combined into "^0.13 and ^0.24", so glam 0.24.9 was reported as
    # unbounded although only the ^0.24 alias has an edge in the lockfile.
    aliases = [
        {"name": n, "package": "glam", "req": r, "kind": "normal", "optional": True}
        for n, r in (("glam013", "^0.13"), ("glam024", "^0.24"))
    ]
    (tmp_path / "ma" / "th").mkdir(parents=True)
    (tmp_path / "ma" / "th" / "mathlib").write_text(
        _index_line("mathlib", "1.0.0", "2023-01-05T00:00:00Z", aliases)
    )
    (tmp_path / "gl" / "am").mkdir(parents=True)
    (tmp_path / "gl" / "am" / "glam").write_text(
        _index_line("glam", "0.13.1", "2021-03-01T00:00:00Z", [])
        + _index_line("glam", "0.24.2", "2023-09-23T00:00:00Z", [])
        + _index_line("glam", "0.24.9", "2024-06-10T00:00:00Z", [])
    )
    index = DirectoryIndex(tmp_path)

    def lock(glam: str) -> str:
        return (
            'version = 3\n[[package]]\nname = "demo"\nversion = "0.1.0"\n'
            'dependencies = ["mathlib"]\n'
            f'[[package]]\nname = "mathlib"\nversion = "1.0.0"\nsource = "{REGISTRY}"\n'
            'dependencies = ["glam"]\n'
            f'[[package]]\nname = "glam"\nversion = "{glam}"\nsource = "{REGISTRY}"\n'
        )

    class Moves:
        def generate(self) -> str:
            return lock("0.24.9")

        def pin(self, pins: list[Pin]) -> tuple[str, dict[str, str]]:
            (only,) = pins
            return lock(only.to_version), {}

    reqs = [Requirement("demo", "mathlib", "1", "normal")]
    result = bound_lockfile(Moves(), index, when("2024-03-01T00:00:00"), reqs, print)
    assert result.bounded, result.unbounded
    (pin,) = result.pins()
    assert (pin.from_version, pin.to_version) == ("0.24.9", "0.24.2")
    assert pin.reason.endswith("matches ^0.24 (mathlib 1.0.0)")


@pytest.mark.parametrize(
    ("texts", "locked", "kept"),
    [
        (["0.1", "1.6"], "1.18.0", ["1.6"]),
        (["0.1", "1.6"], "0.1.7", ["0.1"]),
        ([">=1", "1.2"], "1.9.0", [">=1", "1.2"]),
        (["latest", "1.6"], "1.9.0", ["latest", "1.6"]),
        (["0.1", "0.2"], "1.0.0", ["0.1", "0.2"]),  # none satisfied: keep them all
        (["0.1"], "not-a-version", ["0.1"]),
    ],
)
def test_edge_requirements(texts: list[str], locked: str, kept: list[str]) -> None:
    assert edge_requirements(texts, locked) == kept


def test_pin_loop_reports_entries_that_cannot_be_bounded() -> None:
    reqs = [Requirement("demo", "either", ">=1.13", "normal")]
    cargo = FakeCargo(INDEX, {"demo": [("either", ">=1.13")]})
    lines: list[str] = []
    result = bound_lockfile(cargo, INDEX, when("2020-01-01T00:00:00"), reqs, lines.append)
    assert result.rounds == [] and not result.bounded
    (item,) = result.unbounded
    assert item.name == "either"
    assert "no non-yanked version before 2020-01-01 matches >=1.13 (demo)" in item.reason
    assert lines[-1].startswith(f"unbounded either {item.version}:")


def test_pin_loop_gives_up_after_the_round_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deps, "MAX_ROUNDS", 1)
    cargo = FakeCargo(INDEX, {"demo": [("home", "0.5.4")]})
    result = bound_lockfile(cargo, INDEX, when("2024-03-01T00:00:00"), HOME_REQS, print)
    assert len(result.rounds) == 1
    assert {u.name for u in result.unbounded} == {
        "windows-targets",
        *(
            f"windows_{arch}"
            for arch in (
                "aarch64_gnullvm",
                "aarch64_msvc",
                "i686_gnu",
                "i686_gnullvm",
                "i686_msvc",
                "x86_64_gnu",
                "x86_64_gnullvm",
                "x86_64_msvc",
            )
        ),
    }
    reasons = {u.name: u.reason for u in result.unbounded}
    # windows-targets could still move; its dependencies cannot until it does.
    assert reasons["windows-targets"].startswith("still late after 1 rounds: 0.52.6 published")
    assert "no non-yanked version before 2024-03-01 matches ^0.52.6" in reasons["windows_i686_gnu"]


def test_pin_loop_notes_unknown_crates_and_bad_requirements() -> None:
    lock = (
        'version = 3\n[[package]]\nname = "demo"\nversion = "0.1.0"\n'
        'dependencies = ["ghost 1.0.0", "home 0.5.12"]\n'
        f'[[package]]\nname = "ghost"\nversion = "1.0.0"\nsource = "{REGISTRY}"\n'
        f'[[package]]\nname = "home"\nversion = "0.5.12"\nsource = "{REGISTRY}"\n'
    )

    class Fixed:
        def generate(self) -> str:
            return lock

        def pin(self, pins: list[Pin]) -> tuple[str, dict[str, str]]:
            return lock, {p.spec: "error: nope" for p in pins}

    reqs = [Requirement("demo", "home", "latest", "normal")]
    result = bound_lockfile(Fixed(), INDEX, when("2024-03-01T00:00:00"), reqs, print)
    assert any("ghost: not in the recorded index" in n for n in result.notes)
    assert "demo: requirement 'latest' on home ignored" in result.notes
    assert {u.name for u in result.unbounded} == {"ghost", "home"}


def test_pin_loop_reports_an_unwritable_index_cache(tmp_path: Path) -> None:
    lock = (
        'version = 3\n[[package]]\nname = "demo"\nversion = "0.1.0"\n'
        'dependencies = ["home 0.5.9"]\n'
        f'[[package]]\nname = "home"\nversion = "0.5.9"\nsource = "{REGISTRY}"\n'
    )

    class Fixed:
        def generate(self) -> str:
            return lock

        def pin(self, pins: list[Pin]) -> tuple[str, dict[str, str]]:  # pragma: no cover
            raise AssertionError("nothing is late")

    body = (Path(__file__).parent / "fixtures" / "crates-index" / "ho" / "me" / "home").read_bytes()
    http = FakeHttp({("GET", "ho/me/home"): HttpResponse(200, {}, body)})
    blocked = tmp_path / "cache"
    blocked.write_text("not a directory")
    index = SparseIndex(http, blocked)
    result = bound_lockfile(Fixed(), index, when("2024-03-01T00:00:00"), HOME_REQS, print)
    assert result.bounded
    assert result.notes == [f"crates.io index cache {blocked} is not writable (Not a directory)"]


# cargo through a container session


class FakeSession:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = outputs
        self.steps: list[tuple[str, str]] = []

    def run(self, step: str, script: str) -> RunResult:
        self.steps.append((step, script))
        return RunResult(0, self.outputs.pop(0))

    def close(self) -> None:
        return None


LOCK_TEXT = 'version = 3\n\n[[package]]\nname = "demo"\nversion = "0.1.0"\n'


def _output(statuses: list[tuple[str, int, str]], lock: str | None = LOCK_TEXT) -> str:
    text = "".join(
        f"{out}\n--- cargorewind: exit {code} {label} ---\n" for label, code, out in statuses
    )
    if lock is not None:
        text += f"{LOCK_BEGIN}\n{lock}\n{LOCK_END}\n"
    return text


def test_cargo_script_prints_statuses_and_the_lockfile() -> None:
    script = cargo_script([("pin0", "cargo update -p 'home:0.5.12' --precise 0.5.9")])
    assert script.splitlines() == [
        "exec 2>&1",
        "cargo update -p 'home:0.5.12' --precise 0.5.9; echo \"--- cargorewind: exit $? pin0 ---\"",
        f"echo '{LOCK_BEGIN}'; cat Cargo.lock; echo; echo '{LOCK_END}'",
    ]
    lock, statuses = parse_script_output(_output([("pin0", 101, "error: boom")]))
    assert lock == LOCK_TEXT
    assert statuses == {"pin0": (101, "error: boom")}
    assert parse_script_output("no markers") == (None, {})


def test_cargo_driver_generates_and_pins() -> None:
    session = FakeSession(
        [
            _output([("generate", 0, "    Updating crates.io index")]),
            _output([("pin0", 0, "    Updating home"), ("pin1", 101, "error: no match")]),
        ]
    )
    driver = CargoDriver(session)
    assert driver.generate() == LOCK_TEXT
    pins = [Pin("home", "0.5.12", "0.5.9", ""), Pin("either", "1.15.0", "1.9.0", "")]
    lock, failed = driver.pin(pins)
    assert lock == LOCK_TEXT and failed == {"either:1.15.0": "error: no match"}
    assert [step for step, _ in session.steps] == ["generate-lockfile", "pin-round-1"]
    assert "cargo update -p home:0.5.12 --precise 0.5.9" in session.steps[1][1]


def test_cargo_driver_errors() -> None:
    failing = FakeSession([_output([("generate", 101, "error: no network")], lock=None)])
    with pytest.raises(LockError, match="no network"):
        CargoDriver(failing).generate()
    lost = FakeSession(["killed\n"])
    with pytest.raises(LockError, match=r"no Cargo\.lock after pin round 1"):
        CargoDriver(lost).pin([Pin("home", "0.5.12", "0.5.9", "")])
    silent = FakeSession([_output([])])
    _, failed = CargoDriver(silent).pin([Pin("home", "0.5.12", "0.5.9", "")])
    assert failed == {"home:0.5.12": "no exit status"}

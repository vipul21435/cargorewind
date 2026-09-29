from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from itertools import pairwise
from pathlib import Path

import pytest

from cargorewind.layout import MemoryTree
from cargorewind.toolchain import (
    CHANNEL_HOST_VERSION,
    EDITION_MINIMUM,
    IMAGE_DIGESTS,
    STABLE_RELEASES,
    Decision,
    Toolchain,
    ToolchainError,
    ToolchainSpec,
    base_image,
    channel_version,
    manifest_floors,
    newest_stable_before,
    normalize_version,
    parse_toolchain_file,
    parse_version_requirement,
    release_date,
    resolve_toolchain,
    satisfying_release,
    stable_on,
)

FIXTURES = Path(__file__).parent / "fixtures" / "toolchain"
FIX_TIME = datetime(2019, 12, 13, 2, 48, 41, tzinfo=UTC)


def _key(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in version.split("."))


def fixture_tree(name: str) -> MemoryTree:
    root = FIXTURES / name
    return MemoryTree(
        {p.relative_to(root).as_posix(): p.read_text() for p in root.rglob("*") if p.is_file()}
    )


def steps(chosen: Toolchain) -> list[tuple[str, str]]:
    return [(d.step, d.outcome) for d in chosen.decisions]


# The stable release table


def test_table_is_sorted_by_version_and_date() -> None:
    versions = [v for v, _ in STABLE_RELEASES]
    assert versions[0] == "1.0.0"
    assert versions == sorted(versions, key=_key)
    assert len(set(versions)) == len(versions)
    days = [date.fromisoformat(d) for _, d in STABLE_RELEASES]
    assert days == sorted(days)


def test_minor_releases_follow_the_six_week_train() -> None:
    minors = [(v, date.fromisoformat(d)) for v, d in STABLE_RELEASES if v.endswith(".0")]
    assert [_key(v)[1] for v, _ in minors] == list(range(len(minors)))
    gaps = {(b - a).days for (_, a), (_, b) in pairwise(minors)}
    assert gaps <= {41, 42, 43}
    # Releases ship on Thursdays; the three exceptions are real (1.0 was a Friday).
    off_thursday = {v for v, day in minors if day.weekday() != 3}
    assert off_thursday == {"1.0.0", "1.2.0", "1.22.0"}


def test_point_releases_sit_between_their_minor_and_the_next() -> None:
    table = {v: date.fromisoformat(d) for v, d in STABLE_RELEASES}
    points = [v for v in table if not v.endswith(".0")]
    assert len(points) == 41
    for version in points:
        major, minor, patch = _key(version)
        previous = f"{major}.{minor}.{patch - 1}"
        assert previous in table, f"{version} skips a patch number"
        assert table[previous] <= table[version]
        following = f"{major}.{minor + 1}.0"
        if following in table:  # the newest minor has no successor yet
            assert table[version] < table[following]


def test_newest_stable_before_agrees_with_every_row() -> None:
    for version, day in STABLE_RELEASES:
        released = date.fromisoformat(day)
        assert release_date(newest_stable_before(released + timedelta(days=1))) == released
        if version != "1.0.0":
            assert release_date(newest_stable_before(released)) < released
        assert _key(stable_on(released)) >= _key(version)


def test_known_release_dates() -> None:
    assert release_date("1.0.0") == date(2015, 5, 15)
    assert release_date("1.39.0") == date(2019, 11, 7)
    assert release_date("1.40.0") == date(2019, 12, 19)
    with pytest.raises(ToolchainError):
        release_date("1.999.0")


def test_newest_stable_before() -> None:
    assert newest_stable_before(date(2019, 12, 13)) == "1.39.0"
    assert newest_stable_before(date(2019, 12, 19)) == "1.39.0"  # strictly before
    assert newest_stable_before(date(2019, 12, 20)) == "1.40.0"
    assert newest_stable_before(date(2017, 11, 23)) == "1.22.1"
    assert stable_on(date(2019, 12, 19)) == "1.40.0"
    with pytest.raises(ToolchainError, match="no stable"):
        newest_stable_before(date(2015, 5, 15))


def test_normalize_version() -> None:
    assert normalize_version("1.39.0") == "1.39.0"
    assert normalize_version("1.22") == "1.22.1"
    assert normalize_version("stable") is None
    assert normalize_version("nightly-2020-01-01") is None
    with pytest.raises(ToolchainError):
        normalize_version("1.999")


def test_version_requirements_and_channel_versions() -> None:
    assert parse_version_requirement("1.70") == (1, 70, 0)
    assert parse_version_requirement(" 1.72.1 ") == (1, 72, 1)
    assert parse_version_requirement("1.70.0-nightly") is None
    # nightly-2020-01-01 reports rustc 1.42.0-nightly; the beta of that day is 1.41.
    assert channel_version("nightly", date(2020, 1, 1)) == (1, 42, 0)
    assert channel_version("beta", date(2020, 1, 1)) == (1, 41, 0)


def test_satisfying_release_picks_the_newest_patch_before_the_commit() -> None:
    assert satisfying_release((1, 72, 0), date(2024, 1, 1)) == "1.72.1"
    assert satisfying_release((1, 72, 0), date(2023, 9, 1)) == "1.72.0"
    assert satisfying_release((1, 85, 0), date(2025, 2, 20)) == "1.85.0"  # not yet out
    assert satisfying_release((1, 70, 5), date(2024, 1, 1)) == "1.71.1"
    with pytest.raises(ToolchainError, match=r"satisfies 1\.200\.0"):
        satisfying_release((1, 200, 0), date(2024, 1, 1))


def test_edition_minimums_are_real_releases() -> None:
    assert EDITION_MINIMUM == {
        "2015": "1.0.0",
        "2018": "1.31.0",
        "2021": "1.56.0",
        "2024": "1.85.0",
    }
    for version in EDITION_MINIMUM.values():
        release_date(version)


# Toolchain files


@pytest.mark.parametrize(
    ("name", "text", "spec"),
    [
        ("rust-toolchain", "1.39.0\n", ToolchainSpec("1.39.0")),
        ("rust-toolchain", "\n  stable  \n", ToolchainSpec("stable")),
        (
            "rust-toolchain",
            '[toolchain]\nchannel = "1.70.0"\ncomponents = ["clippy"]\n',
            ToolchainSpec("1.70.0", ("clippy",)),
        ),
        (
            "rust-toolchain.toml",
            (FIXTURES / "toml-full" / "rust-toolchain.toml").read_text(),
            ToolchainSpec("1.70.0", ("clippy", "rustfmt"), ("wasm32-unknown-unknown",), "default"),
        ),
        (
            "rust-toolchain.toml",
            '[toolchain]\ntargets = ["x86_64-pc-windows-gnu"]\n',
            ToolchainSpec(None, (), ("x86_64-pc-windows-gnu",)),
        ),
    ],
)
def test_parse_toolchain_file(name: str, text: str, spec: ToolchainSpec) -> None:
    assert parse_toolchain_file(text, name) == spec


@pytest.mark.parametrize(
    ("name", "text", "match"),
    [
        ("rust-toolchain", "", "empty"),
        ("rust-toolchain.toml", "1.39.0\n", "not valid TOML"),
        ("rust-toolchain.toml", "[package]\nname = 'x'\n", r"no \[toolchain\]"),
        ("rust-toolchain", "[toolchain]\n", r"no \[toolchain\]"),
        ("rust-toolchain.toml", '[toolchain]\npath = "/opt/rust"\n', "custom toolchain path"),
        ("rust-toolchain.toml", "[toolchain]\nchannel = 1\n", "must be a string"),
        ("rust-toolchain.toml", '[toolchain]\nprofile = "tiny"\n', "unknown profile"),
        ("rust-toolchain.toml", '[toolchain]\ncomponents = "clippy"\n', "list of strings"),
        ("rust-toolchain.toml", '[toolchain]\ntargets = ["a; rm -rf /"]\n', "invalid name"),
    ],
)
def test_parse_toolchain_file_errors(name: str, text: str, match: str) -> None:
    with pytest.raises(ToolchainError, match=match):
        parse_toolchain_file(text, name)


# Resolution from fixture trees


def test_resolve_by_release_date_without_files() -> None:
    chosen = resolve_toolchain(fixture_tree("plain-2015"), FIX_TIME)
    assert (chosen.version, chosen.source, chosen.image_version) == (
        "1.39.0",
        "release-date",
        "1.39.0",
    )
    assert chosen.reason == "newest stable before 2019-12-13 (1.39.0 released 2019-11-07)"
    assert not chosen.install and chosen.toolchain_file is None
    assert steps(chosen) == [
        ("file", "none"),
        ("date", "1.39.0"),
        ("manifest", "1 package(s)"),
        ("edition", "1.0.0"),
        ("msrv", "none"),
        ("lockfile", "none"),
        ("floor", "1.0.0"),
    ]
    assert "edition 2015 (no edition key)" in chosen.decisions[3].reason


def test_resolve_full_toml_file() -> None:
    chosen = resolve_toolchain(fixture_tree("toml-full"), datetime(2024, 1, 11, tzinfo=UTC))
    assert (chosen.version, chosen.source) == ("1.70.0", "rust-toolchain.toml")
    assert chosen.components == ("clippy", "rustfmt")
    assert chosen.targets == ("wasm32-unknown-unknown",)
    assert chosen.profile == "default"
    assert chosen.toolchain_file == "rust-toolchain.toml"
    assert ("install", "clippy, rustfmt, wasm32-unknown-unknown") in steps(chosen)
    assert ("warning", "profile default") in steps(chosen)
    assert chosen.decisions[0] == Decision(
        "file",
        "rust-toolchain.toml",
        "channel 1.70.0; components clippy, rustfmt; targets wasm32-unknown-unknown; "
        "profile default",
    )


def test_resolve_prefers_legacy_file_like_rustup() -> None:
    chosen = resolve_toolchain(fixture_tree("legacy-and-toml"), FIX_TIME)
    assert (chosen.version, chosen.source) == ("1.56.1", "rust-toolchain")
    assert chosen.reason == "rust-toolchain pins 1.56 (newest 1.56.x is 1.56.1)"
    assert "rustup reads the legacy" in chosen.decisions[0].reason


def test_resolve_raises_a_pin_below_the_msrv() -> None:
    chosen = resolve_toolchain(fixture_tree("msrv-raise"), datetime(2024, 1, 11, tzinfo=UTC))
    assert (chosen.version, chosen.source) == ("1.65.0", "rust-version")
    assert chosen.reason == (
        "raised from 1.60.0 to meet 1.65.0: package msrv-raise (Cargo.toml) sets "
        "rust-version = 1.65"
    )
    assert ("msrv", "1.65.0") in steps(chosen)
    assert ("edition", "1.56.0") in steps(chosen)
    assert chosen.toolchain_file == "rust-toolchain"


def test_resolve_workspace_inheritance_and_edition_floor() -> None:
    tree = fixture_tree("workspace-inherit")
    # One day after 1.85.0: the date rule already meets every floor.
    later = resolve_toolchain(tree, datetime(2025, 2, 21, 9, tzinfo=UTC))
    assert (later.version, later.source) == ("1.85.0", "release-date")
    assert ("floor", "1.85.0") in steps(later)
    # On the release day the date rule gives 1.84.1, and edition 2024 raises it.
    same_day = resolve_toolchain(tree, datetime(2025, 2, 20, 9, tzinfo=UTC))
    assert (same_day.version, same_day.source) == ("1.85.0", "edition")
    assert "package beta (crates/beta/Cargo.toml) sets edition = 2024" in same_day.reason
    decisions = steps(same_day)
    assert ("manifest", "2 package(s)") in decisions  # legacy is excluded, xtask unlisted
    assert ("msrv", "1.74.0") in decisions
    assert ("manifest", "1.64.0") in decisions
    msrv = next(d for d in same_day.decisions if d.step == "msrv")
    assert msrv.reason == (
        "package alpha (crates/alpha/Cargo.toml) inherits rust-version from "
        "[workspace.package] = 1.74"
    )


def test_resolve_dated_nightly_installs_on_the_pinned_host() -> None:
    chosen = resolve_toolchain(fixture_tree("nightly-dated"), datetime(2020, 2, 1, tzinfo=UTC))
    assert (chosen.version, chosen.source) == ("nightly-2020-01-01", "rust-toolchain.toml")
    assert chosen.install and chosen.image_version == CHANNEL_HOST_VERSION
    assert chosen.components == ("rustfmt",)
    decisions = steps(chosen)
    assert ("install", "nightly-2020-01-01") in decisions
    warning = next(d for d in chosen.decisions if d.step == "warning")
    assert warning.outcome == "nightly-2020-01-01 builds 1.42.0-nightly"
    assert "below the floor 1.50.0" in warning.reason


def _tree(channel_file: str, manifest: str = '[package]\nname = "x"\n') -> MemoryTree:
    return MemoryTree({"rust-toolchain": channel_file, "Cargo.toml": manifest})


@pytest.mark.parametrize(
    ("channel", "version", "install", "image"),
    [
        ("stable\n", "1.39.0", False, "1.39.0"),
        ("stable-2019-12-19\n", "1.40.0", False, "1.40.0"),
        ("nightly\n", "nightly-2019-12-12", True, CHANNEL_HOST_VERSION),
        ("beta\n", "beta-2019-12-12", True, CHANNEL_HOST_VERSION),
        ("beta-2019-11-01\n", "beta-2019-11-01", True, CHANNEL_HOST_VERSION),
        ("1.39.0-x86_64-unknown-linux-gnu\n", "1.39.0", False, "1.39.0"),
        (
            "nightly-2019-10-01-aarch64-unknown-linux-gnu\n",
            "nightly-2019-10-01",
            True,
            CHANNEL_HOST_VERSION,
        ),
    ],
)
def test_resolve_channel_forms(channel: str, version: str, install: bool, image: str) -> None:
    chosen = resolve_toolchain(_tree(channel), FIX_TIME)
    assert (chosen.version, chosen.install, chosen.image_version) == (version, install, image)


def test_resolve_dated_channel_that_meets_the_floors() -> None:
    chosen = resolve_toolchain(_tree("nightly-2019-12-01\n"), FIX_TIME)
    floor = next(d for d in chosen.decisions if d.step == "floor")
    assert floor.reason == "nightly-2019-12-01 builds 1.41.0-nightly, which meets every floor"


def test_resolve_host_triple_is_reported() -> None:
    chosen = resolve_toolchain(_tree("1.39.0-x86_64-unknown-linux-gnu\n"), FIX_TIME)
    assert ("channel", "-x86_64-unknown-linux-gnu ignored") in steps(chosen)


def test_resolve_file_without_channel_uses_the_date_rule_with_components() -> None:
    tree = MemoryTree(
        {"rust-toolchain.toml": '[toolchain]\ncomponents = ["rustfmt"]\n', "Cargo.toml": ""}
    )
    chosen = resolve_toolchain(tree, FIX_TIME)
    assert (chosen.version, chosen.source, chosen.components) == (
        "1.39.0",
        "release-date",
        ("rustfmt",),
    )
    assert ("channel", "none") in steps(chosen)
    assert chosen.toolchain_file == "rust-toolchain.toml"


@pytest.mark.parametrize("channel", ["1.39.0-2019-01-01\n", "custom-toolchain\n", "1.999.0\n"])
def test_resolve_rejects_unknown_channels(channel: str) -> None:
    with pytest.raises(ToolchainError):
        resolve_toolchain(_tree(channel), FIX_TIME)


# Manifest floors


def test_manifest_floors_without_manifest_or_with_bad_toml() -> None:
    decisions: list[Decision] = []
    assert manifest_floors(MemoryTree({}), decisions) == []
    assert decisions == [Decision("manifest", "none", "no readable Cargo.toml at the root")]
    decisions = []
    assert manifest_floors(MemoryTree({"Cargo.toml": "[package\n"}), decisions) == []
    assert decisions[0].step == "warning" and "not valid TOML" in decisions[0].reason


def test_manifest_floors_edge_cases() -> None:
    tree = MemoryTree(
        {
            "Cargo.toml": (
                '[package]\nname = "root"\nedition = "2018"\nrust-version = "latest"\n\n'
                '[lib]\nedition = "2021"\n\n[[test]]\nname = "it"\nedition = "2024"\n\n'
                "[target.'cfg(unix)'.dependencies]\nlibc = { workspace = true }\n\n"
                '[workspace]\nmembers = ["broken", "inherits-nothing"]\n'
            ),
            "broken/Cargo.toml": "[package\n",
            "inherits-nothing/Cargo.toml": (
                '[package]\nname = "n"\nrust-version.workspace = true\n'
            ),
        }
    )
    decisions: list[Decision] = []
    floors = manifest_floors(tree, decisions)
    assert sorted((f.step, f.version) for f in floors) == [
        ("edition", (1, 0, 0)),
        ("edition", (1, 31, 0)),
        ("edition", (1, 56, 0)),
        ("edition", (1, 85, 0)),
        ("manifest", (1, 64, 0)),
        ("manifest", (1, 64, 0)),
    ]
    warnings = [(d.outcome, d.reason) for d in decisions if d.step == "warning"]
    assert warnings[0][0] == "broken/Cargo.toml"
    assert ("Cargo.toml", "rust-version 'latest' ignored") in warnings
    assert ("inherits-nothing/Cargo.toml", "rust-version None ignored") in warnings
    assert Decision("msrv", "none", "no package sets rust-version") in decisions


def test_manifest_floors_reject_unknown_editions() -> None:
    tree = MemoryTree({"Cargo.toml": '[package]\nname = "x"\nedition = "2027"\n'})
    with pytest.raises(ToolchainError, match="unknown edition '2027'"):
        manifest_floors(tree, [])


def test_non_string_edition_adds_no_floor() -> None:
    tree = MemoryTree({"Cargo.toml": '[package]\nname = "x"\nedition = 2021\n'})
    assert manifest_floors(tree, []) == []


def test_toolchain_image_version_defaults_to_the_version() -> None:
    assert Toolchain("1.70.0", "test", "why").image_version == "1.70.0"
    assert Toolchain("nightly-2020-01-01", "t", "w", "1.98.1").image_version == "1.98.1"


def test_virtual_workspace_root_has_no_package() -> None:
    tree = MemoryTree({"Cargo.toml": "[workspace]\nmembers = []\n"})
    decisions: list[Decision] = []
    assert manifest_floors(tree, decisions) == []
    assert decisions == [
        Decision("manifest", "0 package(s)", "root package and root workspace members")
    ]


# Images


def test_base_image_is_digest_pinned() -> None:
    assert base_image("1.39.0") == f"rust:1.39.0-slim@{IMAGE_DIGESTS['1.39.0']}"
    assert CHANNEL_HOST_VERSION in IMAGE_DIGESTS
    for version, digest in IMAGE_DIGESTS.items():
        assert version in {v for v, _ in STABLE_RELEASES}
        assert digest.startswith("sha256:") and len(digest) == 71
    with pytest.raises(ToolchainError, match="--registry"):
        base_image("1.40.0")


def test_toolchain_as_dict_lists_decisions() -> None:
    chosen = resolve_toolchain(fixture_tree("plain-2015"), FIX_TIME)
    data = chosen.as_dict()
    assert data["version"] == data["image_version"] == "1.39.0"
    assert data["rustup_install"] is False
    assert data["decisions"][1] == {
        "step": "date",
        "outcome": "1.39.0",
        "reason": "newest stable before 2019-12-13 (1.39.0 released 2019-11-07)",
    }


# Lockfile format floors


@pytest.mark.parametrize(
    ("lock", "outcome", "version", "source"),
    [
        ('[[package]]\nname = "a"\nversion = "0.1.0"\n', "v1", "1.39.0", "release-date"),
        (
            '[[package]]\nname = "a"\nversion = "0.1.0"\nchecksum = "00"\n',
            "v2",
            "1.41.0",
            "Cargo.lock",
        ),
        ("version = 3\n", "v3", "1.53.0", "Cargo.lock"),
        ("version = 4\n", "v4", "1.78.0", "Cargo.lock"),
    ],
)
def test_lockfile_format_raises_the_toolchain_to_a_reader(
    lock: str, outcome: str, version: str, source: str
) -> None:
    tree = MemoryTree({"Cargo.toml": '[package]\nname = "a"\n', "Cargo.lock": lock})
    chosen = resolve_toolchain(tree, FIX_TIME)  # the date rule alone gives 1.39.0
    assert ("lockfile", outcome) in steps(chosen)
    assert (chosen.version, chosen.source) == (version, source)
    if source == "Cargo.lock":
        raise_step = next(d for d in chosen.decisions if d.step == "raise")
        assert f"Cargo.lock format {outcome} needs cargo" in raise_step.reason


def test_lockfile_floor_meets_a_newer_toolchain_and_rejects_unknown_formats() -> None:
    later = datetime(2024, 6, 1, tzinfo=UTC)
    tree = MemoryTree({"Cargo.toml": '[package]\nname = "a"\n', "Cargo.lock": "version = 3\n"})
    chosen = resolve_toolchain(tree, later)
    assert chosen.source == "release-date"
    assert ("floor", "1.53.0") in steps(chosen)
    for bad in ("version = 9\n", "[[package]\n", '[[package]]\nname = "x"\n'):
        with pytest.raises(ToolchainError, match=r"Cargo\.lock"):
            resolve_toolchain(MemoryTree({"Cargo.toml": "", "Cargo.lock": bad}), later)

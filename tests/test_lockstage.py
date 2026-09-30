from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from cargorewind.backend import BuildResult
from cargorewind.crateindex import SparseIndex
from cargorewind.dockerfile import LockStrategy, Recipe
from cargorewind.layout import MemoryTree
from cargorewind.lockstage import (
    cargo_version,
    default_index,
    first_dockerfile,
    plan_lock,
    recipe_for,
    recipe_tag,
    run_pin_loop,
)
from cargorewind.toolchain import Toolchain, ToolchainError
from tests.test_deps import INDEX, FakeCargo
from tests.test_rewind import CargoModelSession

CUTOFF = datetime(2019, 12, 13, 2, 48, 41, tzinfo=UTC)
MANIFEST = '[package]\nname = "demo"\n[dependencies]\nhome = "0.5"\n'


def test_cargo_version_of_stable_and_dated_toolchains() -> None:
    assert cargo_version(Toolchain("1.39.0", "release-date", "")) == (1, 39, 0)
    nightly = Toolchain("nightly-2020-01-01", "rust-toolchain", "", "1.98.1", install=True)
    assert cargo_version(nightly) == (1, 42, 0)


@pytest.mark.parametrize(
    ("files", "strategy", "first_line"),
    [
        (
            {"Cargo.toml": MANIFEST, "Cargo.lock": "version = 3\n"},
            LockStrategy.COMMITTED,
            "lockfile  committed Cargo.lock, format v3 (cargo 1.53.0+ reads it); "
            "cargo fetch --locked",
        ),
        (
            {"Cargo.toml": MANIFEST, "Cargo.lock": ""},
            LockStrategy.COMMITTED,
            "lockfile  committed Cargo.lock, format v1 (every cargo reads it); "
            "cargo fetch --locked",
        ),
        (
            {"Cargo.toml": '[package]\nname = "demo"\n'},
            LockStrategy.GENERATED,
            "lockfile  none, and no crates.io dependencies: cargo generates it in the image",
        ),
        (
            {"Cargo.toml": MANIFEST},
            LockStrategy.BOUNDED,
            "lockfile  none: 1 crates.io requirement(s); bounding every package to before "
            "2019-12-13T02:48:41+00:00",
        ),
    ],
)
def test_plan_lock_strategies(
    files: dict[str, str], strategy: LockStrategy, first_line: str
) -> None:
    plan = plan_lock(MemoryTree(files), Toolchain("1.60.0", "", ""), CUTOFF, vendor=False)
    assert plan.strategy is strategy
    assert plan.lines() == [first_line]


def test_plan_lock_bounds_dependencies_reached_through_path_or_git_crates() -> None:
    # Regression: a root crate whose only dependency is a path crate that depends on
    # regex and serde got the "generated" strategy, so cargo locked today's versions.
    tree = MemoryTree(
        {
            "Cargo.toml": '[package]\nname = "app"\n[dependencies]\n'
            'core-impl = { path = "core", version = "0.1" }\n',
            "core/Cargo.toml": '[package]\nname = "core-impl"\n[dependencies]\n'
            'regex = "1"\nserde = "1"\n',
        }
    )
    plan = plan_lock(tree, Toolchain("1.60.0", "", ""), CUTOFF, vendor=False)
    assert plan.strategy is LockStrategy.BOUNDED
    assert [(r.member, r.name) for r in plan.requirements] == [
        ("core-impl", "regex"),
        ("core-impl", "serde"),
    ]
    git_only = MemoryTree(
        {"Cargo.toml": '[package]\nname = "app"\n[dependencies]\nx = { git = "https://e.x/x" }\n'}
    )
    plan = plan_lock(git_only, Toolchain("1.60.0", "", ""), CUTOFF, vendor=False)
    assert plan.strategy is LockStrategy.BOUNDED
    assert plan.lines() == [
        "lockfile  none: no crates.io requirement in the manifests, but 1 dependency(ies) can "
        "bring some; bounding every package to before 2019-12-13T02:48:41+00:00",
        "note      Cargo.toml: x: a git dependency; cargo resolves its dependencies",
    ]


def test_pin_loop_summary_counts_a_retried_pin(tmp_path: Path) -> None:
    # Regression: the summary dropped a pin that cargo accepted on retry, because the
    # refused attempt in the round before had the same spec.
    cargo = FakeCargo(INDEX, {"demo": [("home", "0.5.4")]}, refuse=frozenset({("home", "0.5.9")}))
    session = CargoModelSession(cargo)

    class Backend:
        def build(
            self, context: Path, tag: str, target: str | None = None, *, no_cache: bool = False
        ) -> BuildResult:
            return BuildResult("sha256:stage", "")

        def open_session(self, tag: str) -> CargoModelSession:
            return session

    (tmp_path / "Dockerfile").write_text("FROM scratch\n")
    plan = plan_lock(
        MemoryTree({"Cargo.toml": '[package]\nname = "demo"\n[dependencies]\nhome = "0.5.4"\n'}),
        Toolchain("1.60.0", "", ""),
        datetime(2024, 3, 1, tzinfo=UTC),
        vendor=False,
    )
    lines: list[str] = []
    bound = run_pin_loop(plan, Backend(), tmp_path, INDEX, lines.append)  # type: ignore[arg-type]
    assert session.closed and bound.bounded
    assert lines[-1] == "lock      1 pin(s) in 2 round(s); every crates.io package is bounded"
    assert [p.to_version for p in bound.pins() if p.name == "home"] == ["0.5.5"]
    assert (tmp_path / "Cargo.lock").read_text() == bound.lockfile


def test_vendor_needs_cargo_vendor_and_picks_the_config_file_name() -> None:
    tree = MemoryTree({"Cargo.toml": MANIFEST})
    old = plan_lock(tree, Toolchain("1.38.0", "", ""), CUTOFF, vendor=True)
    assert old.cargo_config == "config"
    assert old.lines()[-1] == (
        "vendor    cargo vendor, source replacement in ~/.cargo/config, tests --offline"
    )
    assert plan_lock(tree, Toolchain("1.39.0", "", ""), CUTOFF, vendor=True).cargo_config == (
        "config.toml"
    )
    with pytest.raises(ToolchainError, match=r"the toolchain is 1\.36\.0"):
        plan_lock(tree, Toolchain("1.36.0", "", ""), CUTOFF, vendor=True)


def test_notes_about_unboundable_dependencies_are_shown() -> None:
    tree = MemoryTree({"Cargo.toml": MANIFEST + 'corp = { version = "1", registry = "corp" }\n'})
    plan = plan_lock(tree, Toolchain("1.60.0", "", ""), CUTOFF, vendor=False)
    note = "note      Cargo.toml: corp: an alternate registry, not bounded by date"
    assert plan.lines()[-1] == note


def test_default_index_and_recipe_tags(tmp_path: Path) -> None:
    index = default_index(CUTOFF, tmp_path)
    assert isinstance(index, SparseIndex)
    assert (index.fresh_after, index.cache_dir) == (CUTOFF, tmp_path)
    recipe = Recipe("rust:1.39.0-slim@sha256:" + "a" * 64, "1.39.0", "c4cdd9c35dfa")
    tag = recipe_tag("https://github.com/rapidfuzz/strsim-rs", recipe)
    assert tag == f"cargorewind/strsim-rs:{recipe.hash[:16]}"
    other = Recipe(recipe.image, "1.39.0", "c4cdd9c35dfb")
    assert recipe_tag("strsim-rs", other) != tag


def test_recipe_for_a_bounded_plan_starts_with_the_toolchain_stage_only() -> None:
    tree = MemoryTree({"Cargo.toml": MANIFEST})
    toolchain = Toolchain("1.60.0", "release-date", "", toolchain_file="rust-toolchain")
    plan = plan_lock(tree, toolchain, CUTOFF, vendor=False)
    recipe = recipe_for("rust:1.60.0-slim@sha256:" + "b" * 64, toolchain, "abc123", plan)
    assert (recipe.lock, recipe.pin_toolchain, recipe.cutoff) == (
        LockStrategy.BOUNDED,
        True,
        CUTOFF.isoformat(),
    )
    stage = first_dockerfile(recipe)
    assert "FROM toolchain" not in stage and "cargorewind.recipe" not in stage
    committed = plan_lock(
        MemoryTree({"Cargo.toml": MANIFEST, "Cargo.lock": ""}), toolchain, CUTOFF, vendor=False
    )
    whole = first_dockerfile(recipe_for(recipe.image, toolchain, "abc123", committed))
    assert whole.rstrip().splitlines()[-1].startswith("LABEL cargorewind.recipe=")

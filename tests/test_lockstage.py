from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

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
)
from cargorewind.toolchain import Toolchain, ToolchainError

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

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from cargorewind import dockerfile
from cargorewind.dockerfile import (
    LockStrategy,
    Recipe,
    RecipeError,
    render_body,
    render_dockerfile,
    render_toolchain_stage,
    stage_test_command,
)

IMAGE = "rust:1.39.0-slim@sha256:" + "a" * 64


def test_render_without_lockfile_generates_one() -> None:
    text = render_dockerfile(Recipe(IMAGE, "1.39.0", "c4cdd9c35d", lock=LockStrategy.GENERATED))
    lines = text.splitlines()
    assert lines[1] == f"FROM {IMAGE}"
    assert "LABEL project=cargorewind \\" in lines
    assert "ENV CARGO_BUILD_JOBS=2 \\" in lines
    assert "USER rewind" in lines
    assert "RUN cargo generate-lockfile" in lines
    assert "RUN cargo fetch --locked" not in lines
    assert lines[-4] == "RUN cargo test --no-run"
    assert lines[-1].startswith("LABEL cargorewind.recipe=")
    assert text.endswith("\n")


def test_render_with_lockfile_fetches_locked_and_is_deterministic() -> None:
    recipe = Recipe(IMAGE, "1.39.0", "c4cdd9c35d", lock=LockStrategy.COMMITTED)
    text = render_dockerfile(recipe)
    assert "RUN cargo fetch --locked" in text.splitlines()
    assert "generate-lockfile" not in text
    assert render_dockerfile(recipe) == text


def test_plain_stable_image_has_no_rustup_steps() -> None:
    text = render_dockerfile(Recipe(IMAGE, "1.39.0", "c4cdd9c35d", lock=LockStrategy.GENERATED))
    assert "rustup" not in text
    assert "RUSTUP_TOOLCHAIN" not in text


def test_dated_channel_is_installed_and_pinned_before_the_user_switch() -> None:
    recipe = Recipe(
        "rust:1.98.1-slim@sha256:" + "b" * 64,
        "nightly-2020-01-01",
        "abc123",
        lock=LockStrategy.COMMITTED,
        install_toolchain=True,
        components=("rustfmt",),
        targets=("wasm32-unknown-unknown",),
        pin_toolchain=True,
    )
    lines = render_dockerfile(recipe).splitlines()
    install = (
        "RUN rustup toolchain install nightly-2020-01-01 --profile minimal "
        "--component rustfmt --target wasm32-unknown-unknown"
    )
    assert install in lines
    assert "ENV RUSTUP_TOOLCHAIN=nightly-2020-01-01" in lines
    assert lines.index(install) < lines.index("USER rewind")
    assert lines.index("ENV RUSTUP_TOOLCHAIN=nightly-2020-01-01") < lines.index("USER rewind")
    assert "      cargorewind.toolchain=nightly-2020-01-01" in lines


def test_stable_components_targets_and_toolchain_file_pin() -> None:
    recipe = Recipe(
        IMAGE,
        "1.70.0",
        "abc123",
        lock=LockStrategy.COMMITTED,
        components=("clippy", "rustfmt"),
        targets=("wasm32-unknown-unknown",),
        profile="default",
        pin_toolchain=True,
    )
    text = render_dockerfile(recipe)
    assert (
        "RUN rustup component add --toolchain 1.70.0 clippy rustfmt \\\n"
        "    && rustup target add --toolchain 1.70.0 wasm32-unknown-unknown\n"
    ) in text
    assert "ENV RUSTUP_TOOLCHAIN=1.70.0\n" in text
    assert "toolchain install" not in text
    only_pin = render_dockerfile(Recipe(IMAGE, "1.70.0", "abc123", pin_toolchain=True))
    assert "ENV RUSTUP_TOOLCHAIN=1.70.0" in only_pin and "RUN rustup" not in only_pin


def test_date_bounded_lockfile_uses_a_toolchain_stage_and_copies_the_lockfile() -> None:
    recipe = Recipe(
        IMAGE, "1.73.0", "e776ff0", LockStrategy.BOUNDED, cutoff="2023-10-17T21:35:00+00:00"
    )
    lines = render_dockerfile(recipe).splitlines()
    assert lines[1] == f"FROM {IMAGE} AS toolchain"
    stage = lines.index("FROM toolchain")
    assert lines.index("COPY --chown=rewind:rewind repo/ ./") < stage
    assert lines[stage - 1] == ""
    copy = lines.index("COPY --chown=rewind:rewind Cargo.lock ./")
    assert stage < copy < lines.index("RUN cargo fetch --locked")
    assert "# before 2023-10-17T21:35:00+00:00 (see lock.json)." in lines
    assert "generate-lockfile" not in "\n".join(lines)


def test_vendoring_writes_the_source_replacement_and_builds_offline() -> None:
    recipe = Recipe(IMAGE, "1.73.0", "abc123", LockStrategy.COMMITTED, vendor=True)
    text = render_dockerfile(recipe)
    assert (
        "RUN mkdir -p /home/rewind/.cargo \\\n"
        "    && cargo vendor --locked /home/rewind/vendor > /home/rewind/.cargo/config.toml\n"
        "ENV CARGO_NET_OFFLINE=true\n"
    ) in text
    assert "RUN cargo test --no-run --offline\n" in text
    old = Recipe(
        IMAGE, "1.38.0", "abc123", LockStrategy.GENERATED, vendor=True, cargo_config="config"
    )
    old_text = render_dockerfile(old)
    assert "> /home/rewind/.cargo/config\n" in old_text
    assert old_text.index("RUN cargo generate-lockfile") < old_text.index("cargo vendor")
    assert stage_test_command(True) == ("cargo", "test", "--no-fail-fast", "--offline")
    assert stage_test_command(False) == ("cargo", "test", "--no-fail-fast")


# Golden Dockerfiles: every byte of the rendered file is pinned. Regenerate with
# UPDATE_GOLDEN=1 uv run pytest tests/test_dockerfile.py after an intended change.

GOLDEN = Path(__file__).parent / "fixtures" / "dockerfiles"
STRSIM = Recipe(
    "rust:1.39.0-slim@sha256:b47dd7b5f59bea2bc19ac18e81cc6b5b3cfe6c4e40082cab09604b296bca2652",
    "1.39.0",
    "c4cdd9c35dfaf7fa4e5e023d22854180b114dd9c",
    LockStrategy.GENERATED,
    probes=("jaro_same_one_character", "jaro_winkler_same_one_character"),
)
CASES: dict[str, Recipe] = {
    "generated-with-probes": STRSIM,
    "committed-components-pinned": Recipe(
        "rust:1.70.0-slim@sha256:" + "d" * 64,
        "1.70.0",
        "0123456789abcdef0123456789abcdef01234567",
        LockStrategy.COMMITTED,
        components=("clippy", "rustfmt"),
        targets=("wasm32-unknown-unknown",),
        profile="default",
        pin_toolchain=True,
        probes=("parse_header", "MAX_DEPTH", "HeaderError"),
    ),
    "dated-nightly": Recipe(
        "rust:1.98.1-slim@sha256:" + "e" * 64,
        "nightly-2020-01-01",
        "89abcdef0123456789abcdef0123456789abcdef",
        LockStrategy.COMMITTED,
        install_toolchain=True,
        components=("rustfmt",),
        pin_toolchain=True,
    ),
    "bounded-vendored": Recipe(
        "rust:1.73.0-slim@sha256:" + "f" * 64,
        "1.73.0",
        "e776ff0000000000000000000000000000000000",
        LockStrategy.BOUNDED,
        cutoff="2023-10-17T21:35:00+00:00",
        vendor=True,
        lockfile_sha256="1" * 64,
        probes=("check_name",),
    ),
    "old-cargo-vendor-config": Recipe(
        "rust:1.38.0-slim@sha256:" + "0" * 64,
        "1.38.0",
        "abcdef0",
        LockStrategy.GENERATED,
        vendor=True,
        cargo_config="config",
    ),
}


def _golden(name: str, text: str) -> str:
    path = GOLDEN / name
    if os.environ.get("UPDATE_GOLDEN"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return path.read_text()


@pytest.mark.parametrize("name", sorted(CASES))
def test_golden_dockerfiles(name: str) -> None:
    recipe = CASES[name]
    text = render_dockerfile(recipe)
    assert text == _golden(f"{name}.Dockerfile", text)
    assert render_dockerfile(Recipe.from_dict(recipe.as_dict())) == text


def test_golden_toolchain_stage_and_recipe_json() -> None:
    bounded = CASES["bounded-vendored"]
    stage = render_toolchain_stage(bounded)
    assert stage == _golden("bounded-vendored.stage.Dockerfile", stage)
    assert render_dockerfile(bounded).startswith(stage)
    # The stage does not depend on the lockfile the pin loop writes afterwards.
    assert render_toolchain_stage(replace(bounded, lockfile_sha256="")) == stage
    document = json.dumps(STRSIM.document(), indent=2) + "\n"
    assert document == _golden("strsim.recipe.json", document)
    with pytest.raises(RecipeError, match="only a date-bounded recipe"):
        render_toolchain_stage(STRSIM)


def test_recipe_hash_covers_the_canonical_json_and_the_dockerfile_body() -> None:
    canonical = STRSIM.canonical_json()
    assert canonical.startswith('{"base_commit":"c4cdd9c35dfaf7fa4e5e023d22854180b114dd9c",')
    assert " " not in canonical and canonical.isascii()
    assert json.loads(canonical) == STRSIM.as_dict()
    body = render_body(STRSIM)
    assert render_dockerfile(STRSIM).startswith(body) and "cargorewind.recipe" not in body
    expected = hashlib.sha256(f"{canonical}\n{body}".encode()).hexdigest()
    assert STRSIM.hash == expected
    # Pinned: a change here means every cached image and golden file changes too.
    assert STRSIM.hash == "edcbd61bae401cffde7cb88d436491b9ecf7a98368354ae2a9478ec7d82b482a"
    assert render_dockerfile(STRSIM).endswith(f"LABEL cargorewind.recipe={STRSIM.hash}\n")
    document = STRSIM.document()
    assert document["hash"] == STRSIM.hash
    assert document["dockerfile_sha256"] == hashlib.sha256(body.encode()).hexdigest()


def test_a_template_change_is_a_new_recipe(monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression: the hash covered the recipe fields only, so after an upgrade that
    # changed the Dockerfile template a cached image built from the old template was
    # still a cache hit, and the bundle's Dockerfile did not describe the image used.
    before_text, before_hash = render_dockerfile(STRSIM), STRSIM.hash
    real = dockerfile._stage_lines

    def more_jobs(recipe: Recipe) -> list[str]:
        return [line.replace("CARGO_BUILD_JOBS=2", "CARGO_BUILD_JOBS=8") for line in real(recipe)]

    monkeypatch.setattr(dockerfile, "_stage_lines", more_jobs)
    after_text = render_dockerfile(STRSIM)
    assert "CARGO_BUILD_JOBS=8" in after_text and after_text != before_text
    assert STRSIM.hash != before_hash
    assert after_text.endswith(f"LABEL cargorewind.recipe={STRSIM.hash}\n")
    assert STRSIM.canonical_json() == json.dumps(
        STRSIM.as_dict(), sort_keys=True, separators=(",", ":")
    )  # the fields did not change; only the template did


@pytest.mark.parametrize(
    "change",
    [
        {"image": "rust:1.39.0-slim@sha256:" + "9" * 64},
        {"toolchain": "1.39"},
        {"base_commit": "c4cdd9c35dfb"},
        {"lock": LockStrategy.COMMITTED},
        {"pin_toolchain": True},
        {"vendor": True},
        {"probes": ("jaro_same_one_character",)},
        {"test_command": ("cargo", "test")},
        {"warm_command": ("cargo", "build")},
        {"components": ("rustfmt",)},
    ],
)
def test_every_field_changes_the_hash(change: dict[str, Any]) -> None:
    assert replace(STRSIM, **change).hash != STRSIM.hash


def test_equal_recipes_hash_equally_and_defaults_fill_the_commands() -> None:
    again = Recipe(STRSIM.image, "1.39.0", STRSIM.base_commit, "generated", probes=STRSIM.probes)  # type: ignore[arg-type]
    assert again == STRSIM and again.hash == STRSIM.hash
    assert STRSIM.warm_command == ("cargo", "test", "--no-run")
    assert STRSIM.test_command == ("cargo", "test", "--no-fail-fast")
    vendored = replace(STRSIM, vendor=True, warm_command=(), test_command=())
    assert vendored.warm_command[-1] == vendored.test_command[-1] == "--offline"


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"image": "rust:1.39.0-slim"}, "image"),
        ({"image": STRSIM.image + "\nRUN evil"}, "image"),
        ({"toolchain": "1.39.0\n"}, "toolchain"),
        ({"toolchain": "1.39.0 && curl x"}, "toolchain"),
        ({"base_commit": "HEAD"}, "base_commit"),
        ({"components": ("rustfmt\n",)}, "component"),
        ({"targets": ("a;b",)}, "component or target"),
        ({"profile": "tiny"}, "profile"),
        ({"cutoff": "2023-10-17 RUN"}, "cutoff"),
        ({"cargo_config": "../config"}, "cargo_config"),
        ({"lockfile_sha256": "1" * 64}, "only set for a date-bounded"),
        ({"lockfile_sha256": "xyz"}, "lockfile_sha256"),
        ({"probes": ("two words",)}, "probe"),
        ({"probes": ("r#type",)}, "probe"),
        ({"probes": ("same", "same")}, "unique"),
        ({"test_command": ("cargo", "test;", "rm")}, "command word"),
        ({"warm_command": ("cargo", "$(evil)")}, "command word"),
    ],
)
def test_recipe_rejects_values_that_could_inject_dockerfile_lines(
    change: dict[str, Any], match: str
) -> None:
    with pytest.raises(RecipeError, match=match):
        replace(STRSIM, **change)


def test_recipe_from_dict_errors() -> None:
    data = STRSIM.document()
    assert Recipe.from_dict(data) == STRSIM  # the hash key is ignored
    with pytest.raises(RecipeError, match="schema"):
        Recipe.from_dict({**data, "schema": 2})
    with pytest.raises(RecipeError, match="unknown recipe fields: extra"):
        Recipe.from_dict({**data, "extra": 1})
    with pytest.raises(RecipeError, match="incomplete recipe"):
        Recipe.from_dict({"schema": 1, "image": STRSIM.image})
    with pytest.raises(RecipeError, match="lock 'sometimes'"):
        Recipe.from_dict({**data, "lock": "sometimes"})

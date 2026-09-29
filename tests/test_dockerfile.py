from __future__ import annotations

from cargorewind.dockerfile import Recipe, render_dockerfile

IMAGE = "rust:1.39.0-slim@sha256:" + "a" * 64


def test_render_without_lockfile_generates_one() -> None:
    text = render_dockerfile(Recipe(IMAGE, "1.39.0", "c4cdd9c35d", has_lockfile=False))
    lines = text.splitlines()
    assert lines[1] == f"FROM {IMAGE}"
    assert "LABEL project=cargorewind \\" in lines
    assert "ENV CARGO_BUILD_JOBS=2 \\" in lines
    assert "USER rewind" in lines
    assert "RUN cargo generate-lockfile" in lines
    assert "RUN cargo fetch --locked" not in lines
    assert lines[-1] == "RUN cargo test --no-run"
    assert text.endswith("\n")


def test_render_with_lockfile_fetches_locked_and_is_deterministic() -> None:
    recipe = Recipe(IMAGE, "1.39.0", "c4cdd9c35d", has_lockfile=True)
    text = render_dockerfile(recipe)
    assert "RUN cargo fetch --locked" in text.splitlines()
    assert "generate-lockfile" not in text
    assert render_dockerfile(recipe) == text


def test_plain_stable_image_has_no_rustup_steps() -> None:
    text = render_dockerfile(Recipe(IMAGE, "1.39.0", "c4cdd9c35d", has_lockfile=False))
    assert "rustup" not in text
    assert "RUSTUP_TOOLCHAIN" not in text


def test_dated_channel_is_installed_and_pinned_before_the_user_switch() -> None:
    recipe = Recipe(
        "rust:1.98.1-slim@sha256:" + "b" * 64,
        "nightly-2020-01-01",
        "abc123",
        has_lockfile=True,
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
        has_lockfile=True,
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
    only_pin = render_dockerfile(Recipe(IMAGE, "1.70.0", "abc123", True, pin_toolchain=True))
    assert "ENV RUSTUP_TOOLCHAIN=1.70.0" in only_pin and "RUN rustup" not in only_pin

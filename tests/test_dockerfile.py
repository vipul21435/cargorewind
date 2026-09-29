from __future__ import annotations

from cargorewind.dockerfile import LockStrategy, Recipe, render_dockerfile, stage_test_command

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
    assert lines[-1] == "RUN cargo test --no-run"
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
    recipe = Recipe(IMAGE, "1.73.0", "abc", LockStrategy.COMMITTED, vendor=True)
    text = render_dockerfile(recipe)
    assert (
        "RUN mkdir -p /home/rewind/.cargo \\\n"
        "    && cargo vendor --locked /home/rewind/vendor > /home/rewind/.cargo/config.toml\n"
        "ENV CARGO_NET_OFFLINE=true\n"
    ) in text
    assert text.endswith("RUN cargo test --no-run --offline\n")
    old = Recipe(IMAGE, "1.38.0", "abc", LockStrategy.GENERATED, vendor=True, cargo_config="config")
    old_text = render_dockerfile(old)
    assert "> /home/rewind/.cargo/config\n" in old_text
    assert old_text.index("RUN cargo generate-lockfile") < old_text.index("cargo vendor")
    assert stage_test_command(True) == ("cargo", "test", "--no-fail-fast", "--offline")
    assert stage_test_command(False) == ("cargo", "test", "--no-fail-fast")

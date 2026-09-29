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

"""Live end-to-end run through Docker (deselected by default; the CI docker job runs it)."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from cargorewind.backend import DockerBackend
from cargorewind.buildcache import BuildCache
from cargorewind.crateindex import DirectoryIndex
from cargorewind.dockerfile import render_dockerfile
from cargorewind.gitops import Git
from cargorewind.rewind import RewindOptions, rewind
from cargorewind.runner import CommandError, SubprocessRunner
from cargorewind.verify import VerifyOptions, verify

DEMO = Path(__file__).resolve().parents[1] / "examples" / "strsim"


@pytest.mark.docker
def test_live_rewind_of_strsim_fix(tmp_path: Path) -> None:
    runner = SubprocessRunner()
    options = RewindOptions(
        str(DEMO / "strsim-rs.bundle"), "605c81c9b9", tmp_path / "out", tmp_path / "work"
    )
    report = rewind(options, runner, DockerBackend(runner, timeout=1800), print)

    assert report.flip is not None and report.flip.verified
    assert report.flip.fail_to_pass == [
        "tests::jaro_same_one_character",
        "tests::jaro_winkler_same_one_character",
    ]
    assert report.runs["before"].result.exit_code == 101
    recorded = json.loads((DEMO / "transcript.json").read_text())
    passing = len(report.flip.pass_to_pass) + len(report.flip.fail_to_pass)
    assert passing == recorded["runs"]["after"]["output"].count(" ... ok")


WHICH = Path(__file__).resolve().parents[1] / "examples" / "which-rs"


@pytest.mark.docker
def test_live_vendored_rewind_bounds_the_which_rs_lockfile(tmp_path: Path) -> None:
    """which-rs e776ff0 has no Cargo.lock: the pin loop runs cargo for real, the
    environment vendors its dependencies and every stage runs offline."""
    runner = SubprocessRunner()
    options = RewindOptions(
        str(WHICH / "which-rs.bundle"),
        "e776ff0",
        tmp_path / "out",
        tmp_path / "work",
        vendor=True,
        index=DirectoryIndex(WHICH / "index"),
    )
    report = rewind(options, runner, DockerBackend(runner, timeout=1800), print)

    bound = report.lock.bound
    assert bound is not None and bound.bounded
    assert 'name = "home"\nversion = "0.5.5"' in bound.lockfile
    assert all(run.result.exit_code == 0 for run in report.runs.values())
    assert report.flip is not None and len(report.flip.pass_to_pass) == 19
    assert report.flip.fail_to_pass == []  # this fix commit changes no test


@pytest.mark.docker
def test_live_build_cache_reuses_the_labelled_image(tmp_path: Path) -> None:
    """A second rewind of the same recipe finds the image by its recipe label."""
    runner = SubprocessRunner()
    cache = BuildCache(tmp_path / "cache", runner)
    reports = []
    for name in ("one", "two"):
        options = RewindOptions(
            str(DEMO / "strsim-rs.bundle"),
            "605c81c9b9",
            tmp_path / name,
            tmp_path / "work",
            cache=cache,
        )
        reports.append(rewind(options, runner, DockerBackend(runner, timeout=1800), print))
    first, second = reports
    assert second.build.cache == "hit" and second.verified
    assert second.build.tag == first.build.tag
    assert second.image_id == first.image_id
    label = subprocess.run(
        [
            "docker",
            "image",
            "inspect",
            "--format",
            '{{index .Config.Labels "cargorewind.recipe"}}',
            second.build.tag,
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert label == second.recipe.hash
    assert list(cache.entries()) == [second.recipe.hash]


@pytest.mark.docker
def test_live_verify_rebuilds_the_strsim_bundle(tmp_path: Path) -> None:
    """`verify` rebuilds the environment from the bundle's base.bundle and Dockerfile,
    runs the three stages and the reruns, and finds the recorded lists again."""
    runner = SubprocessRunner()
    cache = BuildCache(tmp_path / "cache", runner)
    options = RewindOptions(
        str(DEMO / "strsim-rs.bundle"),
        "605c81c9b9",
        tmp_path / "out",
        tmp_path / "work",
        cache=cache,
    )
    made = rewind(options, runner, DockerBackend(runner, timeout=1800), print)
    assert made.verified
    verified = verify(
        VerifyOptions(tmp_path / "out", tmp_path / "out" / "verify", tmp_path / "vw", cache=cache),
        runner,
        DockerBackend(runner, timeout=1800),
        print,
    )
    assert verified.verified
    assert verified.build is not None and verified.build.cache == "hit"
    assert verified.flip is not None
    assert verified.flip.fail_to_pass == made.flip.fail_to_pass  # type: ignore[union-attr]
    assert [c.ok for c in verified.checks] == [True] * 6
    document = json.loads((tmp_path / "out" / "verify" / "verify.json").read_text())
    assert document["verified"] is True and document["runs"]["before"]["failed"] == 2


@pytest.mark.docker
def test_live_build_fails_when_a_probe_word_exists_at_base(tmp_path: Path) -> None:
    """The Dockerfile probe: a word that is already in the base checkout stops the build."""
    runner = SubprocessRunner()
    options = RewindOptions(
        str(DEMO / "strsim-rs.bundle"), "605c81c9b9", tmp_path / "out", tmp_path / "work"
    )
    report = rewind(options, runner, DockerBackend(runner, timeout=1800), print)
    wrong = replace(report.recipe, probes=("generic_jaro",))  # defined in src/lib.rs at base
    context = tmp_path / "context"
    Git(runner, tmp_path / "work" / "repo").archive(report.base, context / "repo")
    (context / "Dockerfile").write_text(render_dockerfile(wrong))
    with pytest.raises(CommandError, match="cargorewind probe failed: found at base"):
        DockerBackend(runner, timeout=600).build(context, "cargorewind/probe-check:live")

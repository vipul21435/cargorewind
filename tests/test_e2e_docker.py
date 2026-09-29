"""Live end-to-end run through Docker (deselected by default; the CI docker job runs it)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cargorewind.backend import DockerBackend
from cargorewind.rewind import RewindOptions, rewind
from cargorewind.runner import SubprocessRunner

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

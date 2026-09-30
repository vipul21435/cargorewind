from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

from cargorewind.backend import (
    BuildResult,
    DockerBackend,
    Overlay,
    RecordingBackend,
    ReplayBackend,
    ReplayError,
    RunResult,
    container_script,
    probe_step,
    sha256_text,
)
from cargorewind.dockerfile import TEST_COMMAND
from cargorewind.runner import CommandError, CommandResult
from tests.conftest import FakeRunner


def _context(tmp_path: Path, text: str = "FROM scratch\n") -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "Dockerfile").write_text(text)
    return tmp_path


def test_overlay_tar_is_deterministic_and_complete() -> None:
    overlay = Overlay({"src/lib.rs": b"fn a() {}\n", "tests/t.rs": b"x"}, {"tests/t.rs": 0o755})
    tar_bytes = overlay.to_tar()
    assert tar_bytes == Overlay(dict(overlay.files), dict(overlay.modes)).to_tar()
    with tarfile.open(fileobj=io.BytesIO(tar_bytes)) as tar:
        members = {m.name: m for m in tar.getmembers()}
        assert sorted(members) == ["src/lib.rs", "tests/t.rs"]
        assert members["tests/t.rs"].mode == 0o755
        assert members["src/lib.rs"].mtime == 0
        extracted = tar.extractfile("src/lib.rs")
        assert extracted is not None and extracted.read() == b"fn a() {}\n"


def test_overlay_digest_tracks_content_modes_and_deletions() -> None:
    a = Overlay({"f": b"1"})
    assert a.digest() == Overlay({"f": b"1"}).digest()
    assert a.digest() != Overlay({"f": b"2"}).digest()
    assert a.digest() != Overlay({"f": b"1"}, {"f": 0o755}).digest()
    assert a.digest() != Overlay({"f": b"1"}, deleted=("g",)).digest()


def test_container_script() -> None:
    assert container_script(Overlay()) == (
        "cd /home/rewind/repo && exec cargo test --no-fail-fast 2>&1"
    )
    offline = container_script(Overlay(), ("cargo", "test", "--offline"))
    assert offline.endswith("exec cargo test --offline 2>&1")
    script = container_script(Overlay({"a.rs": b""}, deleted=("old file.rs",)))
    assert script == (
        "cd /home/rewind/repo && tar -xmf - && rm -f -- 'old file.rs' && "
        "exec cargo test --no-fail-fast 2>&1"
    )


def test_container_script_checks_probe_words_before_cargo() -> None:
    script = container_script(Overlay({"a.rs": b""}), TEST_COMMAND, ("one", "two_2"))
    assert script == (
        "cd /home/rewind/repo && tar -xmf - && "
        "for w in one two_2; do grep -rqwF --include='*.rs' -e \"$w\" . "
        '|| { echo "cargorewind probe failed: $w is missing"; exit 97; }; done && '
        "exec cargo test --no-fail-fast 2>&1"
    )
    with pytest.raises(ValueError, match="not an identifier"):
        probe_step(("ok", "bad; rm -rf /"))
    with pytest.raises(ValueError, match="not an identifier"):
        probe_step(("trailing\n",))


def test_docker_build_uses_label_and_returns_image_id(tmp_path: Path) -> None:
    runner = FakeRunner(
        [
            (("docker", "build"), CommandResult((), 0, "", "#1 DONE\n")),
            (("docker", "image", "inspect"), CommandResult((), 0, "sha256:abc\n", "")),
        ]
    )
    result = DockerBackend(runner, timeout=5).build(_context(tmp_path), "cargorewind/x:1")
    assert result == BuildResult("sha256:abc", "#1 DONE\n")
    build = runner.calls[0]
    assert build["argv"] == (
        "docker",
        "build",
        "--progress=plain",
        "--label",
        "project=cargorewind",
        "-t",
        "cargorewind/x:1",
        ".",
    )
    assert build["cwd"] == tmp_path
    assert build["timeout"] == 5
    DockerBackend(runner).build(_context(tmp_path), "cargorewind/x:1", no_cache=True)
    assert runner.calls[2]["argv"][-2:] == ("--no-cache", ".")  # type: ignore[index]


def test_docker_build_failure_raises(tmp_path: Path) -> None:
    runner = FakeRunner([(("docker", "build"), CommandResult((), 1, "", "error: no\n"))])
    with pytest.raises(CommandError, match="error: no"):
        DockerBackend(runner).build(_context(tmp_path), "t")


def test_docker_run_streams_overlay_and_isolates_network() -> None:
    runner = FakeRunner([(("docker", "run"), CommandResult((), 101, "test a ... FAILED\n", ""))])
    overlay = Overlay({"src/lib.rs": b"x"})
    result = DockerBackend(runner).run_tests("img", "before", overlay)
    assert result == RunResult(101, "test a ... FAILED\n")
    call = runner.calls[0]
    argv = call["argv"]
    assert isinstance(argv, tuple)
    assert argv[:3] == ("docker", "run", "--rm")
    assert argv[argv.index("--network") : argv.index("--network") + 2] == ("--network", "none")
    assert "CARGO_NET_OFFLINE=true" in argv
    assert call["stdin"] == overlay.to_tar()
    DockerBackend(runner).run_tests("img", "after", overlay, TEST_COMMAND, ("fix_word",))
    assert "for w in fix_word;" in runner.calls[1]["argv"][-1]  # type: ignore[index]


def test_docker_run_without_overlay_sends_no_stdin_and_kills_on_timeout() -> None:
    runner = FakeRunner([(("docker", "run"), CommandResult((), 124, "", "", timed_out=True))])
    result = DockerBackend(runner).run_tests("img", "base", Overlay())
    assert result.timed_out
    assert runner.calls[0]["stdin"] is None
    assert runner.calls[1]["argv"][:3] == ("docker", "rm", "-f")  # type: ignore[index]


class _StubSession:
    def __init__(self) -> None:
        self.closed = False

    def run(self, step: str, script: str) -> RunResult:
        return RunResult(0, f"{step}: {script.strip()}\n")

    def close(self) -> None:
        self.closed = True


class _StubBackend:
    def __init__(self) -> None:
        self.session = _StubSession()

    def build(
        self, context: Path, tag: str, target: str | None = None, *, no_cache: bool = False
    ) -> BuildResult:
        return BuildResult(f"sha256:img-{target or 'final'}", "log")

    def run_tests(
        self,
        tag: str,
        stage: str,
        overlay: Overlay,
        command: tuple[str, ...] = (),
        probes: tuple[str, ...] = (),
    ) -> RunResult:
        return RunResult(0, f"test {stage} ... ok\n")

    def open_session(self, tag: str) -> _StubSession:
        return self.session


def test_record_then_replay_round_trip(tmp_path: Path) -> None:
    context = _context(tmp_path / "ctx")
    transcript = tmp_path / "rec" / "transcript.json"
    recorder = RecordingBackend(_StubBackend(), transcript)
    overlay = Overlay({"a": b"1"})
    recorder.build(context, "t")
    recorder.run_tests("t", "before", overlay)

    data = json.loads(transcript.read_text())
    assert data["schema"] == 1
    assert data["build"]["image_id"] == "sha256:img-final"
    assert data["runs"]["before"]["overlay_sha256"] == overlay.digest()
    script = container_script(overlay, TEST_COMMAND)
    assert data["runs"]["before"]["script_sha256"] == sha256_text(script)
    assert "steps" not in data  # no session, no steps: old transcripts stay unchanged

    replay = ReplayBackend(transcript)
    assert replay.build(context, "other-tag").image_id == "sha256:img-final"
    assert replay.run_tests("t", "before", overlay) == RunResult(0, "test before ... ok\n")


def test_record_then_replay_stage_builds_and_session_steps(tmp_path: Path) -> None:
    context = _context(tmp_path / "ctx")
    transcript = tmp_path / "transcript.json"
    stub = _StubBackend()
    recorder = RecordingBackend(stub, transcript)
    assert recorder.build(context, "stage", "toolchain").image_id == "sha256:img-toolchain"
    session = recorder.open_session("stage")
    assert session.run("generate-lockfile", "cargo generate-lockfile\n") == RunResult(
        0, "generate-lockfile: cargo generate-lockfile\n"
    )
    session.close()
    assert stub.session.closed
    data = json.loads(transcript.read_text())
    assert data["build:toolchain"]["image_id"] == "sha256:img-toolchain"
    assert set(data["steps"]) == {"generate-lockfile"}

    replay = ReplayBackend(transcript)
    assert replay.build(context, "x", "toolchain").image_id == "sha256:img-toolchain"
    replayed = replay.open_session("x")
    assert replayed.run("generate-lockfile", "cargo generate-lockfile\n").exit_code == 0
    replayed.close()
    with pytest.raises(ReplayError, match="step 'generate-lockfile' differs"):
        replayed.run("generate-lockfile", "cargo generate-lockfile -v\n")
    with pytest.raises(ReplayError, match="no recorded session step 'pin-round-1'"):
        replayed.run("pin-round-1", "")
    with pytest.raises(ReplayError, match=r"no recorded build$"):
        replay.build(context, "x")


def test_docker_build_with_a_target_and_a_session() -> None:
    runner = FakeRunner(
        [
            (("docker", "image", "inspect"), CommandResult((), 0, "sha256:abc\n", "")),
            (("docker", "exec"), CommandResult((), 0, "out\n", "err\n")),
        ]
    )
    backend = DockerBackend(runner, timeout=9)
    context = Path("/ctx")
    assert backend.build(context, "tag", "toolchain").image_id == "sha256:abc"
    build = runner.calls[0]["argv"]
    assert isinstance(build, tuple)
    assert build[-4:] == ("tag", "--target", "toolchain", ".")
    session = backend.open_session("tag")
    started = runner.calls[2]["argv"]
    assert isinstance(started, tuple)
    assert started[:4] == ("docker", "run", "-d", "--rm")
    assert started[-3:] == ("tag", "sleep", "infinity")
    name = started[started.index("--name") + 1]
    assert session.run("s", "echo hi") == RunResult(0, "out\nerr\n")
    assert runner.calls[3]["argv"] == ("docker", "exec", name, "sh", "-c", "echo hi")
    assert runner.calls[3]["timeout"] == 9
    session.close()
    assert runner.calls[4]["argv"] == ("docker", "rm", "-f", name)
    other = backend.open_session("tag")
    assert isinstance(other, type(session)) and other.name != name  # type: ignore[attr-defined]


def test_replay_rejects_drifted_inputs(tmp_path: Path) -> None:
    context = _context(tmp_path / "ctx")
    transcript = tmp_path / "transcript.json"
    recorder = RecordingBackend(_StubBackend(), transcript)
    recorder.build(context, "t")
    recorder.run_tests("t", "before", Overlay({"a": b"1"}))
    replay = ReplayBackend(transcript)

    with pytest.raises(ReplayError, match="files for stage"):
        replay.run_tests("t", "before", Overlay({"a": b"2"}))
    with pytest.raises(ReplayError, match="script of stage 'before' differs"):
        replay.run_tests("t", "before", Overlay({"a": b"1"}), TEST_COMMAND, ("probe_word",))
    # A transcript recorded before stage scripts were hashed still replays.
    data = json.loads(transcript.read_text())
    del data["runs"]["before"]["script_sha256"]
    transcript.write_text(json.dumps(data))
    old = ReplayBackend(transcript).run_tests("t", "before", Overlay({"a": b"1"}), probes=("w",))
    assert old.exit_code == 0
    with pytest.raises(ReplayError, match="no recorded run"):
        replay.run_tests("t", "after", Overlay())
    (context / "Dockerfile").write_text("FROM other\n")
    with pytest.raises(ReplayError, match="Dockerfile differs"):
        replay.build(context, "t")


def test_replay_rejects_bad_transcripts(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema": 99}))
    with pytest.raises(ReplayError, match="schema"):
        ReplayBackend(bad)
    with pytest.raises(ReplayError, match="cannot read"):
        ReplayBackend(tmp_path / "missing.json")
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"schema": 1, "build": None, "runs": {}}))
    with pytest.raises(ReplayError, match="no recorded build"):
        ReplayBackend(empty).build(_context(tmp_path / "c"), "t")

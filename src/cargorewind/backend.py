"""Container backends: build the environment image and run the test suite in it.

``DockerBackend`` drives the docker CLI through a ``Runner``. ``RecordingBackend`` wraps
it and writes a transcript of every build and test run; ``ReplayBackend`` answers from
such a transcript, offline, after checking that the Dockerfile and the file overlay are
byte-identical to the recorded ones.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shlex
import tarfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from cargorewind import __version__
from cargorewind.dockerfile import PROJECT_LABEL, REPO_DIR, TEST_COMMAND
from cargorewind.runner import Runner, checked

TRANSCRIPT_SCHEMA = 1


class ReplayError(RuntimeError):
    """The replay transcript does not match the current inputs."""


@dataclass(frozen=True)
class Overlay:
    """Files to write over the base checkout (and files to delete) before a run."""

    files: dict[str, bytes] = field(default_factory=dict)
    modes: dict[str, int] = field(default_factory=dict)
    deleted: tuple[str, ...] = ()

    def digest(self) -> str:
        sha = hashlib.sha256()
        for path in sorted(self.files):
            content = hashlib.sha256(self.files[path]).hexdigest()
            sha.update(f"F {path} {self.modes.get(path, 0o644):o} {content}\n".encode())
        for path in sorted(self.deleted):
            sha.update(f"D {path}\n".encode())
        return sha.hexdigest()

    def to_tar(self) -> bytes:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for path in sorted(self.files):
                data = self.files[path]
                info = tarfile.TarInfo(path)
                info.size = len(data)
                info.mode = self.modes.get(path, 0o644)
                info.mtime = 0
                tar.addfile(info, io.BytesIO(data))
        return buffer.getvalue()


@dataclass(frozen=True)
class BuildResult:
    image_id: str
    log: str


@dataclass(frozen=True)
class RunResult:
    exit_code: int
    output: str
    timed_out: bool = False


class Backend(Protocol):
    def build(self, context: Path, tag: str) -> BuildResult: ...

    def run_tests(self, tag: str, stage: str, overlay: Overlay) -> RunResult: ...


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def container_script(overlay: Overlay) -> str:
    """Shell script run in the container: unpack the overlay, then run the tests."""
    steps = [f"cd {REPO_DIR}"]
    if overlay.files:
        steps.append("tar -xmf -")  # -m: fresh mtimes, so cargo rebuilds changed files
    if overlay.deleted:
        steps.append("rm -f -- " + " ".join(shlex.quote(p) for p in overlay.deleted))
    steps.append("exec " + " ".join(TEST_COMMAND) + " 2>&1")
    return " && ".join(steps)


class DockerBackend:
    def __init__(self, runner: Runner, *, timeout: float = 3600.0) -> None:
        self.runner = runner
        self.timeout = timeout

    def build(self, context: Path, tag: str) -> BuildResult:
        result = self.runner.run(
            ["docker", "build", "--progress=plain", "--label", PROJECT_LABEL, "-t", tag, "."],
            cwd=context,
            timeout=self.timeout,
        )
        checked(result)
        inspect = self.runner.run(["docker", "image", "inspect", "--format", "{{.Id}}", tag])
        image_id = checked(inspect).stdout.strip()
        return BuildResult(image_id, result.stdout + result.stderr)

    def run_tests(self, tag: str, stage: str, overlay: Overlay) -> RunResult:
        name = f"cargorewind-{stage}-{os.getpid()}"
        argv = [
            "docker",
            "run",
            "--rm",
            "-i",
            "--name",
            name,
            "--network",
            "none",
            "--label",
            PROJECT_LABEL,
            "-e",
            "CARGO_NET_OFFLINE=true",
            tag,
            "sh",
            "-c",
            container_script(overlay),
        ]
        stdin = overlay.to_tar() if overlay.files else None
        result = self.runner.run(argv, stdin=stdin, timeout=self.timeout)
        if result.timed_out:
            self.runner.run(["docker", "rm", "-f", name])
        return RunResult(result.returncode, result.stdout + result.stderr, result.timed_out)


def _dockerfile_digest(context: Path) -> str:
    return sha256_text((context / "Dockerfile").read_text())


class RecordingBackend:
    """Delegate to ``inner`` and write every outcome to a JSON transcript."""

    def __init__(self, inner: Backend, path: Path) -> None:
        self.inner = inner
        self.path = path
        self.data: dict[str, Any] = {
            "schema": TRANSCRIPT_SCHEMA,
            "recorded_with": f"cargorewind {__version__}",
            "build": None,
            "runs": {},
        }

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n")

    def build(self, context: Path, tag: str) -> BuildResult:
        result = self.inner.build(context, tag)
        self.data["build"] = {
            "dockerfile_sha256": _dockerfile_digest(context),
            "image_id": result.image_id,
        }
        self._save()
        return result

    def run_tests(self, tag: str, stage: str, overlay: Overlay) -> RunResult:
        result = self.inner.run_tests(tag, stage, overlay)
        self.data["runs"][stage] = {"overlay_sha256": overlay.digest(), **asdict(result)}
        self._save()
        return result


class ReplayBackend:
    """Answer builds and runs from a transcript written by ``RecordingBackend``."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise ReplayError(f"{path}: cannot read transcript: {exc}") from exc
        if data.get("schema") != TRANSCRIPT_SCHEMA:
            raise ReplayError(f"{path}: unsupported transcript schema {data.get('schema')!r}")
        self.data: dict[str, Any] = data

    def build(self, context: Path, tag: str) -> BuildResult:
        recorded = self.data.get("build")
        if not recorded:
            raise ReplayError(f"{self.path}: no recorded build")
        if recorded["dockerfile_sha256"] != _dockerfile_digest(context):
            raise ReplayError("Dockerfile differs from the recorded run; record it again")
        return BuildResult(str(recorded["image_id"]), f"replayed from {self.path.name}\n")

    def run_tests(self, tag: str, stage: str, overlay: Overlay) -> RunResult:
        recorded = self.data["runs"].get(stage)
        if recorded is None:
            raise ReplayError(f"{self.path}: no recorded run for stage {stage!r}")
        if recorded["overlay_sha256"] != overlay.digest():
            raise ReplayError(f"files for stage {stage!r} differ from the recorded run")
        return RunResult(
            int(recorded["exit_code"]), str(recorded["output"]), bool(recorded["timed_out"])
        )

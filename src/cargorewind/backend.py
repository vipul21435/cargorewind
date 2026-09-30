"""Container backends: build the environment image and run the test suite in it.

``DockerBackend`` drives the docker CLI through a ``Runner``. ``RecordingBackend`` wraps
it and writes a transcript of every build, test run and session step;
``ReplayBackend`` answers from such a transcript, offline, after checking that the
Dockerfile, the file overlay and each session script are byte-identical to the
recorded ones.

A session is a long-lived container of an image (``docker run -d ... sleep infinity``)
that runs shell scripts with ``docker exec``; the pin loop uses one so the registry
index is downloaded once for all of its cargo commands.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shlex
import tarfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from cargorewind import __version__
from cargorewind.dockerfile import (
    PROBE_EXIT_CODE,
    PROBE_GLOB,
    PROBE_MARKER,
    PROJECT_LABEL,
    REPO_DIR,
    TEST_COMMAND,
)
from cargorewind.runner import Runner, checked

_sessions = 0

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


class Session(Protocol):
    """A running container that executes shell scripts in the base checkout."""

    def run(self, step: str, script: str) -> RunResult: ...

    def close(self) -> None: ...


class Backend(Protocol):
    def build(
        self, context: Path, tag: str, target: str | None = None, *, no_cache: bool = False
    ) -> BuildResult: ...

    def run_tests(
        self,
        tag: str,
        stage: str,
        overlay: Overlay,
        command: tuple[str, ...] = TEST_COMMAND,
        probes: tuple[str, ...] = (),
    ) -> RunResult: ...

    def open_session(self, tag: str) -> Session: ...


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


_PROBE_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def probe_step(probes: tuple[str, ...]) -> str:
    """Shell loop that exits ``PROBE_EXIT_CODE`` unless every probe word is in the checkout."""
    for word in probes:
        if _PROBE_WORD.fullmatch(word) is None:
            raise ValueError(f"probe {word!r} is not an identifier")
    return (
        f"for w in {' '.join(probes)}; do grep -rqwF --include='{PROBE_GLOB}' -e \"$w\" . "
        f'|| {{ echo "{PROBE_MARKER} $w is missing"; exit {PROBE_EXIT_CODE}; }}; done'
    )


def container_script(
    overlay: Overlay, command: tuple[str, ...] = TEST_COMMAND, probes: tuple[str, ...] = ()
) -> str:
    """Shell script run in the container: unpack the overlay, check that the probe
    identifiers are there, then run the tests."""
    steps = [f"cd {REPO_DIR}"]
    if overlay.files:
        steps.append("tar -xmf -")  # -m: fresh mtimes, so cargo rebuilds changed files
    if overlay.deleted:
        steps.append("rm -f -- " + " ".join(shlex.quote(p) for p in overlay.deleted))
    if probes:
        steps.append(probe_step(probes))
    steps.append("exec " + " ".join(command) + " 2>&1")
    return " && ".join(steps)


class DockerSession:
    """``docker run -d <image> sleep infinity``, then one ``docker exec`` per script."""

    def __init__(self, runner: Runner, tag: str, timeout: float) -> None:
        global _sessions  # noqa: PLW0603 - a per-process counter keeps names unique
        _sessions += 1
        self.runner = runner
        self.timeout = timeout
        self.name = f"cargorewind-session-{os.getpid()}-{_sessions}"
        argv = ["docker", "run", "-d", "--rm", "--name", self.name, "--label", PROJECT_LABEL]
        checked(self.runner.run([*argv, tag, "sleep", "infinity"], timeout=timeout))

    def run(self, step: str, script: str) -> RunResult:
        result = self.runner.run(
            ["docker", "exec", self.name, "sh", "-c", script], timeout=self.timeout
        )
        return RunResult(result.returncode, result.stdout + result.stderr, result.timed_out)

    def close(self) -> None:
        self.runner.run(["docker", "rm", "-f", self.name])


class DockerBackend:
    def __init__(self, runner: Runner, *, timeout: float = 3600.0) -> None:
        self.runner = runner
        self.timeout = timeout

    def build(
        self, context: Path, tag: str, target: str | None = None, *, no_cache: bool = False
    ) -> BuildResult:
        argv = ["docker", "build", "--progress=plain", "--label", PROJECT_LABEL, "-t", tag]
        if target is not None:
            argv += ["--target", target]
        if no_cache:
            argv.append("--no-cache")
        result = self.runner.run([*argv, "."], cwd=context, timeout=self.timeout)
        checked(result)
        inspect = self.runner.run(["docker", "image", "inspect", "--format", "{{.Id}}", tag])
        image_id = checked(inspect).stdout.strip()
        return BuildResult(image_id, result.stdout + result.stderr)

    def open_session(self, tag: str) -> Session:
        return DockerSession(self.runner, tag, self.timeout)

    def run_tests(
        self,
        tag: str,
        stage: str,
        overlay: Overlay,
        command: tuple[str, ...] = TEST_COMMAND,
        probes: tuple[str, ...] = (),
    ) -> RunResult:
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
            container_script(overlay, command, probes),
        ]
        stdin = overlay.to_tar() if overlay.files else None
        result = self.runner.run(argv, stdin=stdin, timeout=self.timeout)
        if result.timed_out:
            self.runner.run(["docker", "rm", "-f", name])
        return RunResult(result.returncode, result.stdout + result.stderr, result.timed_out)


def _dockerfile_digest(context: Path) -> str:
    return sha256_text((context / "Dockerfile").read_text())


def _build_key(target: str | None) -> str:
    return "build" if target is None else f"build:{target}"


class _RecordingSession:
    def __init__(self, owner: RecordingBackend, inner: Session) -> None:
        self.owner = owner
        self.inner = inner

    def run(self, step: str, script: str) -> RunResult:
        result = self.inner.run(step, script)
        self.owner.data["steps"][step] = {"script_sha256": sha256_text(script), **asdict(result)}
        self.owner.save()
        return result

    def close(self) -> None:
        self.inner.close()


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

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n")

    def build(
        self, context: Path, tag: str, target: str | None = None, *, no_cache: bool = False
    ) -> BuildResult:
        result = self.inner.build(context, tag, target, no_cache=no_cache)
        self.data[_build_key(target)] = {
            "dockerfile_sha256": _dockerfile_digest(context),
            "image_id": result.image_id,
        }
        self.save()
        return result

    def open_session(self, tag: str) -> Session:
        self.data.setdefault("steps", {})
        return _RecordingSession(self, self.inner.open_session(tag))

    def run_tests(
        self,
        tag: str,
        stage: str,
        overlay: Overlay,
        command: tuple[str, ...] = TEST_COMMAND,
        probes: tuple[str, ...] = (),
    ) -> RunResult:
        result = self.inner.run_tests(tag, stage, overlay, command, probes)
        self.data["runs"][stage] = {
            "overlay_sha256": overlay.digest(),
            "script_sha256": sha256_text(container_script(overlay, command, probes)),
            **asdict(result),
        }
        self.save()
        return result


class _ReplaySession:
    def __init__(self, owner: ReplayBackend) -> None:
        self.owner = owner

    def run(self, step: str, script: str) -> RunResult:
        recorded = self.owner.data.get("steps", {}).get(step)
        if recorded is None:
            raise ReplayError(f"{self.owner.path}: no recorded session step {step!r}")
        if recorded["script_sha256"] != sha256_text(script):
            raise ReplayError(f"session step {step!r} differs from the recorded run")
        return RunResult(
            int(recorded["exit_code"]), str(recorded["output"]), bool(recorded["timed_out"])
        )

    def close(self) -> None:
        return None


class ReplayBackend:
    """Answer builds, runs and session steps from a transcript of ``RecordingBackend``."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise ReplayError(f"{path}: cannot read transcript: {exc}") from exc
        if data.get("schema") != TRANSCRIPT_SCHEMA:
            raise ReplayError(f"{path}: unsupported transcript schema {data.get('schema')!r}")
        self.data: dict[str, Any] = data

    def build(
        self, context: Path, tag: str, target: str | None = None, *, no_cache: bool = False
    ) -> BuildResult:
        recorded = self.data.get(_build_key(target))
        if not recorded:
            raise ReplayError(f"{self.path}: no recorded {_build_key(target)}")
        if recorded["dockerfile_sha256"] != _dockerfile_digest(context):
            raise ReplayError("Dockerfile differs from the recorded run; record it again")
        return BuildResult(str(recorded["image_id"]), f"replayed from {self.path.name}\n")

    def open_session(self, tag: str) -> Session:
        return _ReplaySession(self)

    def run_tests(
        self,
        tag: str,
        stage: str,
        overlay: Overlay,
        command: tuple[str, ...] = TEST_COMMAND,
        probes: tuple[str, ...] = (),
    ) -> RunResult:
        recorded = self.data["runs"].get(stage)
        if recorded is None:
            raise ReplayError(f"{self.path}: no recorded run for stage {stage!r}")
        if recorded["overlay_sha256"] != overlay.digest():
            raise ReplayError(f"files for stage {stage!r} differ from the recorded run")
        script = sha256_text(container_script(overlay, command, probes))
        if recorded.get("script_sha256", script) != script:
            raise ReplayError(f"the script of stage {stage!r} differs from the recorded run")
        return RunResult(
            int(recorded["exit_code"]), str(recorded["output"]), bool(recorded["timed_out"])
        )

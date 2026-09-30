from __future__ import annotations

import json
import threading
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cargorewind.backend import BuildResult, Overlay, RunResult, Session
from cargorewind.buildcache import (
    INDEX_FILE,
    BuildCache,
    BuildCacheError,
    BuildRequest,
    FileLock,
)
from cargorewind.runner import CommandError, CommandResult
from tests.conftest import FakeRunner

RECIPE = "a" * 64
OTHER = "b" * 64
TAG = f"cargorewind/demo:{RECIPE[:16]}"
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


class FakeDocker:
    """Answers docker image inspect and prune from an in-memory image table."""

    def __init__(self) -> None:
        self.images: dict[str, tuple[str, str]] = {}  # tag -> (image id, recipe label)
        self.calls: list[tuple[str, ...]] = []
        self.pruned = "Deleted Images:\nTotal reclaimed space: 1.2GB"

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        stdin: bytes | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        args = tuple(argv)
        self.calls.append(args)
        if args[:3] == ("docker", "image", "inspect"):
            if args[-1] not in self.images:
                return CommandResult(args, 1, "", f"Error: No such image: {args[-1]}\n")
            image_id, label = self.images[args[-1]]
            return CommandResult(args, 0, f"{image_id} {label or '<no value>'}\n", "")
        if args[:3] == ("docker", "image", "prune"):
            return CommandResult(args, 0, self.pruned + "\n", "")
        raise AssertionError(f"unexpected command {args}")


class FakeBuilder:
    """A backend whose builds register an image with the recipe label in FakeDocker."""

    def __init__(self, docker: FakeDocker, label: str = RECIPE) -> None:
        self.docker = docker
        self.label = label
        self.calls: list[tuple[str, bool]] = []
        self.gate: threading.Event | None = None
        self.started = threading.Event()

    def build(
        self, context: Path, tag: str, target: str | None = None, *, no_cache: bool = False
    ) -> BuildResult:
        self.calls.append((tag, no_cache))
        self.started.set()
        if self.gate is not None:
            assert self.gate.wait(5)
        image_id = f"sha256:img{len(self.calls)}"
        self.docker.images[tag] = (image_id, self.label)
        return BuildResult(image_id, "built\n")

    def run_tests(
        self,
        tag: str,
        stage: str,
        overlay: Overlay,
        command: tuple[str, ...] = (),
        probes: tuple[str, ...] = (),
    ) -> RunResult:
        raise NotImplementedError

    def open_session(self, tag: str) -> Session:
        raise NotImplementedError


def _request(tag: str = TAG, recipe: str = RECIPE) -> BuildRequest:
    return BuildRequest(tag, recipe, "examples/demo.bundle", "c" * 40, "1.39.0")


def _cache(tmp_path: Path, docker: FakeDocker, **kwargs: float) -> BuildCache:
    return BuildCache(tmp_path / "cache", docker, now=lambda: NOW, poll=0.02, **kwargs)


def test_miss_builds_and_indexes_then_hit_reuses(tmp_path: Path) -> None:
    docker = FakeDocker()
    builder = FakeBuilder(docker)
    cache = _cache(tmp_path, docker)

    first = cache.build(builder, tmp_path, _request())
    assert (first.hit, first.tag, first.result.image_id) == (False, TAG, "sha256:img1")
    assert first.reason.startswith("no image for this recipe; built in ")
    entry = cache.entries()[RECIPE]
    assert (entry.tag, entry.image_id, entry.built_at) == (TAG, "sha256:img1", NOW.isoformat())
    assert (entry.repo, entry.base_commit, entry.toolchain) == (
        "examples/demo.bundle",
        "c" * 40,
        "1.39.0",
    )
    document = json.loads((tmp_path / "cache" / INDEX_FILE).read_text())
    assert document["schema_version"] == 1 and list(document["images"]) == [RECIPE]

    second = cache.build(builder, tmp_path, _request())
    assert second.hit and second.result.image_id == "sha256:img1"
    assert second.reason.startswith(f"image {TAG} built 2026-09-30T12:00:00+00:00 in ")
    assert builder.calls == [(TAG, False)]  # built once
    inspect = docker.calls[-1]
    assert inspect[:4] == ("docker", "image", "inspect", "--format")
    assert inspect[4] == '{{.Id}} {{index .Config.Labels "cargorewind.recipe"}}'


def test_a_missing_or_foreign_image_is_rebuilt(tmp_path: Path) -> None:
    docker = FakeDocker()
    builder = FakeBuilder(docker)
    cache = _cache(tmp_path, docker)
    cache.build(builder, tmp_path, _request())
    del docker.images[TAG]  # removed with docker rmi
    assert not cache.build(builder, tmp_path, _request()).hit
    docker.images[TAG] = ("sha256:foreign", OTHER)  # the tag now names another recipe
    assert cache.lookup(TAG, RECIPE) is None
    docker.images[TAG] = ("sha256:unlabelled", "")
    assert cache.lookup(TAG, RECIPE) is None
    assert len(builder.calls) == 2
    blank = FakeRunner([(("docker", "image", "inspect"), CommandResult((), 0, "\n", ""))])
    assert BuildCache(tmp_path, blank).inspect(TAG) is None  # docker printed nothing useful


def test_an_image_found_in_docker_is_indexed_again(tmp_path: Path) -> None:
    docker = FakeDocker()
    docker.images[TAG] = ("sha256:kept", RECIPE)  # built earlier, index since deleted
    cache = _cache(tmp_path, docker)
    hit = cache.build(FakeBuilder(docker), tmp_path, _request())
    assert hit.hit and hit.reason == f"image {TAG} found in Docker and indexed again"
    assert cache.entries()[RECIPE].image_id == "sha256:kept"
    # The indexed tag is tried first, even when a run asks for another tag.
    other_tag = cache.build(FakeBuilder(docker), tmp_path, _request("cargorewind/fork:x"))
    assert other_tag.hit and other_tag.tag == TAG


def test_rebuild_skips_the_lookup_builds_without_cache_and_prunes(tmp_path: Path) -> None:
    docker = FakeDocker()
    builder = FakeBuilder(docker)
    cache = _cache(tmp_path, docker)
    cache.build(builder, tmp_path, _request())
    again = cache.build(builder, tmp_path, _request(), rebuild=True)
    assert not again.hit and builder.calls[-1] == (TAG, True)
    assert again.reason.startswith("--rebuild: built with --no-cache; Total reclaimed space")
    assert docker.calls[-1] == (
        "docker",
        "image",
        "prune",
        "-f",
        "--filter",
        "label=project=cargorewind",
        "--filter",
        "dangling=true",
    )
    assert cache.entries()[RECIPE].image_id == "sha256:img2"


def test_a_second_run_of_the_same_recipe_waits_and_reuses(tmp_path: Path) -> None:
    docker = FakeDocker()
    builder = FakeBuilder(docker)
    builder.gate = threading.Event()
    first_cache, second_cache = _cache(tmp_path, docker), _cache(tmp_path, docker)
    results: dict[str, object] = {}
    log: list[str] = []

    def first() -> None:
        results["first"] = first_cache.build(builder, tmp_path, _request())

    def second() -> None:
        results["second"] = second_cache.build(builder, tmp_path, _request(), log=log.append)

    one = threading.Thread(target=first)
    one.start()
    assert builder.started.wait(5)  # the first run holds the recipe lock and is building
    two = threading.Thread(target=second)
    two.start()
    two.join(0.3)
    assert two.is_alive()  # blocked on the lock, not building a second time
    assert log == [f"cache     waiting for another run that builds recipe {RECIPE[:16]}"]
    builder.gate.set()
    one.join(5)
    two.join(5)
    assert builder.calls == [(TAG, False)]
    assert getattr(results["second"], "hit") is True  # noqa: B009


def test_lock_contention_times_out(tmp_path: Path) -> None:
    docker = FakeDocker()
    cache = _cache(tmp_path, docker, lock_timeout=0.1)
    lock_path = tmp_path / "cache" / "build-locks" / f"{RECIPE}.lock"
    with (
        FileLock(lock_path, 1.0),
        pytest.raises(BuildCacheError, match=r"still locked after 0\.1 s"),
    ):
        cache.build(FakeBuilder(docker), tmp_path, _request())
    # A different recipe has its own lock and is not blocked.
    with FileLock(lock_path, 1.0):
        other = cache.build(FakeBuilder(docker, OTHER), tmp_path, _request("t", OTHER))
    assert not other.hit


def test_an_unusable_cache_directory_builds_without_the_cache(tmp_path: Path) -> None:
    docker = FakeDocker()
    blocker = tmp_path / "cache"
    blocker.write_text("a file where the cache directory should be\n")
    builder = FakeBuilder(docker)
    cache = BuildCache(blocker, docker)
    built = cache.build(builder, tmp_path, _request())
    assert not built.hit and builder.calls == [(TAG, False)]
    assert built.reason.startswith(f"build cache {blocker} unavailable (")
    assert built.reason.endswith("; built without it")
    assert cache.entries() == {}


def test_an_unwritable_index_is_reported_but_the_build_stands(tmp_path: Path) -> None:
    docker = FakeDocker()
    cache = _cache(tmp_path, docker)
    (tmp_path / "cache" / INDEX_FILE).mkdir(parents=True)  # a directory where the file goes
    built = cache.build(FakeBuilder(docker), tmp_path, _request())
    assert not built.hit and "not written (" in built.reason
    # The image carries the label, so the next run still reuses it.
    assert cache.build(FakeBuilder(docker), tmp_path, _request()).hit


@pytest.mark.parametrize(
    ("content", "note"),
    [
        ("{broken", "unreadable build index"),
        ('{"schema_version": 7, "images": {}}', "another schema"),
        ('{"schema_version": 1, "images": []}', ""),
        ('{"schema_version": 1, "images": {"x": {"recipe": "x"}}}', ""),
        (
            '{"schema_version": 1, "images": {"x": {"recipe": "y", "tag": "t", "image_id": "i", '
            '"built_at": "", "build_seconds": 0, "repo": "", "base_commit": "", "toolchain": ""}}}',
            "",
        ),
    ],
)
def test_bad_index_files_count_as_empty(tmp_path: Path, content: str, note: str) -> None:
    (tmp_path / INDEX_FILE).write_text(content)
    cache = BuildCache(tmp_path, FakeDocker())
    assert cache.entries() == {}
    assert note in cache.note


def _index(**entry: object) -> str:
    fields: dict[str, object] = {
        "recipe": RECIPE,
        "tag": TAG,
        "image_id": "sha256:x",
        "built_at": "2026-09-30T12:00:00+00:00",
        "build_seconds": 12.5,
        "repo": "r",
        "base_commit": "c",
        "toolchain": "1.70.0",
        **entry,
    }
    return json.dumps({"schema_version": 1, "images": {RECIPE: fields}})


@pytest.mark.parametrize(
    "entry",
    [
        {"build_seconds": None},
        {"build_seconds": "12.5"},
        {"build_seconds": True},
        {"built_at": None},
        {"tag": ["t"]},
        {"image_id": 7},
    ],
)
def test_mistyped_index_entries_are_ignored(tmp_path: Path, entry: dict[str, object]) -> None:
    # Regression: only missing keys were rejected, so a null build_seconds crashed the
    # cache hit message (and `cache list`) with a TypeError on every run of that recipe.
    (tmp_path / INDEX_FILE).write_text(_index(**entry))
    docker = FakeDocker()
    docker.images[TAG] = ("sha256:x", RECIPE)
    cache = BuildCache(tmp_path, docker, now=lambda: NOW)
    assert cache.entries() == {}
    hit = cache.build(FakeBuilder(docker), tmp_path, _request())
    assert hit.hit and hit.reason == f"image {TAG} found in Docker and indexed again"
    assert cache.entries()[RECIPE].build_seconds == 0.0


def test_integer_build_seconds_are_read_as_floats(tmp_path: Path) -> None:
    (tmp_path / INDEX_FILE).write_text(_index(build_seconds=3))
    entry = BuildCache(tmp_path, FakeDocker()).entries()[RECIPE]
    assert entry.build_seconds == 3.0 and isinstance(entry.build_seconds, float)


class PruneRefusingDocker(FakeDocker):
    """Docker while another prune runs: `docker image prune` exits 1."""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None = None,
        stdin: bytes | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> CommandResult:
        if tuple(argv)[:3] == ("docker", "image", "prune"):
            self.calls.append(tuple(argv))
            error = "Error response from daemon: a prune operation is already running\n"
            return CommandResult(tuple(argv), 1, "", error)
        return super().run(argv, cwd=cwd, stdin=stdin, env=env, timeout=timeout)


def test_a_failed_prune_after_rebuild_does_not_fail_the_build(tmp_path: Path) -> None:
    # Regression: the best-effort prune raised CommandError out of build(), so rewind
    # exited 1 after --rebuild although the image was built and indexed.
    docker = PruneRefusingDocker()
    builder = FakeBuilder(docker)
    cache = _cache(tmp_path, docker)
    built = cache.build(builder, tmp_path, _request(), rebuild=True)
    assert builder.calls == [(TAG, True)]
    assert not built.hit and built.result.image_id == "sha256:img1"
    assert built.reason.startswith(
        "--rebuild: built with --no-cache; dangling images not pruned "
        "(Error response from daemon: a prune operation is already running)"
    )
    assert cache.entries()[RECIPE].image_id == "sha256:img1"
    with pytest.raises(CommandError):  # `cache prune` itself still reports the failure
        cache.prune()


def test_prune_drops_entries_whose_image_is_gone(tmp_path: Path) -> None:
    docker = FakeDocker()
    cache = _cache(tmp_path, docker)
    cache.build(FakeBuilder(docker), tmp_path, _request())
    cache.build(FakeBuilder(docker, OTHER), tmp_path, _request("cargorewind/demo:other", OTHER))
    del docker.images["cargorewind/demo:other"]
    report = cache.prune()
    assert report.stale == [OTHER] and report.kept == 1
    assert report.docker_output.endswith("Total reclaimed space: 1.2GB")
    assert list(cache.entries()) == [RECIPE]
    assert cache.prune().stale == []

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from cargorewind import __version__, cli, registry
from cargorewind.backend import ReplayBackend
from cargorewind.buildcache import BuildCache, BuildRequest
from cargorewind.deps import Pin
from cargorewind.flip import Flaky, compute_flip
from cargorewind.libtest import Status
from cargorewind.probes import ProbeReport
from cargorewind.registry import DigestCache, HttpResponse, ImageResolver, RegistryClient
from cargorewind.runner import CommandResult
from tests.test_buildcache import FakeBuilder, FakeDocker
from tests.test_deps import INDEX, FakeCargo
from tests.test_flip import stage
from tests.test_gitops import linked_repo
from tests.test_registry import FakeHttp, recorded
from tests.test_rewind import FIXED, HOME_MANIFEST, LIB, CargoModelSession, ScriptedBackend

runner = CliRunner()


def test_version_prints_package_version() -> None:
    result = runner.invoke(cli.app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_no_args_shows_help() -> None:
    result = runner.invoke(cli.app, [])
    assert "historical commit" in result.output


def test_check_tools_reports_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda name: None if name == "docker" else "/bin/git")
    assert cli.check_tools() == {"git": "/bin/git", "docker": None}


def test_doctor_succeeds_when_all_tools_present(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")
    result = runner.invoke(cli.app, ["doctor"])
    assert result.exit_code == 0
    assert "/usr/bin/docker" in result.stdout


def test_doctor_fails_when_a_tool_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.shutil, "which", lambda name: None if name == "docker" else "/bin/git")
    result = runner.invoke(cli.app, ["doctor"])
    assert result.exit_code == 1
    assert "docker   MISSING" in result.stdout


DEMO = Path(__file__).resolve().parents[1] / "examples" / "strsim"


def _demo_args(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "rewind",
        str(DEMO / "strsim-rs.bundle"),
        "--fix",
        "605c81c9b9",
        "--out",
        str(tmp_path / "out"),
        "--workdir",
        str(tmp_path / "work"),
        *extra,
    ]


def test_rewind_replay_prints_verified_flip(tmp_path: Path) -> None:
    result = runner.invoke(cli.app, _demo_args(tmp_path, "--replay", str(DEMO / "transcript.json")))
    assert result.exit_code == 0, result.output
    assert "FAIL_TO_PASS  2" in result.stdout
    assert "  tests::jaro_winkler_same_one_character" in result.stdout
    assert "verdict       VERIFIED fail-to-pass flip" in result.stdout
    assert "probes        2 identifier(s) passed" in result.stdout
    assert "probe     in Docker: build passed, before passed, after passed" in result.stdout
    assert "(cache off: no build cache (replay, record or --no-build-cache))" in result.stdout
    assert "rerun     before exit   0  3 x 104 test(s) by exact name, 0 changed" in result.stdout
    assert "reruns        3 x by exact name (104 in before, 104 in after)" in result.stdout
    assert "flaky" not in result.stdout
    assert (tmp_path / "out" / "task.json").is_file()
    assert (tmp_path / "out" / "recipe.json").is_file()
    assert (tmp_path / "out" / "logs" / "rerun-after.log").is_file()


def test_rewind_rerun_options_reach_the_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[Any] = []

    def fake_rewind(options: Any, *args: Any) -> Any:
        captured.append(options)
        raise cli.GitError("stop here")

    monkeypatch.setattr(cli, "rewind", fake_rewind)
    args = _demo_args(tmp_path, "--replay", str(DEMO / "transcript.json"))
    runner.invoke(cli.app, [*args, "--reruns", "5", "--test-timeout", "42"])
    assert (captured[0].reruns, captured[0].test_timeout) == (5, 42)
    runner.invoke(cli.app, args)
    assert (captured[1].reruns, captured[1].test_timeout) == (3, 300)
    refused = runner.invoke(cli.app, [*args, "--reruns", "-1"])
    assert refused.exit_code == 2 and len(captured) == 2


def test_rewind_record_writes_a_transcript(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    replayed = ReplayBackend(DEMO / "transcript.json")
    monkeypatch.setattr(cli, "DockerBackend", lambda runner, timeout: replayed)
    record = tmp_path / "rec.json"
    result = runner.invoke(cli.app, _demo_args(tmp_path, "--record", str(record)))
    assert result.exit_code == 0, result.output
    assert json.loads(record.read_text())["runs"]["after"]["exit_code"] == 0


def test_rewind_uses_the_build_cache_for_live_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docker = FakeDocker()
    made: list[Path] = []

    def cache_factory(directory: Path, _runner: object) -> BuildCache:
        made.append(directory)
        return BuildCache(directory, docker)

    replayed = ReplayBackend(DEMO / "transcript.json")
    monkeypatch.setattr(cli, "DockerBackend", lambda runner, timeout: replayed)
    monkeypatch.setattr(cli, "BuildCache", cache_factory)
    args = _demo_args(tmp_path, "--cache-dir", str(tmp_path / "cache"))
    first = runner.invoke(cli.app, args)
    assert first.exit_code == 0, first.output
    assert "(cache miss: no image for this recipe; built in " in first.stdout
    recipe = json.loads((tmp_path / "out" / "recipe.json").read_text())["hash"]
    tag = f"cargorewind/strsim-rs:{recipe[:16]}"
    built = json.loads((DEMO / "transcript.json").read_text())["build"]["image_id"]
    docker.images[tag] = (built, recipe)  # what a real build leaves behind
    second = runner.invoke(cli.app, args)
    assert second.exit_code == 0, second.output
    assert f"build     {tag} (cache hit: image {tag} built " in second.stdout
    assert made == [tmp_path / "cache", tmp_path / "cache"]

    listed = runner.invoke(cli.app, ["cache", "list", "--cache-dir", str(tmp_path / "cache")])
    assert listed.exit_code == 0, listed.output
    assert f"{recipe[:16]}  {tag}  built " in listed.stdout
    assert "1.39.0  base c4cdd9c35dfa" in listed.stdout

    off = runner.invoke(cli.app, [*args, "--no-build-cache"])
    assert off.exit_code == 0 and len(made) == 3  # two rewinds and the listing, not this run
    assert "(cache off: no build cache (replay, record or --no-build-cache))" in off.stdout


def test_cache_list_and_prune(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    docker = FakeDocker()
    monkeypatch.setattr(cli, "SubprocessRunner", lambda: docker)
    cache_dir = tmp_path / "cache"
    empty = runner.invoke(cli.app, ["cache", "list", "--cache-dir", str(cache_dir)])
    assert empty.exit_code == 0
    assert f"index     {cache_dir / 'build-index.json'} (0 image(s))" in empty.stdout
    cache = BuildCache(cache_dir, docker)
    live, gone = "1" * 64, "2" * 64
    for recipe in (live, gone):
        request = BuildRequest(f"cargorewind/x:{recipe[:16]}", recipe, "x", "c" * 40, "1.70.0")
        cache.build(FakeBuilder(docker, recipe), tmp_path, request)
    del docker.images[f"cargorewind/x:{gone[:16]}"]
    pruned = runner.invoke(cli.app, ["cache", "prune", "--cache-dir", str(cache_dir)])
    assert pruned.exit_code == 0, pruned.output
    assert "docker    Deleted Images:" in pruned.stdout
    assert f"stale     {gone[:16]}: its image is gone; entry removed" in pruned.stdout
    assert "index     1 entry(ies) removed, 1 kept" in pruned.stdout
    (cache_dir / "build-index.json").write_text("{broken")
    noted = runner.invoke(cli.app, ["cache", "list", "--cache-dir", str(cache_dir)])
    assert "note      unreadable build index" in noted.stdout
    # Regression: an entry with null built_at and build_seconds crashed the listing.
    entry = {
        "recipe": live,
        "tag": f"cargorewind/x:{live[:16]}",
        "image_id": "sha256:x",
        "built_at": None,
        "build_seconds": None,
        "repo": "x",
        "base_commit": "c" * 40,
        "toolchain": "1.70.0",
    }
    index = {"schema_version": 1, "images": {live: entry}}
    (cache_dir / "build-index.json").write_text(json.dumps(index))
    mistyped = runner.invoke(cli.app, ["cache", "list", "--cache-dir", str(cache_dir)])
    assert mistyped.exit_code == 0, mistyped.output
    assert "(0 image(s))" in mistyped.stdout

    class DockerDown(FakeDocker):
        def run(self, argv: Any, **kwargs: Any) -> CommandResult:
            return CommandResult(tuple(argv), 1, "", "Cannot connect to the Docker daemon\n")

    monkeypatch.setattr(cli, "SubprocessRunner", DockerDown)
    failed = runner.invoke(cli.app, ["cache", "prune", "--cache-dir", str(cache_dir)])
    assert failed.exit_code == 1
    assert "Cannot connect to the Docker daemon" in failed.output


def test_rewind_rejects_conflicting_or_unpinned_options(tmp_path: Path) -> None:
    both = runner.invoke(cli.app, _demo_args(tmp_path, "--record", "a", "--replay", "b"))
    assert both.exit_code == 2
    unpinned = runner.invoke(cli.app, _demo_args(tmp_path, "--image", "rust:1.39.0-slim"))
    assert unpinned.exit_code == 2


def test_rewind_reports_errors_with_exit_code_1(tmp_path: Path) -> None:
    args = _demo_args(tmp_path, "--replay", str(DEMO / "transcript.json"))
    args[3] = "0000000000"
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 1
    assert "unknown commit" in result.output


def test_rewind_exits_2_when_the_flip_is_not_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    flip = compute_flip(
        stage("base", {"a": Status.PASSED}),
        stage("before", {"a": Status.PASSED}),
        stage("after", {"a": Status.FAILED}),
    )
    report = SimpleNamespace(
        flip=flip,
        lock=SimpleNamespace(bound=object()),
        probes=ProbeReport(),
        verified=flip.verified,
        reruns={},
    )
    monkeypatch.setattr(cli, "rewind", lambda *args: report)
    missing = runner.invoke(cli.app, _demo_args(tmp_path, "--replay", "x.json"))
    assert missing.exit_code == 1
    assert "cannot read transcript" in missing.output
    result = runner.invoke(cli.app, _demo_args(tmp_path, "--replay", str(DEMO / "transcript.json")))
    assert result.exit_code == 2
    assert "regressions   1: a" in result.stdout
    assert "reruns" not in result.stdout
    flip.flaky.append(Flaky("b", "outcome changed"))
    report.reruns = {"after": SimpleNamespace(items=[1, 2])}
    report.rerun_rounds = 2
    again = runner.invoke(cli.app, _demo_args(tmp_path, "--replay", str(DEMO / "transcript.json")))
    assert "reruns        2 x by exact name (2 in after)" in again.stdout
    assert "flaky         1: b" in again.stdout
    assert "NOT VERIFIED" in result.stdout
    assert "lock.json, Cargo.lock, probes.json, recipe.json, Dockerfile" in result.stdout
    assert "probes        none (no new identifier to probe)" in result.stdout


def test_rewind_registry_options_reach_the_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[bool, Path | None]] = []
    real = cli.make_resolver

    def spy(registry: bool, cache_dir: Path | None = None) -> object:
        seen.append((registry, cache_dir))
        return real(False)

    monkeypatch.setattr(cli, "make_resolver", spy)
    args = _demo_args(
        tmp_path,
        "--replay",
        str(DEMO / "transcript.json"),
        "--registry",
        "--cache-dir",
        str(tmp_path / "cache"),
    )
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert seen == [(True, tmp_path / "cache")]


def test_rewind_cache_dir_reaches_the_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression: --cache-dir reached the digest resolver but not the crates.io index.
    captured: list[Any] = []

    def fake_rewind(options: Any, *args: Any) -> Any:
        captured.append(options)
        raise cli.GitError("stop here")

    monkeypatch.setattr(cli, "rewind", fake_rewind)
    args = _demo_args(tmp_path, "--replay", str(DEMO / "transcript.json"))
    result = runner.invoke(cli.app, [*args, "--cache-dir", str(tmp_path / "c")])
    assert result.exit_code == 1 and "stop here" in result.output
    assert captured[0].cache_dir == tmp_path / "c"


def test_toolchain_registry_lookup_with_an_unwritable_cache_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression: the cache write raised an OSError that the command did not catch, so
    # --registry died with a traceback and the registry's answer was lost.
    digest = "sha256:" + "f" * 64
    head = HttpResponse(
        200,
        {
            "docker-content-digest": digest,
            "content-type": "application/vnd.oci.image.index.v1+json",
        },
    )
    manifest = ("HEAD", "v2/library/rust/manifests/1.39.0-slim")
    http = FakeHttp({("GET", "token"): recorded("token.json"), manifest: head})
    monkeypatch.setattr(registry, "UrllibClient", lambda *a, **k: http)
    blocker = tmp_path / "cache-is-a-file"
    blocker.write_text("")
    args = [
        "toolchain",
        str(DEMO / "strsim-rs.bundle"),
        "605c81c9b9",
        "--workdir",
        str(tmp_path / "work"),
        "--registry",
        "--cache-dir",
        str(blocker),
    ]
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert f"image     rust:1.39.0-slim@{digest}" in result.stdout
    assert "not written" in result.stdout


TOOLCHAIN_FIXTURES = Path(__file__).parent / "fixtures" / "toolchain"


def test_toolchain_command_on_the_demo_bundle(tmp_path: Path) -> None:
    report = tmp_path / "reports" / "toolchain.json"
    args = [
        "toolchain",
        str(DEMO / "strsim-rs.bundle"),
        "605c81c9b9",
        "--workdir",
        str(tmp_path / "work"),
        "--json",
        str(report),
    ]
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[2] == "file      none: no rust-toolchain.toml or rust-toolchain at the root"
    assert "date      1.39.0: newest stable before 2019-12-13 (1.39.0 released 2019-11-07)" in lines
    assert "floor     1.0.0: 1.39.0 meets every floor" in lines
    assert "          offline-table: offline digest table" in lines
    document = json.loads(report.read_text())
    assert document["schema_version"] == 1
    assert document["fix_commit"].startswith("605c81c9b9")
    assert document["toolchain"]["version"] == "1.39.0"
    assert document["image"]["source"] == "offline-table"


def test_toolchain_command_reads_a_symlinked_legacy_file(make_repo: Any, tmp_path: Path) -> None:
    # Regression: git show printed the link target, so the channel was "rust-toolchain.toml".
    repo = make_repo("linked")
    _, fix = linked_repo(repo)
    args = ["toolchain", str(repo.path), fix, "--workdir", str(tmp_path / "work")]
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert "toolchain 1.70.0 (rust-toolchain): rust-toolchain pins 1.70.0" in result.stdout
    assert "have the same content (one may link to the other)" in result.stdout


def _msrv_repo(make_repo: Any) -> tuple[Any, str]:
    repo = make_repo("msrv")
    files = {
        p.relative_to(TOOLCHAIN_FIXTURES / "msrv-raise").as_posix(): p.read_text()
        for p in (TOOLCHAIN_FIXTURES / "msrv-raise").rglob("*")
        if p.is_file()
    }
    repo.commit("base", {**files, "src/lib.rs": "pub fn f() {}\n"}, "2024-01-10T12:00:00+00:00")
    fix = repo.commit(
        "fix", {"src/lib.rs": "pub fn f() -> u8 { 1 }\n"}, "2024-01-11T12:00:00+00:00"
    )
    return repo, fix


def test_toolchain_command_raises_to_the_msrv_and_needs_a_digest(
    make_repo: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, fix = _msrv_repo(make_repo)
    args = ["toolchain", str(repo.path), fix, "--workdir", str(tmp_path / "work")]
    offline = runner.invoke(cli.app, args)
    assert offline.exit_code == 1
    assert "raise     1.65.0: raised from 1.60.0 to meet 1.65.0" in offline.stdout
    assert "toolchain 1.65.0 (rust-version)" in offline.stdout
    assert "no pinned digest for rust:1.65.0-slim; pass --registry" in offline.output

    digest = "sha256:" + "f" * 64
    head = HttpResponse(
        200,
        {
            "docker-content-digest": digest,
            "content-type": "application/vnd.oci.image.index.v1+json",
        },
    )
    http = FakeHttp({("HEAD", "v2/library/rust/manifests/1.65.0-slim"): head})
    seen: list[tuple[bool, Path | None]] = []

    def fake_resolver(registry: bool, cache_dir: Path | None = None) -> ImageResolver:
        seen.append((registry, cache_dir))
        return ImageResolver(registry=RegistryClient(http, auth=None), cache=DigestCache(tmp_path))

    monkeypatch.setattr(cli, "make_resolver", fake_resolver)
    online = runner.invoke(cli.app, [*args, "--registry", "--cache-dir", str(tmp_path)])
    assert online.exit_code == 0, online.output
    assert f"image     rust:1.65.0-slim@{digest}" in online.stdout
    assert seen == [(True, tmp_path)]


def test_toolchain_command_reports_unknown_commits(make_repo: Any, tmp_path: Path) -> None:
    repo, _ = _msrv_repo(make_repo)
    args = ["toolchain", str(repo.path), "0" * 40, "--workdir", str(tmp_path / "work")]
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 1
    assert "unknown commit" in result.output


# The lock command


def _home_repo(make_repo: Any, lock: str | None = None) -> tuple[str, str]:
    repo = make_repo("origin")
    files: dict[str, str | None] = {
        "Cargo.toml": HOME_MANIFEST,
        "src/lib.rs": LIB,
        "rust-toolchain": "1.75\n",
    }
    if lock is not None:
        files["Cargo.lock"] = lock
    repo.commit("base", files, "2024-02-20T12:00:00+00:00")
    fix = repo.commit("fix", {"src/lib.rs": FIXED}, "2024-03-01T00:00:00+00:00")
    return str(repo.path), fix


def _lock_args(tmp_path: Path, source: str, fix: str, *extra: str) -> list[str]:
    return [
        "lock",
        source,
        fix,
        "--out",
        str(tmp_path / "lock"),
        "--workdir",
        str(tmp_path / "work"),
        *extra,
    ]


def test_lock_command_checks_a_committed_lockfile_without_docker(
    make_repo: Any, tmp_path: Path
) -> None:
    source, fix = _home_repo(make_repo, lock="version = 4\n")
    result = runner.invoke(cli.app, _lock_args(tmp_path, source, fix))
    assert result.exit_code == 0, result.output
    assert (
        "lockfile  committed Cargo.lock, format v4 (cargo 1.78.0+ reads it); cargo fetch --locked"
    ) in result.stdout
    assert "toolchain 1.78.0 (Cargo.lock): raised from 1.75.0" in result.stdout
    document = json.loads((tmp_path / "lock" / "lock.json").read_text())
    assert (document["strategy"], document["format_version"]) == ("committed", 4)
    assert document["readable_from"] == "1.78.0"
    assert not (tmp_path / "lock" / "Cargo.lock").exists()


def test_lock_command_on_a_crate_without_dependencies(tmp_path: Path) -> None:
    args = _lock_args(tmp_path, str(DEMO / "strsim-rs.bundle"), "605c81c9b9")
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert "no crates.io dependencies: cargo generates it in the image" in result.stdout
    assert "wrote     " in result.stdout and "(lock.json)" in result.stdout


def test_lock_command_records_and_replays_the_pin_loop(
    make_repo: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, fix = _home_repo(make_repo)
    session = CargoModelSession(FakeCargo(INDEX, {"demo": [("home", "0.5.4")]}))
    backend = ScriptedBackend({}, session)
    monkeypatch.setattr(cli, "DockerBackend", lambda runner, timeout: backend)
    transcript = tmp_path / "lock-transcript.json"
    index = ["--index-dir", str(INDEX.root)]
    recorded = runner.invoke(
        cli.app, _lock_args(tmp_path, source, fix, *index, "--record", str(transcript))
    )
    assert recorded.exit_code == 0, recorded.output
    assert "pin       round 1: 1 package(s)" in recorded.stdout
    assert "          home 0.5.12 -> 0.5.9 (ok)" in recorded.stdout
    assert "lock      9 pin(s) in 3 round(s); every crates.io package is bounded" in recorded.stdout
    assert "(lock.json, Cargo.lock)" in recorded.stdout
    first = (tmp_path / "lock" / "Cargo.lock").read_text()

    monkeypatch.setattr(cli, "DockerBackend", lambda runner, timeout: None)
    (tmp_path / "lock" / "Cargo.lock").unlink()
    replayed = runner.invoke(
        cli.app, _lock_args(tmp_path, source, fix, *index, "--replay", str(transcript))
    )
    assert replayed.exit_code == 0, replayed.output
    assert "mode      replay of" in replayed.stdout
    assert (tmp_path / "lock" / "Cargo.lock").read_text() == first


def test_lock_command_exit_codes(
    make_repo: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, fix = _home_repo(make_repo)

    class Refusing(FakeCargo):
        def pin(self, pins: list[Pin]) -> tuple[str, dict[str, str]]:
            return self.render(), {p.spec: "error: refused" for p in pins}

    session = CargoModelSession(Refusing(INDEX, {"demo": [("home", "0.5.4")]}))
    monkeypatch.setattr(cli, "DockerBackend", lambda runner, timeout: ScriptedBackend({}, session))
    stuck = runner.invoke(
        cli.app, _lock_args(tmp_path, source, fix, "--index-dir", str(INDEX.root))
    )
    assert stuck.exit_code == 2, stuck.output
    assert "package(s) could not be bounded" in stuck.stdout
    broken, fix2 = _home_repo(lambda name: make_repo(name + "-broken"), "x = [")
    bad = runner.invoke(cli.app, _lock_args(tmp_path / "b", broken, fix2))
    assert bad.exit_code == 1 and "Cargo.lock" in bad.output


WHICH = Path(__file__).resolve().parents[1] / "examples" / "which-rs"


def test_lock_demo_replays_the_recorded_pin_loop_offline(tmp_path: Path) -> None:
    """The committed which-rs demo: a live pin loop with real cargo, replayed."""
    args = _lock_args(
        tmp_path,
        str(WHICH / "which-rs.bundle"),
        "e776ff0",
        "--index-dir",
        str(WHICH / "index"),
        "--replay",
        str(WHICH / "lock-transcript.json"),
    )
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    assert "lockfile  none: 7 crates.io requirement(s)" in result.stdout
    assert "lock      generated: 41 crates.io package(s), 32 published at or after" in result.stdout
    assert "          home 0.5.12 -> 0.5.5 (ok)" in result.stdout
    assert "lock      16 pin(s) in 4 round(s); every crates.io package is bounded" in result.stdout
    document = json.loads((tmp_path / "lock" / "lock.json").read_text())
    assert document["bounded"] is True and len(document["rounds"]) == 4
    assert document["cutoff"] == "2023-10-17T22:45:33+00:00"
    assert 'name = "home"\nversion = "0.5.5"' in (tmp_path / "lock" / "Cargo.lock").read_text()

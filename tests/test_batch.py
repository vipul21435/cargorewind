from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from cargorewind.backend import Backend, BuildResult, ReplayBackend
from cargorewind.batch import (
    BatchError,
    BatchOptions,
    BatchResult,
    BatchTask,
    counts,
    exit_code,
    load_recipes,
    run_batch,
    summary_document,
    summary_markdown,
    summary_table,
    task_key,
    task_options,
    with_defaults,
)
from cargorewind.gitops import GitError
from cargorewind.runner import CommandError, CommandResult, SubprocessRunner
from tests.conftest import GitRepo
from tests.test_rewind import (
    DEMO,
    FIXED,
    LIB,
    PASSING,
    TRIPLE_RUNS,
    ScriptedBackend,
    _crate,
    _triple_crate,
)

FAILING = {**PASSING, "after": (101, "test tests::zero ... ok\ntest tests::two ... FAILED\n")}


def test_load_recipes_folds_defaults_and_resolves_paths(tmp_path: Path) -> None:
    recipes = tmp_path / "recipes" / "batch.toml"
    recipes.parent.mkdir()
    recipes.write_text(
        """
        [defaults]
        reruns = 2
        vendor = true

        [[task]]
        repo = "crates/a.bundle"
        fix = "abc123"

        [[task]]
        name = "b-fix"
        repo = "https://github.com/o/b"
        fix = "def456"
        base = "0000"
        vendor = false
        reruns = 0
        test_timeout = 9
        image = "rust:1.70.0-slim@sha256:" + "d"
        index_dir = "index"
        replay = "/abs/transcript.json"
        """.replace('"rust:1.70.0-slim@sha256:" + "d"', '"rust:1.70.0-slim@sha256:d"')
    )
    a, b = load_recipes(recipes)
    assert a == BatchTask(
        str(recipes.parent / "crates" / "a.bundle"), "abc123", vendor=True, reruns=2
    )
    assert a.slug == "a"
    assert b == BatchTask(
        "https://github.com/o/b",
        "def456",
        base="0000",
        name="b-fix",
        vendor=False,
        image="rust:1.70.0-slim@sha256:d",
        index_dir=recipes.parent / "index",
        replay=Path("/abs/transcript.json"),
        reruns=0,
        test_timeout=9,
    )
    git_at = tmp_path / "ssh.toml"
    git_at.write_text('[[task]]\nrepo = "git@github.com:o/c.git"\nfix = "1"\n')
    assert load_recipes(git_at)[0].repo == "git@github.com:o/c.git"


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("[[task]\n", "not valid TOML"),
        ("[defaults]\nreruns = 1\n", "no \\[\\[task\\]\\] table"),
        ("task = 3\n", "no \\[\\[task\\]\\] table"),
        ("task = [1]\n", "task 1 is not a table"),
        ("[[task]]\nfix = 'a'\n", "task 1: repo must be a non-empty string"),
        ("[[task]]\nrepo = 'r'\nfix = 3\n", "task 1: fix must be a non-empty string"),
        ("[[task]]\nrepo = 'r'\nfix = 'a'\nname = 'x/y'\n", "plain directory name"),
        ("[[task]]\nrepo = 'r'\nfix = 'a'\nname = '..'\n", "plain directory name"),
        ("[[task]]\nrepo = 'r'\nfix = 'a'\nvendor = 'yes'\n", "vendor must be true or false"),
        ("[[task]]\nrepo = 'r'\nfix = 'a'\nreruns = -1\n", "reruns must be an integer >= 0"),
        ("[[task]]\nrepo = 'r'\nfix = 'a'\nreruns = true\n", "reruns must be an integer >= 0"),
        (
            "[[task]]\nrepo = 'r'\nfix = 'a'\ntest_timeout = 0\n",
            "test_timeout must be an integer >= 1",
        ),
        ("[[task]]\nrepo = 'r'\nfix = 'a'\nbase = ''\n", "task 1: base must be a non-empty string"),
        ("[[task]]\nrepo = 'r'\nfix = 'a'\nreplay = 7\n", "replay must be a non-empty string"),
        ("[[task]]\nrepo = 'r'\nfix = 'a'\nextra = 1\n", "unknown key\\(s\\) extra"),
        (
            "[defaults]\nrepo = 'r'\n[[task]]\nrepo = 'r'\nfix = 'a'\n",
            "unknown default\\(s\\) repo",
        ),
        ("defaults = 1\n[[task]]\nrepo = 'r'\nfix = 'a'\n", "\\[defaults\\] must be a table"),
        (
            "[[task]]\nrepo = 'r'\nfix = 'a'\n[[task]]\nrepo = 'r'\nfix = 'b'\nreruns = 'x'\n",
            "task 2: reruns",
        ),
    ],
)
def test_load_recipes_rejects_bad_files(tmp_path: Path, text: str, match: str) -> None:
    recipes = tmp_path / "batch.toml"
    recipes.write_text(text)
    with pytest.raises(BatchError, match=match):
        load_recipes(recipes)
    with pytest.raises(BatchError, match="cannot read"):
        load_recipes(tmp_path / "missing.toml")


def test_task_options_follow_the_task_then_the_batch(tmp_path: Path) -> None:
    options = BatchOptions(tmp_path / "out", tmp_path / "work", reruns=5, test_timeout=40)
    plain = task_options(BatchTask("r/x.bundle", "abc"), options, tmp_path / "out" / "x")
    checkout = tmp_path / "work" / BatchTask("r/x.bundle", "abc").checkout
    assert (plain.reruns, plain.test_timeout, plain.workdir) == (5, 40, checkout)
    assert checkout.name.startswith("x-") and len(checkout.name) == len("x-") + 8
    assert plain.index is None and plain.cache is None
    task = BatchTask("r/x.bundle", "abc", reruns=1, test_timeout=7, index_dir=tmp_path / "i")
    custom = task_options(task, options, tmp_path / "out" / "x")
    assert (custom.reruns, custom.test_timeout) == (1, 7)
    assert custom.index is not None
    assert with_defaults([task], reruns=0)[0].reruns == 0


def _backends(backends: dict[str, Backend]) -> Callable[[BatchTask], Backend]:
    def factory(task: BatchTask) -> Backend:
        return backends[task.name or task.repo]

    return factory


def test_run_batch_dedupes_records_errors_and_summarizes(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    good = make_repo("good")
    _, fix = _crate(good, lockfile=True)
    bad = make_repo("bad")
    fix_bad = _triple_crate(bad)
    tasks = [
        BatchTask(str(good.path), fix[:8], name="good"),
        BatchTask(str(good.path), fix, name="twin"),  # the same commit, spelled in full
        BatchTask(str(bad.path), fix_bad, name="regressed"),
        BatchTask(str(bad.path), "0" * 40, name="unknown"),
    ]
    backends: dict[str, Backend] = {
        "good": ScriptedBackend(PASSING),
        "twin": ScriptedBackend(PASSING),
        "regressed": ScriptedBackend(
            {
                "base": (0, "test tests::zero ... ok\n"),
                "before": (101, "error[E0425]: cannot find function `triple`\n"),
                "after": (101, "test tests::zero ... FAILED\ntest tests::triple_works ... ok\n"),
            }
        ),
    }
    options = BatchOptions(tmp_path / "out", tmp_path / "work", failures=(GitError,))
    lines: list[str] = []
    results = run_batch(tasks, options, SubprocessRunner(), _backends(backends), lines.append)

    assert [r.status for r in results] == ["verified", "duplicate", "not-verified", "error"]
    good_result, twin, regressed, unknown = results
    assert good_result.out == tmp_path / "out" / "good"
    assert (tmp_path / "out" / "good" / "task.json").exists()
    assert (good_result.fail_to_pass, good_result.pass_to_pass, good_result.flaky) == (1, 1, 0)
    assert good_result.toolchain == "1.70.0" and good_result.fix_commit == fix
    assert twin.detail == "same repository, fix, base and options as good" and twin.out is None
    assert not (tmp_path / "out" / "twin").exists()
    assert regressed.detail == "regressions: tests::zero"
    assert unknown.detail.startswith("unknown commit: 0000") and unknown.fix_commit == ""
    assert "skip      same repository, fix, base and options as good" in lines
    assert any(line.startswith("result    good: verified in ") for line in lines)
    assert any(line.startswith("error     unknown commit") for line in lines)

    table = summary_table(results)
    header, *rows = table.splitlines()
    assert header.split() == [
        "task",
        "fix",
        "toolchain",
        "lockfile",
        "F2P",
        "P2P",
        "flaky",
        "status",
        "seconds",
        "detail",
    ]
    assert rows[0].split()[:8] == [
        "good",
        fix[:12],
        "1.70.0",
        "committed",
        "1",
        "1",
        "0",
        "verified",
    ]
    assert rows[1].split()[:4] == ["twin", fix[:12], "-", "-"]
    assert rows[3].split()[3:8] == ["-", "-", "-", "-", "error"]
    markdown = summary_markdown(results)
    assert markdown.startswith(
        "| task | fix | toolchain | lockfile | F2P | P2P | flaky | status | seconds | detail |\n"
        "| --- |"
    )
    assert markdown.count("\n") == 6
    assert counts(results) == {"verified": 1, "not-verified": 1, "error": 1, "duplicate": 1}
    document = summary_document(Path("batch.toml"), results)
    assert document["tasks"] == 4 and document["counts"] == counts(results)
    assert document["results"][0]["FAIL_TO_PASS"] == 1
    assert document["results"][0]["lockfile"] == "committed"
    assert document["results"][1]["status"] == "duplicate"
    assert document["seconds"] == round(sum(r.seconds for r in results), 1)
    assert exit_code(results) == 1
    assert exit_code(results[:3]) == 2
    assert exit_code(results[:2]) == 0


def test_run_batch_lets_unexpected_exceptions_through(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    repo = make_repo("origin")
    _, fix = _crate(repo, lockfile=True)
    options = BatchOptions(tmp_path / "out", tmp_path / "work", failures=(GitError,))

    def exploding(task: BatchTask) -> Backend:
        raise RuntimeError("no backend")

    with pytest.raises(RuntimeError, match="no backend"):
        run_batch([BatchTask(str(repo.path), fix)], options, SubprocessRunner(), exploding, print)


def test_result_details_name_the_reason(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    repo = make_repo("origin")
    _, fix = _crate(repo, lockfile=True)
    options = BatchOptions(tmp_path / "out", tmp_path / "work")
    still = {**PASSING, "after": (101, "test tests::zero ... ok\ntest tests::two ... FAILED\n")}
    (result,) = run_batch(
        [BatchTask(str(repo.path), fix)],
        options,
        SubprocessRunner(),
        lambda task: ScriptedBackend(still),
        lambda _: None,
    )
    assert result.status == "not-verified" and result.detail == "no FAIL_TO_PASS test"
    assert result.label == f"origin-{fix[:12]}"
    plain = BatchResult(BatchTask("x/y.bundle", "abcdef"), "error")
    assert plain.label == "y-abcdef" and plain.as_dict()["bundle"] is None


def test_batch_replays_the_recorded_strsim_demo(tmp_path: Path) -> None:
    task = BatchTask(str(DEMO / "strsim-rs.bundle"), "605c81c9b9", name="strsim")
    options = BatchOptions(tmp_path / "out", tmp_path / "work")
    (result,) = run_batch(
        [task],
        options,
        SubprocessRunner(),
        lambda t: ReplayBackend(DEMO / "transcript.json"),
        lambda _: None,
    )
    assert result.status == "verified"
    assert (result.fail_to_pass, result.pass_to_pass, result.toolchain) == (2, 102, "1.39.0")
    assert json.loads((tmp_path / "out" / "strsim" / "task.json").read_text())["verified"] is True


def test_a_task_after_an_error_runs_and_names_are_owned_by_verdicts(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    """The first spelling fails (its backend is gone), so the second spelling of the same
    commit runs; a later task that asks for a bundle name a verdict holds is an error."""
    repo = make_repo("origin")
    base, fix = _crate(repo, lockfile=True)

    class Broken(ScriptedBackend):
        def build(
            self, context: Path, tag: str, target: str | None = None, *, no_cache: bool = False
        ) -> BuildResult:
            raise CommandError(CommandResult(("docker", "build"), 1, "", "daemon gone"))

    tasks = [
        BatchTask(str(repo.path), fix[:10], name="first"),
        BatchTask(str(repo.path), fix, name="first"),  # may reuse the released name
        BatchTask(str(repo.path), base, name="first"),  # another commit, the name is held
        BatchTask(str(repo.path), fix[:7], name="third"),
    ]
    backends = iter([Broken(PASSING), ScriptedBackend(PASSING)])
    options = BatchOptions(tmp_path / "out", tmp_path / "work", failures=(CommandError,))
    lines: list[str] = []
    results = run_batch(
        tasks, options, SubprocessRunner(), lambda task: next(backends), lines.append
    )
    assert [r.status for r in results] == ["error", "verified", "error", "duplicate"]
    assert results[0].detail == "command exited 1: docker build"
    assert results[1].out == tmp_path / "out" / "first"
    assert results[2].detail == (
        f"bundle directory first is taken by task first: {repo.path} --fix {fix}; "
        "give this task another name"
    )
    assert results[3].detail == "same repository, fix, base and options as first"


def test_local_paths_below_the_working_directory_are_spelled_relative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "recipes").mkdir()
    recipes = tmp_path / "recipes" / "batch.toml"
    recipes.write_text(
        '[[task]]\nrepo = "../crates/a.bundle"\nfix = "abc"\nreplay = "t.json"\n'
        f'[[task]]\nrepo = "{tmp_path}/b.bundle"\nfix = "abc"\nindex_dir = "/elsewhere"\n'
    )
    monkeypatch.chdir(tmp_path)
    a, b = load_recipes(Path("recipes/batch.toml"))
    assert a.repo == str(Path("recipes/../crates/a.bundle"))
    assert a.replay == Path("recipes/t.json")
    assert b.repo == "b.bundle" and b.index_dir == Path("/elsewhere")


EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def test_the_example_recipes_replay_two_real_flips_offline(tmp_path: Path) -> None:
    """examples/batch.toml: strsim-rs (cfg(test) tests in src/lib.rs, no dependencies) and
    semver (an integration test, a lockfile bounded by the commit date), plus a duplicate."""
    tasks = load_recipes(EXAMPLES / "batch.toml")
    assert [t.name for t in tasks] == [
        "strsim-jaro-length-one",
        "semver-empty-version-error",
        "strsim-again",
    ]
    options = BatchOptions(tmp_path / "out", tmp_path / "work")

    def replay(task: BatchTask) -> Backend:
        assert task.replay is not None
        return ReplayBackend(task.replay)

    strsim, semver, again = run_batch(tasks, options, SubprocessRunner(), replay, lambda _: None)
    assert (strsim.status, semver.status, again.status) == ("verified", "verified", "duplicate")
    assert (semver.toolchain, semver.lockfile) == ("1.68.0", "date-bounded")
    assert (semver.fail_to_pass, semver.pass_to_pass, semver.flaky) == (1, 34, 0)
    assert semver.fix_commit == "d92a4d8ff7d1a90caf9fcac9bf120c360455d8d9"
    task = json.loads((tmp_path / "out" / "semver-empty-version-error" / "task.json").read_text())
    assert task["FAIL_TO_PASS"] == ["test_parse"]
    assert task["tests"]["test_parse"]["target"] == "test test_version"
    assert task["tests"]["test_parse"]["command"] == (
        "cargo test --test test_version -- --exact test_parse"
    )
    assert task["split"]["test_files"] == ["tests/test_version.rs"]
    assert task["split"]["fix_files"] == ["src/error.rs", "src/parse.rs"]
    lock = json.loads((tmp_path / "out" / "semver-empty-version-error" / "lock.json").read_text())
    assert lock["strategy"] == "date-bounded" and lock["bounded"] is True
    assert [(p["name"], p["to"]) for r in lock["rounds"] for p in r["pins"]] == [
        ("serde", "1.0.155")
    ]
    assert exit_code([strsim, semver, again]) == 0


def test_two_repositories_with_one_name_get_their_own_checkouts(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    """Forks share a slug; each needs its own checkout, or the second task would fetch
    the first repository and not find its commit."""
    first = make_repo("one")
    _, fix_one = _crate(first, lockfile=True)
    second = make_repo("two")
    fix_two = _triple_crate(second)  # other content, so another fix commit
    forks = tmp_path / "forks"
    for repo, owner in ((first, "a"), (second, "b")):
        (forks / owner).mkdir(parents=True)
        (forks / owner / "crate").symlink_to(repo.path)
    tasks = [
        BatchTask(str(forks / "a" / "crate"), fix_one, name="a"),
        BatchTask(str(forks / "b" / "crate"), fix_two, name="b"),
    ]
    assert tasks[0].slug == tasks[1].slug and tasks[0].checkout != tasks[1].checkout
    options = BatchOptions(tmp_path / "out", tmp_path / "work", failures=(GitError,))
    runs = {"a": PASSING, "b": TRIPLE_RUNS}
    results = run_batch(
        tasks,
        options,
        SubprocessRunner(),
        lambda task: ScriptedBackend(runs[task.name or ""]),
        lambda _: None,
    )
    assert [r.status for r in results] == ["verified", "verified"]
    assert [r.fix_commit for r in results] == [fix_one, fix_two]


def test_scp_like_urls_stay_urls_and_existing_paths_stay_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """git's rule: a colon before the first slash is an scp-like URL, whatever the user
    or host (an ~/.ssh/config alias); the recipes file sits in a subdirectory."""
    (tmp_path / "rv").mkdir()
    (tmp_path / "rv" / "local:crate").mkdir()
    recipes = tmp_path / "rv" / "scp.toml"
    repos = [
        "gh-work:rapidfuzz/strsim-rs.git",
        "deploy@git.example.org:team/strsim-rs.git",
        "git@github.com:rapidfuzz/strsim-rs.git",
        "ssh://git@example.org/team/strsim-rs.git",
        "local:crate",  # exists next to the recipes file: a path, as git clone sees it
        "../crates/a.bundle",
    ]
    recipes.write_text("".join(f'[[task]]\nrepo = "{r}"\nfix = "605c81c9b9"\n' for r in repos))
    monkeypatch.chdir(tmp_path)
    assert [t.repo for t in load_recipes(Path("rv/scp.toml"))] == [
        *repos[:4],
        str(Path("rv/local:crate")),
        str(Path("rv/../crates/a.bundle")),
    ]


def test_names_that_differ_only_in_case_or_normalization_are_one_directory(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    """On APFS (macOS) Demo and demo are one directory: the later task must be an error,
    not a silent overwrite of the earlier task's verified bundle."""
    first = make_repo("one")
    _, fix_one = _crate(first, lockfile=True)
    second = make_repo("two")
    fix_two = _triple_crate(second)
    third = make_repo("three")
    _, fix_three = _crate(third, lockfile=True)
    tasks = [
        BatchTask(str(first.path), fix_one, name="Demo"),
        BatchTask(str(second.path), fix_two, name="demo"),
        BatchTask(str(second.path), fix_two, name="caf\u00e9"),  # NFC
        BatchTask(str(third.path), fix_three, name="cafe\u0301"),  # NFD, another task
    ]
    runs = {"Demo": PASSING, "demo": TRIPLE_RUNS, "caf\u00e9": TRIPLE_RUNS}
    options = BatchOptions(tmp_path / "out", tmp_path / "work", failures=(GitError,))
    results = run_batch(
        tasks,
        options,
        SubprocessRunner(),
        lambda task: ScriptedBackend(runs.get(task.name or "", PASSING)),
        lambda _: None,
    )
    assert [r.status for r in results] == ["verified", "error", "verified", "error"]
    assert results[1].detail.startswith("bundle directory demo is taken by task Demo: ")
    assert results[3].detail.startswith("bundle directory cafe\u0301 is taken by task caf\u00e9: ")
    task = json.loads((tmp_path / "out" / "Demo" / "task.json").read_text())
    assert task["fix_commit"] == fix_one


def test_an_explicit_base_or_other_options_make_another_task(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    """The base decides the patches and the test lists, vendor and image the environment:
    a task that differs in any of them is not a duplicate."""
    repo = make_repo("origin")
    crate = {
        "Cargo.toml": '[package]\nname = "demo"\nversion = "0.1.0"\n',
        "src/lib.rs": LIB,
        "rust-toolchain": "1.70\n",
        "Cargo.lock": "version = 3\n",
        "README.md": "one\n",
    }
    older = repo.commit("older", crate, "2024-01-09T12:00:00+00:00")
    parent = repo.commit("docs", {"README.md": "two\n"}, "2024-01-10T12:00:00+00:00")
    fix = repo.commit("fix", {"src/lib.rs": FIXED}, "2024-01-11T12:00:00+00:00")
    tasks = [
        BatchTask(str(repo.path), fix, name="vs-parent"),
        BatchTask(str(repo.path), fix, base=older[:8], name="vs-older"),
        BatchTask(str(repo.path), fix, base=parent[:9], name="parent-spelled"),
    ]
    options = BatchOptions(tmp_path / "out", tmp_path / "work", failures=(GitError,))
    results = run_batch(
        tasks, options, SubprocessRunner(), lambda task: ScriptedBackend(PASSING), lambda _: None
    )
    assert [r.status for r in results] == ["verified", "verified", "duplicate"]
    assert results[1].base_commit == older and results[0].base_commit == parent
    assert results[2].detail == "same repository, fix, base and options as vs-parent"
    plain = BatchTask("r/x.bundle", "abc")
    assert task_key(plain, fix, parent) != task_key(replace(plain, vendor=True), fix, parent)
    assert task_key(plain, fix, parent) != task_key(
        replace(plain, image="rust@sha256:1"), fix, parent
    )
    assert task_key(plain, fix, parent) == task_key(replace(plain, name="n"), fix, parent)


def test_a_reused_bundle_directory_keeps_nothing_of_the_last_task(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    """A directory left by another task (an earlier run into the same --out, or an
    attempt that ended with an error) must not end up in the new task.json."""
    repo = make_repo("origin")
    _, fix = _crate(repo, lockfile=True)  # committed lockfile: rewind writes no Cargo.lock
    stale = tmp_path / "out" / "x"
    (stale / "logs").mkdir(parents=True)
    (stale / "verify" / "logs").mkdir(parents=True)
    for name in ("Cargo.lock", "logs/rerun-before.log", "verify/verify.json", "lock.json"):
        (stale / name).write_text("another task\n")
    (stale / "verify" / "logs" / "after.log").write_text("another task\n")
    (stale / "notes.txt").write_text("not a bundle file\n")
    options = BatchOptions(tmp_path / "out", tmp_path / "work", reruns=0)
    (result,) = run_batch(
        [BatchTask(str(repo.path), fix, name="x")],
        options,
        SubprocessRunner(),
        lambda task: ScriptedBackend(PASSING),
        lambda _: None,
    )
    assert result.status == "verified"
    task = json.loads((stale / "task.json").read_text())
    assert "Cargo.lock" not in task["files"] and not (stale / "Cargo.lock").exists()
    assert not any(name.startswith("logs/rerun") for name in task["files"])
    assert not (stale / "verify" / "verify.json").exists()
    assert not (stale / "verify" / "logs" / "after.log").exists()
    assert (stale / "lock.json").read_text() != "another task\n"
    assert (stale / "notes.txt").exists()  # not a bundle file: left alone

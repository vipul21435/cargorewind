from __future__ import annotations

from pathlib import Path

from cargorewind.backend import RunResult
from cargorewind.flip import (
    STAGES,
    Flip,
    Rerun,
    StageState,
    StageTests,
    TestKey,
    apply_reruns,
    assign_ids,
    changed_outcomes,
    compute_flip,
    parse_reruns,
    rerun_plan,
    rerun_script,
    stage_tests,
)
from cargorewind.layout import Target
from cargorewind.libtest import UNKNOWN_SUITE, Status, TestResult
from cargorewind.testtargets import UNKNOWN_TARGET, TargetMap, TestTarget

P, F, IG = Status.PASSED, Status.FAILED, Status.IGNORED
LIB = TestTarget("lib", "demo")
IT = TestTarget("test", "it")


def stage(name: str, statuses: dict[str, Status], state: str = StageState.RAN) -> StageTests:
    """A stage whose tests all come from one unknown binary (names are the ids)."""
    results = {
        (UNKNOWN_TARGET, test): TestResult(UNKNOWN_SUITE, test, test, status)
        for test, status in statuses.items()
    }
    return StageTests(name, state, results)


# The flip rules


def test_flip_classic_fail_to_pass() -> None:
    flip = compute_flip(
        stage("base", {"old": P}),
        stage("before", {"old": P, "new": F}),
        stage("after", {"old": P, "new": P}),
    )
    assert flip.fail_to_pass == ["new"]
    assert flip.pass_to_pass == ["old"]
    assert flip.verified
    assert flip.keys == {"new": (UNKNOWN_TARGET, "new"), "old": (UNKNOWN_TARGET, "old")}


def test_flip_compile_error_before_uses_base_run() -> None:
    flip = compute_flip(
        stage("base", {"old": P, "flaky_env": F}),
        stage("before", {}, StageState.COMPILE_ERROR),
        stage("after", {"old": P, "new": P, "flaky_env": F}),
    )
    assert flip.fail_to_pass == ["new"]
    assert flip.pass_to_pass == ["old"]
    assert flip.still_failing == ["flaky_env"]
    assert flip.regressions == []
    assert flip.verified


def test_flip_regressions_block_verification() -> None:
    flip = compute_flip(
        stage("base", {"a": P, "b": P, "c": P, "removed": P}),
        stage("before", {"a": P, "b": P, "c": F, "n": F}),
        stage("after", {"a": F, "c": F, "n": P}),
    )
    assert flip.fail_to_pass == ["n"]
    assert flip.regressions == ["a", "b", "c"]
    assert not flip.verified


def test_flip_ignored_and_timed_out_tests() -> None:
    flip = compute_flip(
        stage("base", {"i": IG, "t": P}),
        stage("before", {"i": IG}, StageState.TIMEOUT),
        stage("after", {"i": IG, "j": P, "t": P}),
    )
    assert flip.fail_to_pass == ["j"]
    assert flip.pass_to_pass == ["t"]  # not reported before (timeout), passed at base
    assert flip.regressions == flip.still_failing == []


def test_no_flip_is_not_verified() -> None:
    flip = compute_flip(
        stage("base", {"a": P}), stage("before", {"a": P}), stage("after", {"a": P})
    )
    assert flip.fail_to_pass == [] and not flip.verified


def test_assign_ids_qualify_names_that_two_targets_share() -> None:
    keys = [(LIB, "shared_name"), (IT, "shared_name"), (LIB, "tests::only")]
    assert assign_ids(keys) == {
        (LIB, "shared_name"): "shared_name [lib demo]",
        (IT, "shared_name"): "shared_name [test it]",
        (LIB, "tests::only"): "tests::only",
    }
    same = stage_tests("after", RunResult(0, "test shared_name ... ok\n"), TargetMap())
    flip = compute_flip(stage("base", {}), stage("before", {}), same)
    assert flip.fail_to_pass == ["shared_name"]


# Stage states


def test_stage_tests_states_and_statuses() -> None:
    targets = TargetMap([Target("lib", "demo", "src/lib.rs"), Target("test", "it", "tests/it.rs")])
    text = (
        "     Running target/debug/deps/demo-0123456789abcdef\n"
        "running 1 test\ntest tests::a ... ok\n"
        "     Running target/debug/deps/it-0123456789abcdef\n"
        "running 1 test\ntest tests::a ... FAILED\n"
    )
    ran = stage_tests("after", RunResult(101, text), targets)
    assert ran.state == StageState.RAN
    assert {k[0].label: r.status for k, r in ran.results.items()} == {"lib demo": P, "test it": F}
    assert ran.status((LIB, "tests::a")) is P and ran.status((IT, "nope")) is Status.MISSING
    assert ran.counts() == {"passed": 1, "failed": 1, "ignored": 0}

    broken = "error[E0425]: cannot find function `x`\nerror: could not compile `demo`\n"
    compile_error = stage_tests("before", RunResult(101, broken), targets)
    assert compile_error.state == StageState.COMPILE_ERROR
    assert compile_error.status((LIB, "tests::a")) is Status.COMPILE_ERROR

    timed_out = stage_tests("after", RunResult(124, text, timed_out=True), targets)
    assert timed_out.state == StageState.TIMEOUT
    assert timed_out.status((LIB, "later")) is Status.TIMEOUT

    probe = stage_tests(
        "before", RunResult(97, "cargorewind probe failed: x"), targets, probe_failed=True
    )
    assert probe.state == StageState.PROBE_FAILED and probe.status((LIB, "a")) is Status.MISSING

    # Two binaries that resolve to the same target: a failure wins.
    twice = text.replace(
        "     Running target/debug/deps/it-0123456789abcdef",
        "     Running unittests src/lib.rs (target/debug/deps/demo-0123456789abcdef)",
    )
    merged = stage_tests("after", RunResult(101, twice), targets)
    assert [(k[0].label, r.status) for k, r in merged.results.items()] == [("lib demo", F)]


# Reruns


def _stages() -> dict[str, StageTests]:
    return {
        "base": stage("base", {"old": P, "only_base": P}),
        "before": stage("before", {"old": P, "new": F, "doc": P}),
        "after": stage("after", {"old": P, "new": P, "doc": P, "only_base": P}),
    }


def test_rerun_plan_picks_the_stages_that_decided_each_candidate() -> None:
    stages = _stages()
    flip = compute_flip(stages["base"], stages["before"], stages["after"])
    assert flip.fail_to_pass == ["new"] and flip.pass_to_pass == ["doc", "old", "only_base"]
    plan = rerun_plan(flip, stages, ("cargo", "test", "--no-fail-fast", "--offline"))
    assert {stage: [i.id for i in items] for stage, items in plan.items()} == {
        "after": ["new", "doc", "old", "only_base"],
        "before": ["new", "doc", "old"],
        "base": ["only_base"],
    }
    new = plan["after"][0]
    # An unknown target runs every binary, so --no-fail-fast stays.
    assert new.command == ("cargo", "test", "--no-fail-fast", "--offline", "--", "--exact", "new")
    assert new.raw == "new" and new.key == (UNKNOWN_TARGET, "new")


def test_rerun_script_wraps_each_command_in_markers_and_a_timeout() -> None:
    items = [
        Rerun("a", (LIB, "a"), "a", ("cargo", "test", "--lib", "--", "--exact", "a")),
        Rerun(
            "d",
            (TestTarget("doc", "demo"), "d"),
            "src/lib.rs - d (line 3)",
            ("cargo", "test", "--doc", "--", "d"),
        ),
    ]
    script = rerun_script(items, 2, 30)
    assert script.splitlines() == [
        "echo '--- cargorewind: rerun 1 0 ---'",
        'timeout -k 10 30 cargo test --lib -- --exact a 2>&1; echo "--- cargorewind: exit $? ---"',
        "echo '--- cargorewind: rerun 1 1 ---'",
        'timeout -k 10 30 cargo test --doc -- d 2>&1; echo "--- cargorewind: exit $? ---"',
        "echo '--- cargorewind: rerun 2 0 ---'",
        'timeout -k 10 30 cargo test --lib -- --exact a 2>&1; echo "--- cargorewind: exit $? ---"',
        "echo '--- cargorewind: rerun 2 1 ---'",
        'timeout -k 10 30 cargo test --doc -- d 2>&1; echo "--- cargorewind: exit $? ---"',
    ]


def _segment(round_: int, index: int, body: str, code: int | None = 0) -> str:
    text = f"--- cargorewind: rerun {round_} {index} ---\n{body}"
    return text + (f"--- cargorewind: exit {code} ---\n" if code is not None else "")


DOC = TestTarget("doc", "demo")
ITEMS = [
    Rerun("a", (LIB, "a"), "a", ("cargo", "test", "--lib", "--", "--exact", "a")),
    Rerun(
        "d",
        (DOC, "src/lib.rs - d"),
        "src/lib.rs - d (line 3)",
        ("cargo", "test", "--doc", "--", "d"),
    ),
]
TARGETS = TargetMap([Target("lib", "demo", "src/lib.rs")])
LIB_RUN = (
    "     Running unittests src/lib.rs (target/debug/deps/demo-0123456789abcdef)\nrunning 1 test\n"
)
DOC_RUN = "   Doc-tests demo\nrunning 2 tests\n"


def test_parse_reruns_reads_each_segment_with_explicit_statuses() -> None:
    output = (
        _segment(1, 0, LIB_RUN + "test a ... ok\n")
        + _segment(
            1,
            1,
            DOC_RUN
            + "test src/lib.rs - d (line 3) ... ok\ntest src/lib.rs - d (line 9) ... FAILED\n",
            101,
        )
        + _segment(2, 0, LIB_RUN + "test a ... FAILED\n", 101)
        + _segment(
            2, 1, "error[E0425]: cannot find value `x`\nerror: could not compile `demo`\n", 101
        )
        + _segment(3, 0, LIB_RUN + "test a ... ", 124)
        + _segment(3, 1, DOC_RUN + "test src/lib.rs - d (line 9) ... ok\n")
        + _segment(4, 0, LIB_RUN, None)
    )
    statuses = parse_reruns(output, ITEMS, 4, TARGETS, timed_out=True)
    # Round 4 was cut off by the run's own timeout (a) or never started (d): left out.
    assert statuses == {
        (LIB, "a"): [P, F, Status.TIMEOUT],
        (DOC, "src/lib.rs - d"): [P, Status.COMPILE_ERROR, Status.MISSING],
    }
    # A script that ended without being stopped: what it did not reach is missing.
    ended = parse_reruns(output, ITEMS, 4, TARGETS)
    assert ended[(LIB, "a")] == [P, F, Status.TIMEOUT, Status.TIMEOUT]
    assert ended[(DOC, "src/lib.rs - d")][-1] == Status.MISSING
    clean = parse_reruns(_segment(1, 0, LIB_RUN + "running 0 tests\n"), ITEMS[:1], 1, TARGETS)
    assert clean == {(LIB, "a"): [Status.MISSING]}
    assert parse_reruns("", ITEMS[:1], 2, TARGETS) == {(LIB, "a"): [Status.MISSING] * 2}


def test_apply_reruns_takes_unstable_tests_out_of_both_lists() -> None:
    stages = _stages()
    flip = compute_flip(stages["base"], stages["before"], stages["after"])
    key = flip.keys
    reruns = {
        "after": {
            key["new"]: [P, F, P],
            key["doc"]: [P, P, P],
            key["old"]: [P, P, P],
            key["only_base"]: [P, P, P],
        },
        "before": {
            key["new"]: [F, F, F],
            key["doc"]: [P, P, P],
            key["old"]: [P, P, Status.TIMEOUT],
        },
        "base": {key["only_base"]: [P, P, P]},
    }
    final = apply_reruns(flip, stages, reruns)
    assert final.fail_to_pass == [] and final.pass_to_pass == ["doc", "only_base"]
    prefix = "outcome changed between the stage run and its reruns: "
    assert [(f.id, f.reason.removeprefix(prefix)) for f in final.flaky] == [
        ("new", "after passed, passed, failed, passed"),
        ("old", "before passed, passed, passed, timeout"),
    ]
    assert not final.verified
    assert final.reruns["old"] == {"after": [P, P, P], "before": [P, P, Status.TIMEOUT]}
    assert final.regressions == flip.regressions == []


def test_apply_reruns_keeps_a_test_that_fails_before_in_a_different_way() -> None:
    # A FAIL_TO_PASS test that fails before by compile error in one round and by a panic
    # in another still changed outcome: the reason says which.
    stages = _stages()
    flip = compute_flip(stages["base"], stages["before"], stages["after"])
    key = flip.keys["new"]
    stable = apply_reruns(flip, stages, {"after": {key: [P, P]}, "before": {key: [F, F]}})
    assert stable.fail_to_pass == ["new"] and stable.flaky == []
    changed = apply_reruns(flip, stages, {"before": {key: [F, Status.COMPILE_ERROR]}})
    assert changed.fail_to_pass == []
    assert changed.flaky[0].reason.endswith("before failed, failed, compile-error")


def test_no_rerun_data_changes_nothing() -> None:
    flip = Flip(["a"], ["b"], [], [], keys={"a": (LIB, "a"), "b": (LIB, "b")})
    assert apply_reruns(flip, _stages(), {}) == flip


def test_a_broken_crate_doctest_of_old_rustdoc_is_a_regression() -> None:
    # rust 1.39 prints a doctest of the crate's own docs as `src/lib.rs -  (line N)`.
    def run(crate_doc: str) -> RunResult:
        return RunResult(
            0 if crate_doc == "ok" else 101,
            "     Running target/debug/deps/b-0123456789abcdef\n"
            "running 1 test\ntest tests::t ... ok\n"
            "   Doc-tests b\n\nrunning 2 tests\n"
            f"test src/lib.rs -  (line 1) ... {crate_doc}\n"
            "test src/lib.rs - two (line 5) ... ok\n",
        )

    targets = TargetMap([Target("lib", "b", "src/lib.rs")])
    stages = {
        "base": stage_tests("base", run("ok"), targets),
        "before": stage_tests("before", run("ok"), targets),
        "after": stage_tests("after", run("FAILED"), targets),
    }
    flip = compute_flip(stages["base"], stages["before"], stages["after"])
    assert flip.regressions == ["src/lib.rs - (crate)"] and not flip.verified
    assert flip.pass_to_pass == ["src/lib.rs - two", "tests::t"]
    # Kept as PASS_TO_PASS when it passes after, and rerun with its file as the filter.
    stages["after"] = stage_tests("after", run("ok"), targets)
    flip = compute_flip(stages["base"], stages["before"], stages["after"])
    assert "src/lib.rs - (crate)" in flip.pass_to_pass
    plan = rerun_plan(flip, stages, ("cargo", "test", "--no-fail-fast"))
    crate_doc = next(r for r in plan["after"] if r.id == "src/lib.rs - (crate)")
    assert crate_doc.command == ("cargo", "test", "--doc", "--", "src/lib.rs")
    assert crate_doc.raw == "src/lib.rs -  (line 1)"


RECORDINGS = Path(__file__).parent / "fixtures" / "libtest"
SHARED_BINARY = RECORDINGS / "shared-binary-1.39.0.txt"
SHARED_FAIL_FAST = RECORDINGS / "shared-binary-fail-fast-1.39.0.txt"


def _recorded_sections(path: Path = SHARED_BINARY) -> dict[str, str]:
    """The ``@@@ <name>`` sections of a rust 1.39.0 recording (header comments dropped)."""
    sections: dict[str, list[str]] = {}
    current: list[str] = []
    for line in path.read_text().splitlines():
        if line.startswith("@@@ "):
            current = sections.setdefault(line.removeprefix("@@@ "), [])
        elif not line.startswith("# "):
            current.append(line)
    return {name: "\n".join(lines) + "\n" for name, lines in sections.items()}


def test_old_cargo_reruns_a_test_of_tests_named_after_the_crate_in_its_own_binary() -> None:
    # On rust 1.39 the library and tests/tempdemo.rs both run as tempdemo-<hash>.
    sections = _recorded_sections()
    targets = TargetMap(
        [Target("lib", "tempdemo", "src/lib.rs"), Target("test", "tempdemo", "tests/tempdemo.rs")]
    )
    codes = {"base": 0, "before": 101, "after": 0}
    stages = {s: stage_tests(s, RunResult(codes[s], sections[s]), targets) for s in codes}
    flip = compute_flip(stages["base"], stages["before"], stages["after"])
    assert flip.fail_to_pass == ["new_case"]
    assert flip.pass_to_pass == [
        "existing",
        "src/lib.rs - (crate)",
        "src/lib.rs - Wrapper<T>::get",
        "src/lib.rs - one",
        "tests::unit",
    ]
    shared = flip.keys["new_case"][0]
    assert shared.label == "lib tempdemo or test tempdemo"
    plan = rerun_plan(flip, stages, ("cargo", "test", "--no-fail-fast"))
    new = next(item for item in plan["after"] if item.id == "new_case")
    assert new.command == (
        "cargo",
        "test",
        "--no-fail-fast",
        "--tests",
        "--",
        "--exact",
        "new_case",
    )
    # The recording ran it without --no-fail-fast, which prints the same here (no binary
    # before tempdemo fails): new_case fails before and passes after.
    reruns = {}
    for name, code in (("before", 101), ("after", 0)):
        items = [item for item in plan[name] if item.id == "new_case"]
        body = sections[f"rerun-{name}"]
        output = "".join(_segment(round_, 0, body, code) for round_ in (1, 2, 3))
        reruns[name] = parse_reruns(output, items, 3, targets)
    assert reruns["before"] == {new.key: [F, F, F]} and reruns["after"] == {new.key: [P, P, P]}
    final = apply_reruns(flip, stages, reruns)
    assert final.fail_to_pass == ["new_case"] and final.flaky == [] and final.verified
    # `cargo test --lib -- --exact new_case` ran the library binary alone: the test was
    # missing from every round, which made it flaky and emptied FAIL_TO_PASS.
    lib_only = parse_reruns(_segment(1, 0, sections["old-rerun"], 0), [new], 1, targets)
    assert lib_only == {new.key: [Status.MISSING]}


def _six_and_one() -> tuple[dict[str, StageTests], Flip, list[Rerun]]:
    """One FAIL_TO_PASS and six PASS_TO_PASS lib tests, and the after stage's reruns."""
    names = [f"tests::old{i}" for i in range(6)]
    targets = TargetMap([Target("lib", "demo", "src/lib.rs")])

    def run(new: str) -> RunResult:
        lines = [f"test {name} ... ok" for name in names] + [f"test tests::new ... {new}"]
        return RunResult(0, LIB_RUN + "\n".join(lines) + "\n")

    stages = {
        "base": stage_tests("base", run("ok"), targets),
        "before": stage_tests("before", run("FAILED"), targets),
        "after": stage_tests("after", run("ok"), targets),
    }
    stages["base"].results = {k: v for k, v in stages["base"].results.items() if k[1] in names}
    flip = compute_flip(stages["base"], stages["before"], stages["after"])
    assert flip.fail_to_pass == ["tests::new"] and len(flip.pass_to_pass) == 6
    plan = rerun_plan(flip, stages, ("cargo", "test", "--no-fail-fast"))
    return stages, flip, plan["after"]


def test_reruns_that_the_run_timeout_stopped_do_not_make_tests_flaky() -> None:
    # The rerun run hit --timeout after round 1: rounds 2 and 3 never started.
    stages, flip, _ = _six_and_one()
    targets = TargetMap([Target("lib", "demo", "src/lib.rs")])
    plan = rerun_plan(flip, stages, ("cargo", "test", "--no-fail-fast"))
    reruns = {}
    for name, items in plan.items():
        output = ""
        for index, item in enumerate(items):
            word = {P: "ok", F: "FAILED"}[stages[name].status(item.key)]
            output += _segment(1, index, LIB_RUN + f"test {item.raw} ... {word}\n")
        reruns[name] = parse_reruns(output, items, 3, targets, timed_out=True)
    assert all(seen == [stages[n].status(k)] for n in reruns for k, seen in reruns[n].items())
    final = apply_reruns(flip, stages, reruns)
    assert final.fail_to_pass == ["tests::new"] and len(final.pass_to_pass) == 6
    assert final.flaky == [] and final.verified
    assert final.reruns["tests::new"] == {"before": [F], "after": [P]}


def test_a_rerun_whose_build_outlasts_the_test_timeout_is_left_out() -> None:
    # Each rerun container recompiles the patched crate inside the first command's
    # per-test timeout; a command stopped there never reached its test.
    stages, flip, items = _six_and_one()
    targets = TargetMap([Target("lib", "demo", "src/lib.rs")])
    building = "   Compiling demo v0.1.0 (/home/rewind/repo)\n"
    output = _segment(1, 0, building, 124)
    for round_ in (1, 2, 3):
        for index, item in enumerate(items):
            if (round_, index) != (1, 0):
                output += _segment(round_, index, LIB_RUN + f"test {item.raw} ... ok\n")
    seen = parse_reruns(output, items, 3, targets)
    assert seen[items[0].key] == [P, P] and all(len(v) == 3 for v in list(seen.values())[1:])
    assert changed_outcomes(stages["after"], seen) == 0
    final = apply_reruns(flip, stages, {"after": seen})
    assert final.flaky == [] and final.verified
    # A test that hangs once its binary runs is still a timeout, and still flaky.
    hung = _segment(1, 0, LIB_RUN + f"test {items[0].raw} ... ", 124)
    assert parse_reruns(hung, items[:1], 1, targets) == {items[0].key: [Status.TIMEOUT]}


def test_a_shared_rerun_goes_on_past_a_failing_same_named_test_of_an_earlier_binary() -> None:
    # tests/api.rs has a `smoke` that fails in every stage, and api runs before the shared
    # tempdemo binary, whose `smoke` is the FAIL_TO_PASS test (rust 1.39.0 recording).
    sections = _recorded_sections(SHARED_FAIL_FAST)
    targets = TargetMap(
        [
            Target("lib", "tempdemo", "src/lib.rs"),
            Target("test", "api", "tests/api.rs"),
            Target("test", "tempdemo", "tests/tempdemo.rs"),
        ]
    )
    stages = {s: stage_tests(s, RunResult(101, sections[s]), targets) for s in STAGES}
    flip = compute_flip(stages["base"], stages["before"], stages["after"])
    smoke = "smoke [lib tempdemo or test tempdemo]"
    assert flip.fail_to_pass == [smoke]
    assert flip.pass_to_pass == ["counts", "existing", "tests::unit"]
    plan = rerun_plan(flip, stages, ("cargo", "test", "--no-fail-fast"))
    item = next(item for item in plan["after"] if item.id == smoke)
    assert item.command == ("cargo", "test", "--no-fail-fast", "--tests", "--", "--exact", "smoke")

    def rerun(prefix: str) -> dict[str, dict[TestKey, list[Status]]]:
        found = {}
        for name in ("before", "after"):
            body = sections[f"{prefix}rerun-{name}"]
            output = "".join(_segment(round_, 0, body, 101) for round_ in (1, 2, 3))
            found[name] = parse_reruns(output, [item], 3, targets)
        return found

    reruns = rerun("")
    assert reruns == {"before": {item.key: [F, F, F]}, "after": {item.key: [P, P, P]}}
    final = apply_reruns(flip, stages, reruns)
    assert final.fail_to_pass == [smoke] and final.flaky == [] and final.verified
    # Without --no-fail-fast cargo stopped after api's failure and never ran tempdemo:
    # every round was missing, the test was flaky and FAIL_TO_PASS was empty.
    fail_fast = rerun("fail-fast-")
    assert fail_fast == {
        "before": {item.key: [Status.MISSING] * 3},
        "after": {item.key: [Status.MISSING] * 3},
    }
    dropped = apply_reruns(flip, stages, fail_fast)
    assert dropped.fail_to_pass == [] and not dropped.verified

from __future__ import annotations

from cargorewind.backend import RunResult
from cargorewind.flip import (
    Flip,
    Rerun,
    StageState,
    StageTests,
    apply_reruns,
    assign_ids,
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


def assign_ids_qualify_names_that_two_targets_share() -> None:
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
    assert new.command == ("cargo", "test", "--offline", "--", "--exact", "new")
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
    assert statuses == {
        (LIB, "a"): [P, F, Status.TIMEOUT, Status.TIMEOUT],
        (DOC, "src/lib.rs - d"): [P, Status.COMPILE_ERROR, Status.MISSING, Status.TIMEOUT],
    }
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

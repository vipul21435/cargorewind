from __future__ import annotations

import re
from pathlib import Path

import pytest

from cargorewind.libtest import (
    UNKNOWN_SUITE,
    ParsedRun,
    Status,
    Suite,
    compile_failed,
    doctest_item,
    parse_libtest,
    suite_of,
    summarize,
)

P, F, IG = Status.PASSED, Status.FAILED, Status.IGNORED
FIXTURES = Path(__file__).parent / "fixtures" / "libtest"
VERSIONS = ("rust-1.39.0", "rust-1.73.0", "rust-1.98.1")


def recorded(version: str, section: str) -> tuple[str, int]:
    """One section of a recorded run of the zoo crate (see fixtures/libtest/record.sh)."""
    text = (FIXTURES / f"{version}.txt").read_text()
    match = re.search(rf"^=== {section}\n(.*?)^=== exit (\d+)$", text, re.DOTALL | re.MULTILINE)
    assert match is not None, f"{version} has no section {section}"
    return match.group(1), int(match.group(2))


def by_binary(run: ParsedRun) -> dict[tuple[str, bool, str], Status]:
    return {(r.suite.binary, r.suite.doc, r.name): r.status for r in run.results}


# The zoo crate has 10 unit tests, a test in a bin, 3 + 1 integration tests and 6 doctests.
ZOO = {
    ("libtest_zoo", False, "shared_name"): P,
    ("libtest_zoo", False, "tests::fails"): F,
    ("libtest_zoo", False, "tests::ignored_plain"): IG,
    ("libtest_zoo", False, "tests::ignored_with_reason"): IG,
    ("libtest_zoo", False, "tests::noisy"): P,
    ("libtest_zoo", False, "tests::panics_as_expected"): P,
    ("libtest_zoo", False, "tests::passes"): P,
    ("libtest_zoo", False, "tests::result_err"): F,
    ("libtest_zoo", False, "tests::should_panic_but_does_not"): F,
    ("libtest_zoo", False, "tests::thread_panics"): P,
    ("tool", False, "bin_test"): P,
    ("it", False, "it_fails"): F,
    ("it", False, "it_passes"): P,
    ("it", False, "shared_name"): P,
    ("more_checks", False, "slow_but_fine"): P,
    ("libtest_zoo", True, "src/lib.rs - add"): P,
    ("libtest_zoo", True, "src/lib.rs - boom"): P,  # line 16: should_panic
    ("libtest_zoo", True, "src/lib.rs - boom #2"): P,  # line 20: compile_fail
    ("libtest_zoo", True, "src/lib.rs - boom #3"): P,  # line 24: no_run
    ("libtest_zoo", True, "src/lib.rs - boom #4"): IG,  # line 28: ignore
    ("libtest_zoo", True, "src/lib.rs - boom #5"): F,  # line 32: a failing assertion
}


@pytest.mark.parametrize("version", VERSIONS)
@pytest.mark.parametrize("section", ["text", "nocapture", "json"])
def test_recorded_runs_of_three_toolchains_agree(version: str, section: str) -> None:
    """The captured text format, --nocapture --test-threads=1 (test output between a
    test's name and its status, panics in the middle of a line) and the JSON format
    give the same results, on rust 1.39.0, 1.73.0 and 1.98.1."""
    text, exit_code = recorded(version, section)
    run = parse_libtest(text)
    assert by_binary(run) == ZOO
    assert exit_code == 101 and not compile_failed(text, exit_code)
    assert [(s.suite.binary, s.suite.doc, s.planned) for s in run.suites] == [
        ("libtest_zoo", False, 10),
        ("tool", False, 1),
        ("it", False, 3),
        ("more_checks", False, 1),
        ("libtest_zoo", True, 6),
    ]
    summaries = [s.summary for s in run.suites]
    assert [(s["passed"], s["failed"], s["ignored"]) for s in summaries if s] == [
        (5, 3, 2),
        (1, 0, 0),
        (2, 1, 0),
        (1, 0, 0),
        (4, 1, 1),
    ]
    assert (run.json_events > 0) == (section == "json")
    raw = {r.name: r.raw for r in run.results if r.suite.doc}
    assert raw["src/lib.rs - boom #5"] == "src/lib.rs - boom (line 32)"


@pytest.mark.parametrize("version", VERSIONS)
def test_recorded_exact_name_runs(version: str) -> None:
    for section, binary, name in [
        ("exact-lib", "libtest_zoo", "tests::passes"),
        ("exact-test", "more_checks", "slow_but_fine"),
        ("exact-bin", "tool", "bin_test"),
    ]:
        text, exit_code = recorded(version, section)
        run = parse_libtest(text)
        assert by_binary(run) == {(binary, False, name): P} and exit_code == 0
        (suite,) = run.suites
        assert suite.planned == 1 and suite.summary is not None
        assert suite.summary["filtered_out"] == {"libtest_zoo": 9}.get(binary, 0)
    # rustdoc splits test arguments on whitespace, so an exact doctest name matches nothing.
    text, _ = recorded(version, "exact-doc")
    run = parse_libtest(text)
    assert run.results == [] and run.suites[0].suite == Suite("libtest_zoo", doc=True)
    text, _ = recorded(version, "exact-missing")
    assert parse_libtest(text).results == [] and parse_libtest(text).started


@pytest.mark.parametrize("version", VERSIONS)
def test_recorded_compile_error(version: str) -> None:
    text, exit_code = recorded(version, "compile-error")
    run = parse_libtest(text)
    assert run.results == [] and run.suites == [] and not run.started
    assert compile_failed(text, exit_code)
    assert not compile_failed(text, 0)


def test_new_and_old_running_lines() -> None:
    old = suite_of("     Running /home/rewind/target/debug/deps/strsim-a4121e5696016f65")
    assert old == Suite("strsim")
    new = suite_of(
        "     Running unittests src/lib.rs (target/debug/deps/libtest_zoo-f12aa2eeabfcb19e)"
    )
    assert new == Suite("libtest_zoo", path="src/lib.rs", unittests=True)
    assert new is not None and new.label == "src/lib.rs (libtest_zoo)"
    mid = suite_of("     Running unittests (target/debug/deps/demo-0123456789abcdef)")
    assert mid == Suite("demo", unittests=True)
    it = suite_of(
        "     Running tests/more-checks.rs (target/debug/deps/more_checks-765e77c545e30d66)"
    )
    assert it == Suite("more_checks", path="tests/more-checks.rs")
    exe = suite_of("     Running target\\debug\\deps\\demo-0123456789abcdef.exe")
    assert exe is not None and exe.binary.endswith("demo")
    assert suite_of("   Doc-tests libtest-zoo") == Suite("libtest_zoo", doc=True)
    assert suite_of("   Doc-tests libtest_zoo") == Suite("libtest_zoo", doc=True)
    assert suite_of("Running migrations for the test database") is None
    assert suite_of("test result: ok. 1 passed") is None
    assert UNKNOWN_SUITE.label == "unknown binary" and Suite("x", doc=True).label == "doctests of x"


def test_generated_text_output_edge_cases() -> None:
    text = """\
running 8 tests
test tests::adds ... ok
test tests::slow ... ignored
test tests::slow_reason ... ignored, needs network
test tests::panics - should panic ... ok
test tests::timed ... ok <0.123s>
test tests::broken ... FAILED
test bench_sum ... bench:         120 ns/iter (+/- 3)
test tests::noisy ... prints without a newline
and then more
ok
test tests::crashed ...
"""
    run = parse_libtest(text)
    assert {r.name: r.status for r in run.results} == {
        "bench_sum": P,
        "tests::adds": P,
        "tests::broken": F,
        "tests::crashed": F,  # started, never reported, no summary: the binary died
        "tests::noisy": P,
        "tests::panics": P,
        "tests::slow": IG,
        "tests::slow_reason": IG,
        "tests::timed": P,
    }
    stopped = parse_libtest(text, timed_out=True)
    assert {r.name: r.status for r in stopped.results}["tests::crashed"] is Status.TIMEOUT
    assert run.by_name()["tests::crashed"] is F
    assert (run.results[1].suite, run.results[1].name) == (UNKNOWN_SUITE, "tests::adds")


def test_a_pending_test_fails_when_the_next_result_line_arrives() -> None:
    text = "running 2 tests\ntest one ... some noise\ntest two ... ok\n"
    assert parse_libtest(text).by_name() == {"one": F, "two": P}
    odd = '{ "type": "suite", "event": "started", "test_count": "many" }\n'
    assert parse_libtest(odd).suites[0].planned == 0


def test_a_panic_from_another_thread_can_split_a_result_line() -> None:
    # Recorded on rust 1.39.0: stderr of a spawned thread landed inside the line.
    text = (
        "running 2 tests\n"
        "thread '<unnamed>' panicked at 'inner thread panic', src/lib.rs:84:"
        "test tests::should_panic_but_does_not ... 44\n"
        "FAILED\n"
        "test tests::thread_panics ... ok\n"
        "test result: FAILED. 1 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out\n"
    )
    assert parse_libtest(text).by_name() == {
        "tests::should_panic_but_does_not": F,
        "tests::thread_panics": P,
    }


def test_a_test_that_started_before_a_crash_fails_and_the_next_one_is_pending() -> None:
    text = (
        "     Running target/debug/deps/a-0123456789abcdef\n"
        "running 2 tests\n"
        "test one ... \n"
        "test two ... ok\n"
        "error: test failed, to rerun pass `--lib`\n"
        "     Running target/debug/deps/b-0123456789abcdef\n"
        "running 1 test\n"
        "test three ... ok\n"
        "test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out\n"
    )
    run = parse_libtest(text)
    assert by_binary(run) == {
        ("a", False, "one"): F,
        ("a", False, "two"): P,
        ("b", False, "three"): P,
    }


def test_duplicate_names_keep_the_worst_outcome_per_binary() -> None:
    text = "test same ... ok\ntest same ... FAILED\ntest other ... ignored\ntest other ... ok\n"
    assert parse_libtest(text).by_name() == {"other": P, "same": F}
    two = (
        "     Running target/debug/deps/a-0123456789abcdef\ntest same ... ok\n"
        "     Running target/debug/deps/b-0123456789abcdef\ntest same ... FAILED\n"
    )
    run = parse_libtest(two)
    assert by_binary(run) == {("a", False, "same"): P, ("b", False, "same"): F}
    assert run.by_name() == {"same": F}


def test_doctest_names_drop_line_numbers_and_mode_suffixes_and_get_ordinals() -> None:
    before = (
        "test src/lib.rs - jaro (line 150) ... ok\n"
        "test src/lib.rs - hamming (line 30) ... ok\n"
        "test src/lib.rs - hamming (line 12) ... FAILED\n"
        "test src/lib.rs - Foo::bar (line 40) - compile fail ... ok\n"
        "test src/lib.rs - Foo::bar (line 50) - compile ... ok\n"
        "test src/lib.rs - (line 8) - compile ... ok\n"  # the crate's own docs (which-rs)
        "test src/lib.rs - (line 20) ... FAILED\n"
    )
    after = before.replace("line 150", "line 152")
    expected = {
        "src/lib.rs - (crate)": P,
        "src/lib.rs - (crate) #2": F,
        "src/lib.rs - Foo::bar": P,
        "src/lib.rs - Foo::bar #2": P,
        "src/lib.rs - hamming": F,
        "src/lib.rs - hamming #2": P,
        "src/lib.rs - jaro": P,
    }
    assert parse_libtest(before).by_name() == expected
    assert parse_libtest(after).by_name() == expected
    raw = {r.name: r.raw for r in parse_libtest(after).results}
    assert raw["src/lib.rs - jaro"] == "src/lib.rs - jaro (line 152)"
    assert raw["src/lib.rs - Foo::bar"] == "src/lib.rs - Foo::bar (line 40)"
    assert doctest_item("src/lib.rs - Foo::bar (line 40)") == "Foo::bar"
    assert doctest_item("src/lib.rs - (line 8)") == "src/lib.rs"
    assert doctest_item("tests::plain") is None


def test_json_events_with_old_string_counts_and_stray_lines() -> None:
    text = """\
     Running unittests src/lib.rs (target/debug/deps/demo-0123456789abcdef)
{ "type": "suite", "event": "started", "test_count": "3" }
{ "type": "test", "event": "started", "name": "a" }
{ "type": "test", "event": "started", "name": "b" }
{ "type": "test", "event": "timeout", "name": "b" }
{ "type": "test", "name": "a", "event": "ok", "exec_time": 0.001 }
{ "type": "test", "name": "b", "event": "allowed_failure" }
{ "type": "test", "event": "started", "name": "c" }
{ "type": "test", "name": "c", "event": "ignored", "message": "slow" }
{ "type": "bench", "name": "bench_x", "median": 5, "deviation": 1 }
{ not json at all
{ "type": "other", "event": "x" }
{ "type": "suite", "event": "ok", "passed": 1, "failed": 0, "ignored": 1, "measured": 1, \
"filtered_out": 0, "exec_time": 0.01 }
"""
    run = parse_libtest(text)
    assert by_binary(run) == {
        ("demo", False, "a"): P,
        ("demo", False, "b"): F,
        ("demo", False, "bench_x"): P,
        ("demo", False, "c"): IG,
    }
    (suite,) = run.suites
    assert suite.planned == 3 and suite.summary == {
        "passed": 1,
        "failed": 0,
        "ignored": 1,
        "measured": 1,
        "filtered_out": 0,
    }
    assert run.json_events == 10
    crashed = text.split('{ "type": "test", "name": "c"', maxsplit=1)[0]
    statuses = by_binary(parse_libtest(crashed))
    assert statuses[("demo", False, "c")] is F  # started, no result, no suite summary


def test_summarize_counts() -> None:
    assert summarize({"a": P, "b": P, "c": F, "d": IG, "e": Status.MISSING}) == {
        "passed": 2,
        "failed": 1,
        "ignored": 1,
    }
    assert summarize([P, Status.TIMEOUT]) == {"passed": 1, "failed": 0, "ignored": 0}


# Recorded with cargo test on rust 1.39.0 (a crate written for this test): rustdoc of
# that era names a doctest of the crate's own docs with two spaces, since its item is
# empty. 1.73 prints one (``src/lib.rs - (line 3)``).
OLD_RUSTDOC = """\
   Doc-tests tempdemo

running 3 tests
test src/lib.rs -  (line 3) ... ok
test src/lib.rs - one (line 9) ... ok
test src/lib.rs - Wrapper<T>::get (line 20) ... ok

test result: ok. 3 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out
"""


def test_crate_doctests_of_old_rustdoc_keep_their_stable_name() -> None:
    run = parse_libtest(OLD_RUSTDOC)
    assert run.by_name() == {
        "src/lib.rs - (crate)": P,
        "src/lib.rs - Wrapper<T>::get": P,
        "src/lib.rs - one": P,
    }
    raw = {r.name: r.raw for r in run.results}
    assert raw["src/lib.rs - (crate)"] == "src/lib.rs -  (line 3)"
    assert doctest_item("src/lib.rs -  (line 3)") == "src/lib.rs"
    # The same doctest on newer rustdoc has the same stable name.
    newer = parse_libtest(OLD_RUSTDOC.replace(" -  (line", " - (line"))
    assert newer.by_name() == run.by_name()
    failed = parse_libtest(OLD_RUSTDOC.replace("(line 3) ... ok", "(line 3) ... FAILED"))
    assert failed.by_name()["src/lib.rs - (crate)"] == F

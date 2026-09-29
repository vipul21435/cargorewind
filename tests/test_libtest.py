from __future__ import annotations

from cargorewind.libtest import Outcome, compute_flip, parse_libtest, summarize

P, F, IG = Outcome.PASSED, Outcome.FAILED, Outcome.IGNORED


def test_parse_text_output() -> None:
    text = """\
   Compiling demo v0.1.0 (/home/rewind/repo)
     Running target/debug/deps/demo-1234

running 5 tests
test tests::adds ... ok
test tests::slow ... ignored
test tests::slow_reason ... ignored, needs network
test tests::panics - should panic ... ok
test tests::broken ... FAILED
test bench_sum ... bench:         120 ns/iter (+/- 3)

failures:

---- tests::broken stdout ----
thread 'tests::broken' panicked at 'assertion failed', src/lib.rs:10:9
test result: FAILED. 3 passed; 1 failed; 2 ignored; 0 measured; 0 filtered out
"""
    assert parse_libtest(text) == {
        "bench_sum": P,
        "tests::adds": P,
        "tests::broken": F,
        "tests::panics": P,
        "tests::slow": IG,
        "tests::slow_reason": IG,
    }


def test_duplicate_names_keep_the_worst_outcome() -> None:
    text = "test same ... ok\ntest same ... FAILED\ntest other ... ignored\ntest other ... ok\n"
    assert parse_libtest(text) == {"other": P, "same": F}


def test_doctest_names_drop_line_numbers_and_get_ordinals() -> None:
    before = (
        "test src/lib.rs - jaro (line 150) ... ok\n"
        "test src/lib.rs - hamming (line 30) ... ok\n"
        "test src/lib.rs - hamming (line 12) ... FAILED\n"
        "test src/lib.rs - Foo::bar (line 40) - compile fail ... ok\n"
    )
    after = before.replace("line 150", "line 152")
    expected = {
        "src/lib.rs - Foo::bar - compile fail": P,
        "src/lib.rs - hamming": F,
        "src/lib.rs - hamming #2": P,
        "src/lib.rs - jaro": P,
    }
    assert parse_libtest(before) == expected
    assert parse_libtest(after) == expected


def test_summarize_counts() -> None:
    assert summarize({"a": P, "b": P, "c": F, "d": IG}) == {"passed": 2, "failed": 1, "ignored": 1}


def test_flip_classic_fail_to_pass() -> None:
    base = {"old": P}
    before = {"old": P, "new": F}
    after = {"old": P, "new": P}
    flip = compute_flip(base, before, after)
    assert flip.fail_to_pass == ["new"]
    assert flip.pass_to_pass == ["old"]
    assert flip.verified


def test_flip_compile_error_before_uses_base_run() -> None:
    base = {"old": P, "flaky_env": F}
    before: dict[str, Outcome] = {}
    after = {"old": P, "new": P, "flaky_env": F}
    flip = compute_flip(base, before, after)
    assert flip.fail_to_pass == ["new"]
    assert flip.pass_to_pass == ["old"]
    assert flip.still_failing == ["flaky_env"]
    assert flip.regressions == []
    assert flip.verified


def test_flip_regressions_block_verification() -> None:
    base = {"a": P, "b": P, "c": P, "removed": P}
    before = {"a": P, "b": P, "c": F, "n": F}
    after = {"a": F, "c": F, "n": P}
    flip = compute_flip(base, before, after)
    assert flip.fail_to_pass == ["n"]
    assert flip.regressions == ["a", "b", "c"]
    assert not flip.verified


def test_flip_ignored_tests_are_in_no_list() -> None:
    flip = compute_flip({"i": IG}, {"i": IG}, {"i": IG, "j": P})
    assert flip.fail_to_pass == ["j"]
    assert flip.pass_to_pass == flip.regressions == flip.still_failing == []


def test_no_flip_is_not_verified() -> None:
    flip = compute_flip({"a": P}, {"a": P}, {"a": P})
    assert flip.fail_to_pass == []
    assert not flip.verified

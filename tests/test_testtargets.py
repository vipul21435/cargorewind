from __future__ import annotations

import pytest

from cargorewind.layout import Target
from cargorewind.libtest import Suite, suite_of
from cargorewind.testtargets import SHARED, UNKNOWN_TARGET, TargetMap, TestTarget, rerun_command

TARGETS = [
    Target("lib", "my_crate", "src/lib.rs"),
    Target("bin", "my-crate", "src/main.rs"),
    Target("bin", "tool", "src/bin/tool.rs"),
    Target("test", "more-checks", "tests/more-checks.rs"),
    Target("test", "it", "tests/it.rs"),
    Target("test", "my_crate", "tests/my_crate.rs"),  # same binary name as the library
    Target("bench", "speed", "benches/speed.rs"),
    Target("example", "demo", "examples/demo.rs"),
    Target("build", "build-script", "build.rs"),
    Target("test", "checks", "checks/it.rs"),  # [[test]] with a custom path
    Target("lib", "member", "crates/member/src/lib.rs"),
]


@pytest.mark.parametrize(
    ("line", "kind", "name"),
    [
        (
            "     Running unittests src/lib.rs (t/debug/deps/my_crate-0123456789abcdef)",
            "lib",
            "my_crate",
        ),
        (
            "     Running unittests src/main.rs (t/debug/deps/my_crate-0123456789abcdef)",
            "bin",
            "my-crate",
        ),
        (
            "     Running unittests src/bin/tool.rs (t/debug/deps/tool-0123456789abcdef)",
            "bin",
            "tool",
        ),
        (
            "     Running tests/more-checks.rs (t/debug/deps/more_checks-0123456789abcdef)",
            "test",
            "more-checks",
        ),
        (
            "     Running tests/my_crate.rs (t/debug/deps/my_crate-0123456789abcdef)",
            "test",
            "my_crate",
        ),
        ("     Running checks/it.rs (t/debug/deps/checks-0123456789abcdef)", "test", "checks"),
        ("     Running benches/speed.rs (t/debug/deps/speed-0123456789abcdef)", "bench", "speed"),
        (
            "     Running unittests src/lib.rs (t/debug/deps/member-0123456789abcdef)",
            "lib",
            "member",
        ),
        ("   Doc-tests my-crate", "doc", "my_crate"),
        # Old cargo prints only the binary, which the layout maps when one target builds it.
        ("     Running t/debug/deps/it-0123456789abcdef", "test", "it"),
        ("     Running t/debug/deps/demo-0123456789abcdef", "example", "demo"),
        # Targets the layout does not list: the printed path says what they are.
        (
            "     Running tests/new_case.rs (t/debug/deps/new_case-0123456789abcdef)",
            "test",
            "new_case",
        ),
        ("     Running tests/suite/main.rs (t/debug/deps/suite-0123456789abcdef)", "test", "suite"),
        (
            "     Running unittests src/bin/extra/main.rs (t/debug/deps/extra-0123456789abcdef)",
            "bin",
            "extra",
        ),
        ("     Running unittests src/lib.rs (t/debug/deps/other-0123456789abcdef)", "lib", "other"),
        (
            "     Running unittests src/main.rs (t/debug/deps/other-0123456789abcdef)",
            "bin",
            "other",
        ),
        ("     Running examples/x.rs (t/debug/deps/x-0123456789abcdef)", "example", "x"),
        ("     Running odd/path.rs (t/debug/deps/odd-0123456789abcdef)", "unknown", "odd"),
        ("     Running t/debug/deps/stranger-0123456789abcdef", "unknown", "stranger"),
    ],
)
def test_binaries_resolve_to_cargo_targets(line: str, kind: str, name: str) -> None:
    suite = suite_of(line)
    assert suite is not None
    assert TargetMap(TARGETS).resolve(suite) == TestTarget(kind, name)


def test_unknown_suite_and_selectors() -> None:
    assert TargetMap().resolve(Suite("")) == UNKNOWN_TARGET
    assert UNKNOWN_TARGET.label == "unknown" and UNKNOWN_TARGET.selector == ()
    assert TestTarget("lib", "x").selector == ("--lib",)
    assert TestTarget("doc", "x").selector == ("--doc",)
    assert TestTarget("test", "more-checks").selector == ("--test", "more-checks")
    assert TestTarget("bin", "tool").label == "bin tool"
    assert TestTarget("unknown", "stranger").selector == ()


def test_rerun_commands() -> None:
    stage = ("cargo", "test", "--no-fail-fast", "--offline")
    assert rerun_command(TestTarget("lib", "x"), "tests::a", stage) == (
        "cargo",
        "test",
        "--offline",
        "--lib",
        "--",
        "--exact",
        "tests::a",
    )
    assert rerun_command(TestTarget("test", "it"), "b", ("cargo", "test", "--no-fail-fast")) == (
        "cargo",
        "test",
        "--test",
        "it",
        "--",
        "--exact",
        "b",
    )
    nightly = (*stage, "--", "-Z", "unstable-options", "--format", "json")
    assert rerun_command(TestTarget("bin", "tool"), "c", nightly) == (
        "cargo",
        "test",
        "--offline",
        "--bin",
        "tool",
        "--",
        "--exact",
        "c",
        "-Z",
        "unstable-options",
        "--format",
        "json",
    )
    # rustdoc splits its test arguments on whitespace: doctests filter by item path.
    doc = TestTarget("doc", "x")
    assert rerun_command(doc, "src/lib.rs - Foo::bar (line 3)", stage) == (
        "cargo",
        "test",
        "--offline",
        "--doc",
        "--",
        "Foo::bar",
    )
    assert rerun_command(doc, "src/lib.rs - impl Foo<T> for Bar (line 3)", stage)[-1] == "Foo<T>"
    assert rerun_command(doc, "src/lib.rs - (line 8)", stage)[-1] == "src/lib.rs"
    assert rerun_command(doc, "", stage)[-1] == ""
    # An unknown target runs every binary, so a failing namesake in one that runs first
    # must not stop cargo before the right one: --no-fail-fast stays, or is added.
    assert rerun_command(TestTarget("unknown", "s"), "d", stage) == (
        "cargo",
        "test",
        "--no-fail-fast",
        "--offline",
        "--",
        "--exact",
        "d",
    )
    assert rerun_command(UNKNOWN_TARGET, "d", ("cargo", "test", "--", "--nocapture")) == (
        "cargo",
        "test",
        "--no-fail-fast",
        "--",
        "--exact",
        "d",
        "--nocapture",
    )
    assert [TestTarget(k, "x").one_binary for k in ("lib", "bin", "test", "doc")] == [True] * 4
    assert not TestTarget(SHARED, "x").one_binary and not UNKNOWN_TARGET.one_binary


def test_old_cargo_binaries_that_several_targets_build_resolve_to_all_of_them() -> None:
    # Before cargo printed source paths, the library, src/main.rs and tests/my_crate.rs
    # all ran as `my_crate-<hash>`: which one reported a test is unknown.
    suite = suite_of("     Running t/debug/deps/my_crate-0123456789abcdef")
    assert suite is not None
    shared = TargetMap(TARGETS).resolve(suite)
    assert shared == TestTarget(
        SHARED,
        "my_crate",
        (
            TestTarget("lib", "my_crate"),
            TestTarget("bin", "my-crate"),
            TestTarget("test", "my_crate"),
        ),
    )
    assert shared.label == "lib my_crate or bin my-crate or test my_crate"
    # --tests runs every binary a stage runs (not the doctests), so the rerun reaches the
    # test wherever it is, and is valid in a stage where one of the targets is missing.
    assert shared.selector == ("--tests",)
    # --no-fail-fast stays: cargo would otherwise stop at the first binary that fails.
    stage = ("cargo", "test", "--no-fail-fast", "--offline")
    assert rerun_command(shared, "new_case", stage) == (
        "cargo",
        "test",
        "--no-fail-fast",
        "--offline",
        "--tests",
        "--",
        "--exact",
        "new_case",
    )
    assert rerun_command(shared, "new_case", ("cargo", "test"))[:4] == (
        "cargo",
        "test",
        "--no-fail-fast",
        "--tests",
    )
    # The same target listed twice (two packages of a workspace) is still one target.
    twice = TargetMap(
        [Target("lib", "member", "a/src/lib.rs"), Target("lib", "member", "b/src/lib.rs")]
    )
    plain = suite_of("     Running t/debug/deps/member-0123456789abcdef")
    assert plain is not None and twice.resolve(plain) == TestTarget("lib", "member")

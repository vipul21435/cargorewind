from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from cargorewind.backend import Overlay
from cargorewind.dockerfile import PROBE_MARKER
from cargorewind.patchsplit import FileDiff, SplitResult, parse_diff
from cargorewind.probes import (
    MAX_PROBES,
    Definition,
    ProbeReport,
    added_lines,
    candidates,
    definitions,
    host_checks,
    plan_probes,
)

FIXTURES = Path(__file__).parent / "fixtures" / "probes"
BASE_LIB = "pub fn double(x: i32) -> i32 {\n    x * 3\n}\n"


def _patch(name: str) -> list[FileDiff]:
    return parse_diff((FIXTURES / name).read_text())


def _new_file(diff: FileDiff) -> bytes:
    """The content of a file the patch creates: every added line."""
    lines = [line[1:] for hunk in diff.hunks for line in hunk.lines if line.startswith("+")]
    return ("\n".join(lines) + "\n").encode()


def _lib_after() -> bytes:
    return (
        b"pub mod window;\n\npub fn double(x: i32) -> i32 {\n    x * 2\n}\n\n"
        b"fn helper_unused() {}\n"
    )


def _window_split() -> tuple[SplitResult, dict[str, Overlay]]:
    test = _patch("window.test.patch")
    fix = _patch("window.fix.patch") + _patch("lib.fix.patch")
    before = Overlay({"tests/window.rs": _new_file(test[0])})
    after = Overlay(
        {
            "tests/window.rs": _new_file(test[0]),
            "src/window.rs": _new_file(fix[0]),
            "src/lib.rs": _lib_after(),
        }
    )
    return SplitResult(test_files=test, fix_files=fix), {
        "base": Overlay(),
        "before": before,
        "after": after,
    }


def _no_base_words(words: list[str]) -> Mapping[str, list[str]]:
    return {}


def test_definitions_find_items_and_skip_comments_strings_and_generics() -> None:
    source = _new_file(_patch("window.fix.patch")[0]).decode()
    found = [(d.kind, d.name) for d in definitions(source)]
    assert found == [
        ("const", "MAX_WINDOW"),
        ("struct", "Window"),
        ("enum", "Mode"),
        ("trait", "Scorer"),
        ("fn", "score"),
        ("macro_rules", "ensure_width"),
        ("fn", "clamp_width"),
        ("fn", "slide"),
    ]
    assert Definition("struct", "Window", 7) in definitions(source)
    tricky = (
        "struct r#type;\nconst _: () = ();\nlet p: *const u8;\n"
        "fn g<const N: usize, const M: u8>() {}\n"
        "const unsafe fn h() {}\nfn été() {}\nmacro_rules! {}\n"
    )
    assert [(d.kind, d.name) for d in definitions(tricky)] == [("fn", "g"), ("fn", "h")]


def test_added_lines_follow_the_new_side_numbering() -> None:
    (lib,) = _patch("lib.fix.patch")
    assert added_lines(lib) == {1, 2, 3, 4, 6, 7}
    (window,) = _patch("window.fix.patch")
    assert added_lines(window) == set(range(1, 34))


def test_candidates_come_from_added_lines_only() -> None:
    (lib,) = _patch("lib.fix.patch")
    # "double" is on an added line too: its signature line was rewritten.
    found = candidates([lib], "fix", {"src/lib.rs": _lib_after()})
    assert [(p.kind, p.name, p.line) for p in found] == [
        ("fn", "double", 3),
        ("fn", "helper_unused", 7),
    ]
    assert candidates([lib], "fix", {}) == []  # no content for the file
    deleted = parse_diff(
        "diff --git a/src/old.rs b/src/old.rs\ndeleted file mode 100644\n"
        "--- a/src/old.rs\n+++ /dev/null\n@@ -1 +0,0 @@\n-fn gone() {}\n"
    )
    assert candidates(deleted, "fix", {"src/old.rs": b""}) == []


def test_plan_probes_on_fixture_patches() -> None:
    split, overlays = _window_split()
    asked: list[list[str]] = []

    def base_words(words: list[str]) -> Mapping[str, list[str]]:
        asked.append(words)
        # "double" already occurs in the base commit (its signature line was rewritten).
        return {"double": ["benches/a.rs", "src/lib.rs", "src/x.rs", "tests/y.rs"]}

    report = plan_probes(split, overlays, base_words, {"test_patch_applies_at_base": True})
    assert asked == [sorted(set(asked[0]))] and "double" in asked[0]
    assert [(p.patch, p.label) for p in report.probes] == [
        ("fix", "fn helper_unused"),
        ("fix", "const MAX_WINDOW"),
        ("fix", "struct Window"),
        ("fix", "enum Mode"),
        ("fix", "trait Scorer"),
        ("fix", "macro_rules! ensure_width"),
        ("fix", "fn clamp_width"),
        ("fix", "fn slide"),
        ("test", "fn score"),
        ("test", "fn slide_counts_every_window"),
    ]
    reasons = {(s.probe.patch, s.probe.name): s.reason for s in report.skipped}
    assert reasons == {
        ("fix", "double"): "occurs at base in benches/a.rs, src/lib.rs, src/x.rs and 1 more; "
        "a word search cannot prove it absent",
        # The trait method shares its name with the test file's helper, which wins.
        ("fix", "score"): "also defined by test.patch (tests/window.rs:3)",
    }
    assert report.required("base") == ()
    assert report.required("before") == ("score", "slide_counts_every_window")
    assert report.required("after") == report.words and len(report.words) == 10
    assert [c.ok for c in report.checks] == [True, True, True, True]
    assert report.ok
    document = report.document("repo", "b" * 40, "f" * 40)
    assert document["patch_checks"] == {"test_patch_applies_at_base": True}
    assert document["identifiers"][0] == {
        "kind": "fn",
        "name": "helper_unused",
        "patch": "fix",
        "path": "src/lib.rs",
        "line": 7,
    }
    assert {s["name"] for s in document["skipped"]} == {"double", "score"}
    lines = report.lines()
    assert lines[0] == (
        "probe     fix fn helper_unused (src/lib.rs:7): absent at base, defined after only"
    )
    assert lines[-1].startswith("probe     10 identifier(s), every host check passed;")


def test_probe_limit_keeps_the_fix_names_first() -> None:
    split, overlays = _window_split()
    report = plan_probes(split, overlays, _no_base_words, limit=3)
    assert [p.patch for p in report.probes] == ["fix", "fix", "fix"]
    limited = [s for s in report.skipped if s.reason == "over the limit of 3 probes"]
    # 11 names: without base occurrences, "double" is a probe too.
    assert len(limited) == 8 and limited[-1].probe.patch == "test"
    assert MAX_PROBES == 16


def test_host_checks_catch_overlays_that_lack_the_change() -> None:
    split, overlays = _window_split()
    probes = plan_probes(split, overlays, _no_base_words).probes
    # The fix leaked into the before tree, and the after tree lost the new module.
    before = Overlay({**overlays["before"].files, "src/window.rs": b"pub fn slide() {}\n"})
    after = Overlay({"tests/window.rs": overlays["after"].files["tests/window.rs"]})
    checks = host_checks(probes, before, after, {"Mode": ["src/lib.rs"]})
    failed = {(c.stage, c.expectation): c.detail for c in checks if not c.ok}
    assert failed[("base", "no probe identifier occurs at base")].startswith("git grep -w")
    assert failed[("before", "fix.patch identifiers not yet defined")] == "slide"
    assert "clamp_width" in failed[("after", "every probe identifier defined")]
    missing_test = host_checks(probes, Overlay(), overlays["after"], {})
    assert [c.ok for c in missing_test] == [True, False, True, True]
    report = ProbeReport(probes, checks=checks)
    assert not report.ok
    assert any(line.startswith("probe     FAILED after:") for line in report.lines())


def test_patch_checks_and_container_results_decide_ok() -> None:
    split, overlays = _window_split()
    report = plan_probes(split, overlays, _no_base_words, {"fix_patch_applies": False})
    assert not report.ok
    report = plan_probes(split, overlays, _no_base_words)
    report.record_stage("base", f"{PROBE_MARKER} x is missing")  # base requires nothing
    report.record_stage("before", "test a ... ok\n")
    assert report.container == {"before": "passed"} and report.ok
    report.record_stage("after", f"{PROBE_MARKER} slide is missing\n")
    assert report.container["after"] == "failed" and not report.ok


def test_no_probes_when_nothing_new_is_defined() -> None:
    (lib,) = _patch("lib.fix.patch")
    split = SplitResult(fix_files=[lib])
    overlays = {"before": Overlay(), "after": Overlay({"src/lib.rs": b"pub mod window;\n"})}
    report = plan_probes(split, overlays, _no_base_words)
    assert report.probes == [] and report.checks == [] and report.ok
    assert report.lines() == ["probe     no new identifier to probe; the flip is the only evidence"]
    assert ProbeReport().words == ()

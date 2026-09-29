from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from cargorewind.gitops import Git, GitTree
from cargorewind.layout import MemoryTree, Role
from cargorewind.patchsplit import (
    PatchError,
    Side,
    parse_diff,
    render_patch,
    split_diff,
)
from cargorewind.runner import SubprocessRunner
from tests.conftest import GitRepo

BASE_LIB = """\
pub fn clamp(x: i32) -> i32 {
    if x > 10 {
        10
    } else {
        x
    }
}

pub fn double(x: i32) -> i32 {
    x * 3
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clamps() {
        assert_eq!(clamp(11), 10);
    }

    // padding line 1
    // padding line 2
    // padding line 3
    // padding line 4
    // padding line 5
    // padding line 6
    // padding line 7
}

// trailing line 1
// trailing line 2
// trailing line 3
// trailing line 4

pub fn tail() -> u8 {
    1
}
"""

FIX_LIB = (
    BASE_LIB.replace("if x > 10", "if x >= 10")
    .replace("x * 3", "x * 2")
    .replace(
        "    use super::*;\n",
        "    use super::*;\n\n"
        "    #[test]\n    fn doubles() {\n        assert_eq!(double(2), 4);\n    }\n",
    )
    .replace("    1\n}", "    2\n}")
)

INTEGRATION = "#[test]\nfn it_works() {\n    assert_eq!(demo::double(3), 6);\n}\n"
NEW_SRC = "pub fn helper() {}\n\n#[cfg(test)]\nmod tests {\n    #[test]\n    fn t() {}\n}\n"


def _make_fix_commit(repo: GitRepo) -> tuple[str, str]:
    base = repo.commit(
        "base",
        {
            "src/lib.rs": BASE_LIB,
            "README.md": "demo\n",
            "examples/old.rs": "fn main() {}\n",
            "assets/blob.bin": "\x00\x01\x02",
        },
        "2024-01-10T12:00:00+00:00",
    )
    fix = repo.commit(
        "fix doubling",
        {
            "src/lib.rs": FIX_LIB,
            "src/new.rs": NEW_SRC,
            "tests/integration.rs": INTEGRATION,
            "README.md": "demo, fixed\n",
            "examples/old.rs": None,
            "assets/blob.bin": "\x00\x01\x03\x04",
        },
        "2024-01-11T12:00:00+00:00",
    )
    return base, fix


def test_split_round_trip_reproduces_the_fix(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    repo = make_repo("origin")
    base, fix = _make_fix_commit(repo)
    git = Git(SubprocessRunner(), repo.path)
    files = parse_diff(git.diff(base, fix))
    split = split_diff(files, GitTree(git, base), GitTree(git, fix))

    assert sorted(d.path for d in split.test_files) == ["src/lib.rs", "tests/integration.rs"]
    assert sorted(d.path for d in split.fix_files) == [
        "README.md",
        "assets/blob.bin",
        "examples/old.rs",
        "src/lib.rs",
        "src/new.rs",
    ]
    assert split.shared_files == ["src/lib.rs"]
    assert [(h.test_lines, h.fix_lines) for h in split.shared_hunks] == [(0, 2), (5, 2), (0, 2)]
    mixed = split.shared_hunks[1]
    assert mixed.test_header is not None and mixed.fix_header is not None
    assert split.shared_hunks[0].test_header is None
    assert split.notes == ["src/new.rs: new file with test-only code kept whole in fix.patch"]
    by_path = {f.path: f for f in split.files}
    assert (by_path["src/lib.rs"].role, by_path["src/lib.rs"].patch) == (Role.SOURCE, "both")
    assert (by_path["examples/old.rs"].status, by_path["examples/old.rs"].role) == (
        "deleted",
        Role.EXAMPLE,
    )
    assert by_path["tests/integration.rs"].patch == "test"
    assert [(r.path, r.revision, r.region.name) for r in split.regions] == [
        ("src/lib.rs", "fix", "tests"),
        ("src/new.rs", "fix", "tests"),
    ]

    test_patch, fix_patch = tmp_path / "test.patch", tmp_path / "fix.patch"
    test_patch.write_text(split.test_patch)
    fix_patch.write_text(split.fix_patch)

    git.checkout_clean(base)
    git.apply(test_patch)
    mid = (repo.path / "src/lib.rs").read_text()
    assert "fn doubles()" in mid
    assert "x * 3" in mid
    assert "if x > 10" in mid
    assert "    1\n}" in mid
    assert (repo.path / "tests/integration.rs").exists()
    assert not (repo.path / "src/new.rs").exists()

    git.apply(fix_patch)
    touched = sorted({p for d in files for p in d.paths})
    assert git.matches(fix, touched)
    assert not (repo.path / "examples/old.rs").exists()


def test_file_level_routing_without_cfg_test_changes() -> None:
    diff = (
        "diff --git a/src/lib.rs b/src/lib.rs\n"
        "--- a/src/lib.rs\n"
        "+++ b/src/lib.rs\n"
        "@@ -1 +1 @@\n"
        "-a\n"
        "+b\n"
        "diff --git a/crates/x/tests/t.rs b/crates/x/tests/t.rs\n"
        "--- a/crates/x/tests/t.rs\n"
        "+++ b/crates/x/tests/t.rs\n"
        "@@ -1 +1 @@\n"
        "-a\n"
        "+b\n"
    )
    files = parse_diff(diff)
    split = split_diff(
        files,
        MemoryTree({"src/lib.rs": "a\n", "crates/x/tests/t.rs": "a\n"}),
        MemoryTree({"src/lib.rs": "b\n", "crates/x/tests/t.rs": "b\n"}),
    )
    assert [d.path for d in split.fix_files] == ["src/lib.rs"]
    assert [d.path for d in split.test_files] == ["crates/x/tests/t.rs"]
    assert split.shared_hunks == []
    assert split.fix_patch == render_patch(files[:1])


def test_rust_file_that_cannot_be_read_goes_to_fix() -> None:
    diff = "diff --git a/src/a.rs b/src/a.rs\n--- a/src/a.rs\n+++ b/src/a.rs\n@@ -1 +1 @@\n-a\n+b\n"
    split = split_diff(parse_diff(diff), MemoryTree({}), MemoryTree({}))
    assert [d.path for d in split.fix_files] == ["src/a.rs"]


def test_only_test_changes_move_the_whole_file_without_index_line() -> None:
    base = "fn f() {}\n#[cfg(test)]\nmod tests {\n    fn old() {}\n}\n"
    fix = base.replace("fn old()", "fn new()")
    diff = (
        "diff --git a/src/lib.rs b/src/lib.rs\n"
        "index 1111111..2222222 100644\n"
        "--- a/src/lib.rs\n"
        "+++ b/src/lib.rs\n"
        "@@ -3,3 +3,3 @@ fn f() {}\n"
        " mod tests {\n"
        "-    fn old() {}\n"
        "+    fn new() {}\n"
        " }\n"
    )
    split = split_diff(
        parse_diff(diff), MemoryTree({"src/lib.rs": base}), MemoryTree({"src/lib.rs": fix})
    )
    assert split.fix_files == []
    assert [d.path for d in split.test_files] == ["src/lib.rs"]
    assert "index " not in split.test_patch
    assert "@@ -3,3 +3,3 @@ fn f() {}" in split.test_patch


def test_parse_diff_handles_markers_modes_and_defaults() -> None:
    diff = (
        "diff --git a/a.txt b/a.txt\n"
        "--- a/a.txt\n"
        "+++ b/a.txt\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "\\ No newline at end of file\n"
        "+new\n"
        "\\ No newline at end of file\n"
        "diff --git a/run.sh b/run.sh\n"
        "old mode 100644\n"
        "new mode 100755\n"
    )
    files = parse_diff(diff)
    assert [f.path for f in files] == ["a.txt", "run.sh"]
    hunk = files[0].hunks[0]
    assert (hunk.old_start, hunk.old_len, hunk.new_start, hunk.new_len) == (1, 1, 1, 1)
    assert len(hunk.lines) == 4
    assert files[1].hunks == [] and files[1].body == []
    assert render_patch(files) == diff.replace("@@ -1 +1 @@", "@@ -1,1 +1,1 @@")


def test_parse_diff_new_and_deleted_files() -> None:
    diff = (
        "diff --git a/n.rs b/n.rs\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        "+++ b/n.rs\n"
        "@@ -0,0 +1 @@\n"
        "+x\n"
        "diff --git a/d.rs b/d.rs\n"
        "deleted file mode 100644\n"
        "--- a/d.rs\n"
        "+++ /dev/null\n"
        "@@ -1 +0,0 @@\n"
        "-x\n"
    )
    new, deleted = parse_diff(diff)
    assert new.is_new and not new.is_deleted and new.path == "n.rs"
    assert deleted.is_deleted and deleted.path == "d.rs"
    assert parse_diff("") == []


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("junk\n", "must start with"),
        ("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ nonsense @@\n", "bad hunk header"),
        ("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n x\n", "truncated hunk"),
        ("diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n?x\n", "unexpected line"),
    ],
)
def test_parse_diff_errors(text: str, message: str) -> None:
    with pytest.raises(PatchError, match=message):
        parse_diff(text)


def test_side_values() -> None:
    assert [s.value for s in Side] == ["test", "fix"]


ROLE_MANIFEST = """\
[package]
name = "demo"
version = "0.1.0"

[[test]]
name = "it"
path = "checks/it.rs"
"""
ROLE_LIB = """\
mod util;

pub fn one() -> u8 {
    1
}

#[cfg(test)]
mod tests {
    #[test]
    fn one_is_one() {
        assert_eq!(super::one(), 1);
    }
}
"""
ROLE_UTIL = "pub fn helper() -> u8 {\n    2\n}\n"
ROLE_BASE: dict[str, str | None] = {
    "Cargo.toml": ROLE_MANIFEST,
    "Cargo.lock": "version = 3\n",
    "README.md": "demo\n",
    "build.rs": "fn main() {}\n",
    "src/lib.rs": ROLE_LIB,
    "src/util.rs": ROLE_UTIL,
    "tests/api.rs": "#[test]\nfn api() {}\n",
    "tests/data/input.txt": "a\n",
    "checks/it.rs": "#[test]\nfn it() {}\n",
    "benches/speed.rs": "fn main() {}\n",
    "examples/demo.rs": "fn main() {}\n",
}
NEW_TEST = "\n    #[test]\n    fn two() {\n        assert_eq!(1 + 1, 2);\n    }\n}\n"

ROLE_CASES: list[tuple[str, dict[str, str | None], dict[str, tuple[Role, str]]]] = [
    (
        "integration-test",
        {"tests/api.rs": "#[test]\nfn api() {}\n#[test]\nfn api2() {}\n"},
        {"tests/api.rs": (Role.TEST, "test")},
    ),
    ("deleted-test", {"tests/api.rs": None}, {"tests/api.rs": (Role.TEST, "test")}),
    ("test-data", {"tests/data/input.txt": "b\n"}, {"tests/data/input.txt": (Role.TEST, "test")}),
    (
        "custom-test-target",
        {"checks/it.rs": "#[test]\nfn it() {\n    assert!(true);\n}\n"},
        {"checks/it.rs": (Role.TEST, "test")},
    ),
    (
        "source",
        {"src/util.rs": ROLE_UTIL.replace("2", "3")},
        {"src/util.rs": (Role.SOURCE, "fix")},
    ),
    (
        "inline-cfg-test-shared",
        {"src/lib.rs": ROLE_LIB.replace("    1\n", "    2 - 1\n").replace("    }\n}\n", NEW_TEST)},
        {"src/lib.rs": (Role.SOURCE, "both")},
    ),
    (
        "inline-cfg-test-only",
        {"src/lib.rs": ROLE_LIB.replace("    }\n}\n", NEW_TEST)},
        {"src/lib.rs": (Role.SOURCE, "test")},
    ),
    (
        "new-out-of-line-test-module",
        {
            "src/lib.rs": ROLE_LIB.replace(
                "mod util;\n", "mod util;\n#[cfg(test)]\nmod more_tests;\n"
            ).replace("    1\n", "    1 + 0\n"),
            "src/more_tests.rs": "#[test]\nfn more() {}\n",
        },
        {"src/lib.rs": (Role.SOURCE, "both"), "src/more_tests.rs": (Role.SOURCE, "test")},
    ),
    (
        "item-level-cfg-test",
        {
            "src/util.rs": ROLE_UTIL.replace("2", "3")
            + "\n#[cfg(all(test, not(miri)))]\npub fn fake() -> u8 {\n    0\n}\n"
        },
        {"src/util.rs": (Role.SOURCE, "both")},
    ),
    (
        "new-source-with-tests",
        {
            "src/lib.rs": ROLE_LIB.replace("mod util;\n", "mod util;\nmod extra;\n"),
            "src/extra.rs": "pub fn e() {}\n#[cfg(test)]\nmod tests {}\n",
        },
        {"src/lib.rs": (Role.SOURCE, "fix"), "src/extra.rs": (Role.SOURCE, "fix")},
    ),
    (
        "support-roles",
        {
            "benches/speed.rs": "fn main() { let _ = 1; }\n",
            "examples/demo.rs": "fn main() { let _ = 2; }\n",
            "build.rs": "fn main() { let _ = 3; }\n",
            "Cargo.toml": ROLE_MANIFEST + "\n[dev-dependencies]\n",
            "Cargo.lock": "version = 4\n",
            "README.md": "demo, fixed\n",
        },
        {
            "benches/speed.rs": (Role.BENCH, "fix"),
            "examples/demo.rs": (Role.EXAMPLE, "fix"),
            "build.rs": (Role.BUILD_SCRIPT, "fix"),
            "Cargo.toml": (Role.MANIFEST, "fix"),
            "Cargo.lock": (Role.LOCKFILE, "fix"),
            "README.md": (Role.OTHER, "fix"),
        },
    ),
]


@pytest.mark.parametrize(
    ("changes", "expected"), [c[1:] for c in ROLE_CASES], ids=[c[0] for c in ROLE_CASES]
)
def test_role_fixture_diffs_split_and_round_trip(
    make_repo: Callable[[str], GitRepo],
    tmp_path: Path,
    changes: dict[str, str | None],
    expected: dict[str, tuple[Role, str]],
) -> None:
    repo = make_repo("origin")
    base = repo.commit("base", ROLE_BASE, "2024-01-10T12:00:00+00:00")
    fix = repo.commit("fix", changes, "2024-01-11T12:00:00+00:00")
    git = Git(SubprocessRunner(), repo.path)
    files = parse_diff(git.diff(base, fix))
    split = split_diff(files, GitTree(git, base), GitTree(git, fix))

    assert {f.path: (f.role, f.patch) for f in split.files} == expected
    if "src/extra.rs" in expected:
        assert split.notes == ["src/extra.rs: new file with test-only code kept whole in fix.patch"]

    git.checkout_clean(base)
    for name, text in (("test.patch", split.test_patch), ("fix.patch", split.fix_patch)):
        if text:
            (tmp_path / name).write_text(text)
            git.apply(tmp_path / name)
    assert git.matches(fix, sorted({p for d in files for p in d.paths}))

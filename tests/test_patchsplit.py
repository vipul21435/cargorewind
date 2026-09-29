from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from cargorewind.gitops import Git
from cargorewind.patchsplit import (
    PatchError,
    Side,
    file_role,
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
    split = split_diff(files, lambda p: git.show_file(base, p), lambda p: git.show_file(fix, p))

    assert sorted(d.path for d in split.test_files) == ["src/lib.rs", "tests/integration.rs"]
    assert sorted(d.path for d in split.fix_files) == [
        "README.md",
        "assets/blob.bin",
        "examples/old.rs",
        "src/lib.rs",
        "src/new.rs",
    ]
    assert split.shared_files == ["src/lib.rs"]
    assert len(split.shared_hunks) == 1
    shared = split.shared_hunks[0]
    assert (shared.test_lines, shared.fix_lines) == (5, 2)
    assert split.notes == [
        "src/new.rs: new file with a #[cfg(test)] module kept whole in fix.patch"
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
    split = split_diff(files, lambda p: "a\n", lambda p: "b\n")
    assert [d.path for d in split.fix_files] == ["src/lib.rs"]
    assert [d.path for d in split.test_files] == ["crates/x/tests/t.rs"]
    assert split.shared_hunks == []
    assert split.fix_patch == render_patch(files[:1])


def test_rust_file_that_cannot_be_read_goes_to_fix() -> None:
    diff = "diff --git a/src/a.rs b/src/a.rs\n--- a/src/a.rs\n+++ b/src/a.rs\n@@ -1 +1 @@\n-a\n+b\n"
    split = split_diff(parse_diff(diff), lambda p: None, lambda p: None)
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
    split = split_diff(parse_diff(diff), lambda p: base, lambda p: fix)
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


@pytest.mark.parametrize(
    ("path", "role"),
    [
        ("tests/lib.rs", "test"),
        ("crates/core/tests/data/input.txt", "test"),
        ("src/lib.rs", "rust"),
        ("src/tests.rs", "rust"),
        ("benches/b.rs", "rust"),
        ("README.md", "other"),
    ],
)
def test_file_role(path: str, role: str) -> None:
    assert file_role(path) == role


def test_side_values() -> None:
    assert [s.value for s in Side] == ["test", "fix"]

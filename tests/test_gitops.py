from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cargorewind.gitops import CheckoutLock, GitError, GitTree, open_checkout, repo_slug
from cargorewind.patchsplit import parse_diff, split_diff
from cargorewind.rewind import build_overlays
from cargorewind.runner import CommandError, SubprocessRunner
from tests.conftest import GitRepo


@pytest.mark.parametrize(
    ("source", "slug"),
    [
        ("https://github.com/rapidfuzz/strsim-rs", "strsim-rs"),
        ("https://github.com/rapidfuzz/strsim-rs.git/", "strsim-rs"),
        ("git@github.com:Owner/My_Crate.git", "my_crate"),
        ("examples/strsim/strsim-rs.bundle", "strsim-rs"),
        ("weird name!!", "weird-name"),
        ("...", "repo"),
    ],
)
def test_repo_slug(source: str, slug: str) -> None:
    assert repo_slug(source) == slug


def _two_commits(repo: GitRepo) -> tuple[str, str]:
    base = repo.commit("base", {"a.txt": "one\n"}, "2019-12-12T21:48:41-05:00")
    fix = repo.commit("fix", {"a.txt": "two\n", "b.txt": "new\n"}, "2019-12-12T21:48:41-05:00")
    return base, fix


def test_clone_resolve_and_inspect(make_repo: Callable[[str], GitRepo], tmp_path: Path) -> None:
    origin = make_repo("origin")
    base, fix = _two_commits(origin)
    git = open_checkout(SubprocessRunner(), str(origin.path), tmp_path / "work")

    assert git.rev_parse(fix[:10]) == fix
    assert git.first_parent(fix) == base
    assert git.commit_date(fix) == datetime(2019, 12, 13, 2, 48, 41, tzinfo=UTC)
    assert git.show_file(base, "a.txt") == "one\n"
    assert git.show_file(base, "b.txt") is None
    assert "+two" in git.diff(base, fix)

    with pytest.raises(GitError, match="unknown commit"):
        git.rev_parse("0" * 40)
    with pytest.raises(GitError, match="has no parent"):
        git.first_parent(base)

    dest = tmp_path / "ctx" / "repo"
    git.archive(fix, dest)
    assert sorted(p.name for p in dest.iterdir()) == ["a.txt", "b.txt"]
    assert not (tmp_path / "ctx" / ".repo.tar").exists()


def test_reopen_fetches_new_commits(make_repo: Callable[[str], GitRepo], tmp_path: Path) -> None:
    origin = make_repo("origin")
    _two_commits(origin)
    open_checkout(SubprocessRunner(), str(origin.path), tmp_path / "work")
    later = origin.commit("later", {"c.txt": "c\n"}, "2020-01-01T00:00:00+00:00")
    git = open_checkout(SubprocessRunner(), str(origin.path), tmp_path / "work")
    assert git.rev_parse(later) == later


def test_stale_non_repo_directory_is_replaced(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    origin = make_repo("origin")
    _, fix = _two_commits(origin)
    stale = tmp_path / "work" / "repo"
    stale.mkdir(parents=True)
    (stale / "junk").write_text("x")
    git = open_checkout(SubprocessRunner(), str(origin.path), tmp_path / "work")
    assert git.rev_parse(fix) == fix


def test_clone_failure_raises(tmp_path: Path) -> None:
    with pytest.raises(CommandError):
        open_checkout(SubprocessRunner(), str(tmp_path / "missing"), tmp_path / "work")


def test_apply_and_matches(make_repo: Callable[[str], GitRepo], tmp_path: Path) -> None:
    origin = make_repo("origin")
    base, fix = _two_commits(origin)
    git = open_checkout(SubprocessRunner(), str(origin.path), tmp_path / "work")
    patch = tmp_path / "fix.patch"
    patch.write_text(git.diff(base, fix))

    git.checkout_clean(base)
    assert git.matches(fix, [])
    assert not git.matches(fix, ["a.txt", "b.txt"])
    git.checkout_clean(base)
    git.apply(patch)
    assert git.matches(fix, ["a.txt", "b.txt"])

    git.checkout_clean(fix)
    with pytest.raises(GitError, match="does not apply"):
        git.apply(patch)


def test_checkout_lock_is_exclusive_across_objects_and_reentrant(tmp_path: Path) -> None:
    path = tmp_path / "work" / ".repo.lock"
    first, second = CheckoutLock(path), CheckoutLock(path)
    acquired = threading.Event()

    def contender() -> None:
        with second:
            acquired.set()

    with first, first:  # reentrant within one object
        worker = threading.Thread(target=contender)
        worker.start()
        assert not acquired.wait(0.3)  # blocked while the first object holds it
    assert acquired.wait(5)
    worker.join(5)
    with first:  # released by the contender, free again
        assert path.exists()


def test_rewind_overlays_refuse_a_checkout_that_does_not_match(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    origin = make_repo("origin")
    base, fix = _two_commits(origin)
    git = open_checkout(SubprocessRunner(), str(origin.path), tmp_path / "work")
    split = split_diff(parse_diff(git.diff(base, fix)), GitTree(git, base), GitTree(git, fix))
    (tmp_path / "test.patch").write_text(split.test_patch)
    (tmp_path / "fix.patch").write_text(split.fix_patch)
    assert build_overlays(git, base, fix, split, tmp_path)["after"].files["b.txt"] == b"new\n"
    # Another run moved the tree under us: here, patches for a different target.
    with pytest.raises(GitError, match="does not match the fix commit"):
        build_overlays(git, base, base, split, tmp_path)


def linked_repo(origin: GitRepo) -> tuple[str, str]:
    """rust-toolchain is a symlink to rust-toolchain.toml, as some repositories keep it."""
    origin.write(
        {
            "Cargo.toml": '[package]\nname = "x"\nversion = "0.1.0"\nedition = "2021"\n',
            "rust-toolchain.toml": '[toolchain]\nchannel = "1.70.0"\n',
            "src/lib.rs": "pub fn f() {}\n",
            "docs/readme.txt": "docs\n",
        }
    )
    links = {
        "rust-toolchain": "rust-toolchain.toml",
        "outside": "../../etc/passwd",
        "absolute": "/etc/passwd",
        "dangling": "missing.toml",
        "loop-a": "loop-b",
        "loop-b": "loop-a",
        "to-dir": "docs",
        "docs/up": "../rust-toolchain",
    }
    for name, target in links.items():
        (origin.path / name).symlink_to(target)
    base = origin.commit("base", {}, "2024-01-10T12:00:00+00:00")
    fix = origin.commit(
        "fix", {"src/lib.rs": "pub fn f() -> u8 { 1 }\n"}, "2024-01-11T12:00:00+00:00"
    )
    return base, fix


def test_git_tree_follows_symlinks_only_when_asked(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    origin = make_repo("origin")
    base, _ = linked_repo(origin)
    git = open_checkout(SubprocessRunner(), str(origin.path), tmp_path / "work")
    assert git.list_modes(base)["rust-toolchain"] == "120000"
    plain = GitTree(git, base)
    assert plain.read("rust-toolchain") == "rust-toolchain.toml"  # what a diff shows
    tree = GitTree(git, base, follow_links=True)
    assert tree.read("rust-toolchain") == '[toolchain]\nchannel = "1.70.0"\n'
    assert tree.read("docs/up") == tree.read("rust-toolchain")  # a link to a link
    assert tree.resolve("docs/up") == "rust-toolchain.toml"
    for name in ("outside", "absolute", "dangling", "loop-a", "to-dir", "missing"):
        assert tree.read(name) is None, name
    assert "rust-toolchain" in tree.paths() and "src/lib.rs" in tree.paths()
    assert git.list_files(base) == sorted(git.list_files(base))


def test_grep_words_finds_whole_words_in_matching_files(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    origin = make_repo("origin")
    files = {
        "src/a.rs": "fn alpha() { beta(); }\n",
        "src/deep/b.rs": "// beta\nlet alphabet = 1;\n",
        "notes.txt": "alpha gamma\n",
    }
    rev = origin.commit("base", files, "2024-01-10T12:00:00+00:00")
    git = open_checkout(SubprocessRunner(), str(origin.path), tmp_path / "work")
    found = git.grep_words(rev, ["alpha", "beta", "gamma"], "*.rs")
    assert found == {"alpha": ["src/a.rs"], "beta": ["src/a.rs", "src/deep/b.rs"]}
    assert git.grep_words(rev, ["gamma"], "*.rs") == {}
    assert git.grep_words(rev, [], "*.rs") == {}
    with pytest.raises(CommandError):
        git.grep_words("0" * 40, ["alpha"], "*.rs")


GIT_CONFIG = {
    "grep.lineNumber": "true",
    "grep.column": "true",
    "color.ui": "always",
    "color.grep": "always",
}


def test_grep_words_ignores_the_user_git_configuration(
    make_repo: Callable[[str], GitRepo], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression: grep.lineNumber, grep.column and color settings changed the output
    # format, so every word looked absent at base and the image build's probe failed.
    origin = make_repo("origin")
    files = {"src/a.rs": "fn alpha() { beta(); }\n", "src/b.rs": "// beta\nlet alphabet = 1;\n"}
    rev = origin.commit("base", files, "2024-01-10T12:00:00+00:00")
    git = open_checkout(SubprocessRunner(), str(origin.path), tmp_path / "work")
    monkeypatch.setenv("GIT_CONFIG_COUNT", str(len(GIT_CONFIG)))
    for n, (key, value) in enumerate(GIT_CONFIG.items()):
        monkeypatch.setenv(f"GIT_CONFIG_KEY_{n}", key)
        monkeypatch.setenv(f"GIT_CONFIG_VALUE_{n}", value)
    found = git.grep_words(rev, ["alpha", "beta", "gamma"], "*.rs")
    assert found == {"alpha": ["src/a.rs"], "beta": ["src/a.rs", "src/b.rs"]}


def test_grep_words_takes_tens_of_thousands_of_words(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    # Regression: every word was a "-e word" argument, so a large generated fix hit
    # the argument length limit (OSError: Argument list too long).
    origin = make_repo("origin")
    rev = origin.commit(
        "base",
        {"src/a.rs": "pub const GENERATED_ENTRY_NUMBER_7: u32 = 7;\nfn alpha() {}\n"},
        "2024-01-10T12:00:00+00:00",
    )
    git = open_checkout(SubprocessRunner(), str(origin.path), tmp_path / "work")
    words = [f"GENERATED_ENTRY_NUMBER_{n:05d}_LONG_NAME" for n in range(40_000)]
    words += ["GENERATED_ENTRY_NUMBER_7", "alpha"]
    assert sum(len(w) + 4 for w in words) > 1_048_576  # larger than ARG_MAX on macOS
    found = git.grep_words(rev, words, "*.rs")
    assert found == {"GENERATED_ENTRY_NUMBER_7": ["src/a.rs"], "alpha": ["src/a.rs"]}

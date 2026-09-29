from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cargorewind.gitops import GitError, open_checkout, repo_slug
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

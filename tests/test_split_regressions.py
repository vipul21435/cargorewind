"""Regression tests for review findings on the Rust-aware split.

Each test builds a base and a fix commit in a throwaway repository, splits the diff,
proves the patches with ``check_split`` and checks where the lines went.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from cargorewind.gitops import Git, GitTree
from cargorewind.layout import Role
from cargorewind.patchsplit import SplitResult, parse_diff, split_diff
from cargorewind.runner import SubprocessRunner
from cargorewind.splitreport import SplitChecks, check_split
from tests.conftest import GitRepo

BASE_DATE = "2024-01-10T12:00:00+00:00"
FIX_DATE = "2024-01-11T12:00:00+00:00"
MANIFEST = '[package]\nname = "tool"\nversion = "0.1.0"\n'


@dataclass
class Split:
    git: Git
    base: str
    fix: str
    result: SplitResult
    checks: SplitChecks
    patches: Path

    def files(self) -> dict[str, str]:
        return {f.path: f.patch for f in self.result.files}

    def mid(self, path: str) -> str | None:
        """Content of ``path`` in the intermediate tree (base plus test.patch)."""
        self.git.checkout_clean(self.base)
        try:
            if self.result.test_files:
                self.git.apply(self.patches / "test.patch")
            target = self.git.repo / path
            return target.read_bytes().decode() if target.exists() else None
        finally:
            self.git.checkout_clean(self.base)


def _split(repo: GitRepo, patches: Path, base: str, fix: str) -> Split:
    git = Git(SubprocessRunner(), repo.path)
    result = split_diff(parse_diff(git.diff(base, fix)), GitTree(git, base), GitTree(git, fix))
    patches.mkdir(parents=True, exist_ok=True)
    (patches / "test.patch").write_text(result.test_patch, newline="")
    (patches / "fix.patch").write_text(result.fix_patch, newline="")
    checks = check_split(git, base, fix, result, patches)
    return Split(git, base, fix, result, checks, patches)


def _commits(
    make_repo: Callable[[str], GitRepo],
    tmp_path: Path,
    base_files: dict[str, str | None],
    fix_files: dict[str, str | None],
) -> Split:
    repo = make_repo("origin")
    base = repo.commit("base", base_files, BASE_DATE)
    fix = repo.commit("fix", fix_files, FIX_DATE)
    return _split(repo, tmp_path / "patches", base, fix)


def test_fixture_crates_under_tests_go_to_the_test_patch(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    fixture = '[package]\nname = "{}"\nversion = "0.1.0"\n'
    split = _commits(
        make_repo,
        tmp_path,
        {
            "Cargo.toml": MANIFEST,
            "src/lib.rs": "pub fn count(s: &str) -> usize {\n    s.len()\n}\n",
            "tests/cli.rs": "#[test]\nfn basic() {}\n",
            "tests/fixtures/basic/Cargo.toml": fixture.format("basic"),
            "tests/fixtures/basic/src/lib.rs": "pub fn b() {}\n",
        },
        {
            "src/lib.rs": "pub fn count(s: &str) -> usize {\n    s.chars().count()\n}\n",
            "tests/cli.rs": "#[test]\nfn basic() {}\n#[test]\nfn wide() {}\n",
            "tests/fixtures/wide/Cargo.toml": fixture.format("wide"),
            "tests/fixtures/wide/src/lib.rs": 'pub fn w() -> &\'static str {\n    "e"\n}\n',
        },
    )
    assert split.checks.ok, split.checks.error()
    assert split.files() == {
        "src/lib.rs": "fix",
        "tests/cli.rs": "test",
        "tests/fixtures/wide/Cargo.toml": "test",
        "tests/fixtures/wide/src/lib.rs": "test",
    }
    assert [p.name for p in split.result.packages] == ["tool"]
    roles = {f.path: (f.role, f.package) for f in split.result.files}
    assert roles["tests/fixtures/wide/Cargo.toml"] == (Role.TEST, ".")
    assert "tests/fixtures/wide" not in split.result.fix_patch


def test_statement_after_a_cfg_test_block_stays_in_the_fix_patch(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    base_lib = """\
pub struct Counter {
    pub fail_next: bool,
    pub hits: u32,
}

pub fn bump(c: &mut Counter, total: &mut u32) {
    #[cfg(test)]
    if c.fail_next {
        c.fail_next = false;
    }
    *total += 2;
    c.hits += 1;
}

#[cfg(test)]
mod tests {
    #[test]
    fn t() {}
}
"""
    split = _commits(
        make_repo,
        tmp_path,
        {"Cargo.toml": MANIFEST, "src/lib.rs": base_lib},
        {"src/lib.rs": base_lib.replace("*total += 2;", "*total += 1;")},
    )
    assert split.checks.ok
    assert split.files() == {"src/lib.rs": "fix"}
    assert "+    *total += 1;" in split.result.fix_patch
    assert split.result.test_patch == ""


def test_cfg_test_let_with_an_if_expression_is_one_region(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    lets = "".join(f"    let _v{n} = y + {n};\n" for n in range(8))
    base_lib = f"pub fn scale(x: u32) -> u32 {{\n    let y = x;\n{lets}    y * 3\n}}\n"
    probe = (
        "    #[cfg(test)]\n    let _probe = if y > 100 {\n        100\n    } else {\n"
        "        y\n    };\n"
    )
    fix_lib = base_lib.replace("    let y = x;\n", "    let y = x;\n" + probe).replace(
        "y * 3", "y * 2"
    )
    split = _commits(
        make_repo,
        tmp_path,
        {"Cargo.toml": MANIFEST, "src/lib.rs": base_lib},
        {"src/lib.rs": fix_lib},
    )
    assert split.checks.ok
    assert split.files() == {"src/lib.rs": "both"}
    mid = split.mid("src/lib.rs")
    assert mid is not None and probe in mid and "y * 3" in mid
    assert "+    y * 2" in split.result.fix_patch
    assert "_probe" not in split.result.fix_patch.replace(" let _probe", "")


def test_plain_mod_line_of_a_new_cfg_test_file_goes_with_the_file(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    accented = "\\u{e9}"  # a Rust escape keeps this file ASCII
    tests_rs = (
        "#![cfg(test)]\nuse super::width;\n\n"
        '#[test]\nfn ascii() {\n    assert_eq!(width("abc"), 3);\n}\n\n'
        f'#[test]\nfn accented() {{\n    assert_eq!(width("{accented}"), 1);\n}}\n'
    )
    base_lib = "pub fn width(s: &str) -> usize {\n    s.len()\n}\n"
    fix_lib = "pub fn width(s: &str) -> usize {\n    s.chars().count()\n}\n\nmod tests;\n"
    split = _commits(
        make_repo,
        tmp_path,
        {"Cargo.toml": MANIFEST, "src/lib.rs": base_lib},
        {"src/lib.rs": fix_lib, "src/tests.rs": tests_rs},
    )
    assert split.checks.ok
    assert split.files() == {"src/lib.rs": "both", "src/tests.rs": "test"}
    assert "+mod tests;" in split.result.test_patch
    assert "+mod tests;" not in split.result.fix_patch
    mid = split.mid("src/lib.rs")
    assert mid == "pub fn width(s: &str) -> usize {\n    s.len()\n}\nmod tests;\n"
    regions = [(r.path, r.region.kind, r.region.name) for r in split.result.regions]
    assert ("src/lib.rs", "module-decl", "tests") in regions


def test_deleting_a_cfg_test_file_deletes_its_mod_line_in_the_same_patch(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    base_lib = "pub fn width(s: &str) -> usize {\n    s.len()\n}\n\nmod tests;\n"
    split = _commits(
        make_repo,
        tmp_path,
        {
            "Cargo.toml": MANIFEST,
            "src/lib.rs": base_lib,
            "src/tests.rs": "#![cfg(test)]\n#[test]\nfn t() {}\n",
        },
        {
            "src/lib.rs": "pub fn width(s: &str) -> usize {\n    s.chars().count()\n}\n",
            "src/tests.rs": None,
        },
    )
    assert split.checks.ok
    assert split.files() == {"src/lib.rs": "both", "src/tests.rs": "test"}
    assert "-mod tests;" in split.result.test_patch
    mid = split.mid("src/lib.rs")
    assert mid is not None and "mod tests;" not in mid


def test_test_only_module_of_a_new_source_file_stays_with_it(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    split = _commits(
        make_repo,
        tmp_path,
        {"Cargo.toml": MANIFEST, "src/lib.rs": "pub fn a() {}\n"},
        {
            "src/lib.rs": "pub fn a() {}\npub mod extra;\n",
            "src/extra.rs": "pub fn e() {}\n#[cfg(test)]\nmod tests;\n",
            "src/extra/tests.rs": "#[test]\nfn t() {}\n",
        },
    )
    assert split.checks.ok
    assert split.files() == {
        "src/extra.rs": "fix",
        "src/extra/tests.rs": "fix",
        "src/lib.rs": "fix",
    }
    assert (
        "src/extra/tests.rs: test-only module kept in fix.patch with src/extra.rs, "
        "the new file that declares it"
    ) in split.result.notes

    # And the deletion of both: the module file must not leave a dangling `mod` line.
    repo = GitRepo(split.git.repo)
    repo.git("checkout", "--quiet", "main")
    gone = repo.commit("drop extra", {"src/extra.rs": None, "src/extra/tests.rs": None}, FIX_DATE)
    again = _split(repo, tmp_path / "again", split.fix, gone)
    assert again.checks.ok
    assert again.files()["src/extra/tests.rs"] == "fix"


def test_data_below_src_tests_goes_with_the_tests(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    tests_rs = '#[test]\nfn case1() {\n    let _ = include_str!("tests/data/case1.txt");\n}\n'
    case2 = '#[test]\nfn case2() {\n    let _ = include_str!("tests/data/case2.txt");\n}\n'
    split = _commits(
        make_repo,
        tmp_path,
        {
            "Cargo.toml": MANIFEST,
            "src/lib.rs": "pub fn parse() -> u8 {\n    1\n}\n#[cfg(test)]\nmod tests;\n",
            "src/tests.rs": tests_rs,
            "src/tests/data/case1.txt": "one\n",
        },
        {
            "src/lib.rs": "pub fn parse() -> u8 {\n    2\n}\n#[cfg(test)]\nmod tests;\n",
            "src/tests.rs": tests_rs + case2,
            "src/tests/data/case2.txt": "two\n",
        },
    )
    assert split.checks.ok
    assert split.files() == {
        "src/lib.rs": "fix",
        "src/tests.rs": "test",
        "src/tests/data/case2.txt": "test",
    }


def test_test_lines_after_a_last_line_without_newline(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    split = _commits(
        make_repo,
        tmp_path,
        {"Cargo.toml": MANIFEST, "src/lib.rs": "pub fn f() -> u8 {\n    1\n}"},
        {
            "src/lib.rs": "pub fn f() -> u8 {\n    2\n}\n\n#[cfg(test)]\nmod tests;\n",
            "src/tests.rs": "#[test]\nfn t() {}\n",
        },
    )
    assert split.checks.ok, split.checks.error()
    assert split.files() == {"src/lib.rs": "both", "src/tests.rs": "test"}
    assert split.mid("src/lib.rs") == "pub fn f() -> u8 {\n    1\n}\n#[cfg(test)]\nmod tests;\n"


def test_crlf_files_and_form_feeds_split_and_apply(
    make_repo: Callable[[str], GitRepo], tmp_path: Path
) -> None:
    repo = make_repo("origin")
    repo.git("config", "core.autocrlf", "false")
    lib = "// section one\x0c page break\npub fn f() -> u8 {\n    1\n}\n"
    files = {
        "Cargo.toml": MANIFEST.encode(),
        "src/lib.rs": lib.encode(),
        "src/crlf.rs": b"pub fn g() -> u8 {\r\n    1\r\n}\r\n",
        "README.md": b"hello\r\nworld\r\n",
    }
    for name, data in files.items():
        (repo.path / name).parent.mkdir(parents=True, exist_ok=True)
        (repo.path / name).write_bytes(data)
    base = repo.commit("base", {}, BASE_DATE)
    changes = {
        "src/lib.rs": lib.replace("    1", "    2").encode(),
        "src/crlf.rs": b"pub fn g() -> u8 {\r\n    2\r\n}\r\n",
        "README.md": b"hello\r\nthere\r\n",
    }
    for name, data in changes.items():
        (repo.path / name).write_bytes(data)
    fix = repo.commit("fix", {}, FIX_DATE)
    split = _split(repo, tmp_path / "patches", base, fix)
    assert split.checks.ok, split.checks.error()
    assert "+    2\r\n" in split.result.fix_patch
    assert "\x0c page break" in split.result.fix_patch


_PROD = "pub fn f{n}() -> u8 {{\n    {v}\n}}\n"
_TEST = "    #[test]\n    fn t{n}() {{\n        assert_eq!(super::f0(), {v});\n    }}\n"


def _random_source(rng: random.Random, values: list[int], tests: list[int], tail: bool) -> str:
    parts = [_PROD.format(n=n, v=v) for n, v in enumerate(values)]
    if tests:
        body = "".join(_TEST.format(n=n, v=n) for n in tests)
        parts.append("\n#[cfg(test)]\nmod tests {\n" + body + "}\n")
    if tail:
        parts.append("\n#[cfg(test)]\nfn helper() -> u8 {\n    7\n}\n")
    text = "".join(parts)
    return text.rstrip("\n") if rng.random() < 0.5 else text


@pytest.mark.parametrize("seed", range(10))
def test_random_edits_with_and_without_final_newlines_round_trip(
    make_repo: Callable[[str], GitRepo], tmp_path: Path, seed: int
) -> None:
    rng = random.Random(seed)
    values = [rng.randint(0, 9) for _ in range(rng.randint(1, 3))]
    tests = sorted(rng.sample(range(6), rng.randint(0, 2)))
    tail = rng.random() < 0.3
    base_src = _random_source(rng, values, tests, tail)
    fix_values = [v if rng.random() < 0.5 else v + 1 for v in values]
    fix_tests = sorted({*tests, *rng.sample(range(6), rng.randint(0, 2))})
    fix_src = _random_source(rng, fix_values, fix_tests, tail or rng.random() < 0.5)
    if fix_src == base_src:
        fix_src += "// touched\n"
    split = _commits(
        make_repo,
        tmp_path,
        {"Cargo.toml": MANIFEST, "src/lib.rs": base_src},
        {"src/lib.rs": fix_src},
    )
    assert split.checks.ok, (seed, split.checks.error())
    mid = split.mid("src/lib.rs")
    assert mid is not None
    known = set(base_src.split("\n")) | set(fix_src.split("\n"))
    assert set(mid.split("\n")) <= known, seed

from __future__ import annotations

import pytest

from cargorewind.layout import Layout, MemoryTree, Role, glob_match

WORKSPACE = """\
[workspace]
members = ["crates/*", "tools/**/cli", "./apps/web"]
exclude = ["crates/skip"]

[package]
name = "umbrella"
version = "0.1.0"
"""

CORE_MANIFEST = """\
[package]
name = "core-lib"
version = "0.1.0"
build = "tools/build.rs"
autotests = false

[lib]
path = "lib/core.rs"

[[test]]
name = "it"
path = "checks/it.rs"

[[test]]
name = "named"

[[bench]]
name = "speed"
path = "perf/speed.rs"

[[example]]
name = "demo"
path = "samples/demo.rs"

[[bin]]
name = "core-cli"
"""

CORE_LIB = """\
mod parser;
mod net {
    mod tcp;
}
#[cfg(test)]
mod tests;
#[cfg(all(test, feature = "slow"))]
#[path = "testdata/helpers.rs"]
mod helpers;
mod checks;
"""


def _tree() -> MemoryTree:
    return MemoryTree(
        {
            "Cargo.toml": WORKSPACE,
            "src/lib.rs": "pub fn umbrella() {}\n",
            "README.md": "docs\n",
            "Cargo.lock": "version = 3\n",
            "crates/core/Cargo.toml": CORE_MANIFEST,
            "crates/core/lib/core.rs": CORE_LIB,
            "crates/core/lib/parser.rs": "mod lexer;\n",
            "crates/core/lib/parser/lexer.rs": "pub fn lex() {}\n",
            "crates/core/lib/net/tcp.rs": "pub fn connect() {}\n",
            "crates/core/lib/tests.rs": "mod fixtures;\n",
            "crates/core/lib/tests/fixtures.rs": "pub const X: u8 = 1;\n",
            "crates/core/lib/testdata/helpers.rs": "pub fn helper() {}\n",
            "crates/core/lib/checks.rs": "#![cfg(test)]\nfn check() {}\n",
            "crates/core/lib/orphan.rs": "fn unused() {}\n",
            "crates/core/checks/it.rs": "mod common;\n#[test]\nfn it() {}\n",
            "crates/core/checks/common.rs": "pub fn setup() {}\n",
            "crates/core/checks/data.json": "{}\n",
            "crates/core/tests/named.rs": "#[test]\nfn named() {}\n",
            "crates/core/tests/ignored.rs": "#[test]\nfn ignored() {}\n",
            "crates/core/perf/speed.rs": "fn main() {}\n",
            "crates/core/samples/demo.rs": "fn main() {}\n",
            "crates/core/tools/build.rs": "fn main() {}\n",
            "crates/core/src/bin/core-cli.rs": "fn main() {}\n",
            "crates/skip/Cargo.toml": '[package]\nname = "skip"\n',
            "crates/skip/src/lib.rs": "",
            "tools/a/b/cli/Cargo.toml": '[package]\nname = "cli"\n',
            "tools/a/b/cli/src/main.rs": "fn main() {}\n",
            "tools/a/b/cli/src/bin/extra/main.rs": "fn main() {}\n",
            "apps/web/Cargo.toml": '[package]\nname = "web"\n',
            "fuzz/Cargo.toml": (
                '[package]\nname = "fuzz"\n[[bin]]\nname = "f1"\npath = "targets/f1.rs"\n'
            ),
            "fuzz/targets/f1.rs": "fn main() {}\n",
            "broken/Cargo.toml": "[package\n",
        }
    )


def test_workspace_members_follow_globs_and_excludes() -> None:
    layout = Layout(_tree())
    workspaces = {p.display_root: (p.name, p.workspace) for p in layout.packages}
    assert workspaces == {
        ".": ("umbrella", ""),
        "apps/web": ("web", ""),
        "crates/core": ("core-lib", ""),
        "crates/skip": ("skip", None),
        "fuzz": ("fuzz", None),
        "tools/a/b/cli": ("cli", ""),
    }
    assert len(layout.notes) == 1
    assert layout.notes[0].startswith("broken/Cargo.toml: not valid TOML")


def test_targets_honor_custom_paths_and_auto_discovery_flags() -> None:
    layout = Layout(_tree())
    core = next(p for p in layout.packages if p.name == "core-lib")
    assert [(t.label, t.path) for t in core.targets] == [
        ("bench:speed", "crates/core/perf/speed.rs"),
        ("bin:core-cli", "crates/core/src/bin/core-cli.rs"),
        ("build", "crates/core/tools/build.rs"),
        ("example:demo", "crates/core/samples/demo.rs"),
        ("lib", "crates/core/lib/core.rs"),
        ("test:it", "crates/core/checks/it.rs"),
        ("test:named", "crates/core/tests/named.rs"),
    ]
    cli = next(p for p in layout.packages if p.name == "cli")
    assert [t.label for t in cli.targets] == ["bin:extra", "bin:cli"]
    fuzz = next(p for p in layout.packages if p.name == "fuzz")
    assert [t.path for t in fuzz.targets] == ["fuzz/targets/f1.rs"]


@pytest.mark.parametrize(
    ("path", "role", "test_code", "target", "reason"),
    [
        ("crates/core/lib/core.rs", Role.SOURCE, False, "lib", "lib target root"),
        ("crates/core/lib/parser/lexer.rs", Role.SOURCE, False, "lib", "parser.rs:1"),
        ("crates/core/lib/net/tcp.rs", Role.SOURCE, False, "lib", "module `tcp`"),
        ("crates/core/lib/tests.rs", Role.SOURCE, True, "lib", "under cfg(test)"),
        ("crates/core/lib/tests/fixtures.rs", Role.SOURCE, True, "lib", "tests.rs:1"),
        ("crates/core/lib/testdata/helpers.rs", Role.SOURCE, True, "lib", "core.rs:9"),
        ("crates/core/lib/checks.rs", Role.SOURCE, True, "lib", "#![cfg(test)]"),
        ("crates/core/lib/orphan.rs", Role.SOURCE, False, "lib", "custom target lib"),
        ("crates/core/checks/it.rs", Role.TEST, True, "test:it", "test:it target root"),
        ("crates/core/checks/common.rs", Role.TEST, True, "test:it", "module `common`"),
        ("crates/core/checks/data.json", Role.TEST, True, "test:it", "custom target"),
        ("crates/core/tests/named.rs", Role.TEST, True, "test:named", "target root"),
        ("crates/core/tests/ignored.rs", Role.TEST, True, None, "under tests/"),
        ("crates/core/perf/speed.rs", Role.BENCH, False, "bench:speed", "target root"),
        ("crates/core/samples/demo.rs", Role.EXAMPLE, False, "example:demo", "root"),
        ("crates/core/tools/build.rs", Role.BUILD_SCRIPT, False, "build", "target root"),
        ("crates/core/Cargo.toml", Role.MANIFEST, False, None, "Cargo.toml"),
        ("Cargo.lock", Role.LOCKFILE, False, None, "Cargo.lock"),
        ("README.md", Role.OTHER, False, None, "not part of a Cargo target"),
        ("fuzz/targets/f1.rs", Role.SOURCE, False, "bin:f1", "target root"),
    ],
)
def test_classify_by_module_tree_and_conventions(
    path: str, role: Role, test_code: bool, target: str | None, reason: str
) -> None:
    info = Layout(_tree()).classify(path)
    assert (info.role, info.test_code, info.target) == (role, test_code, target)
    assert reason in info.reason


def test_packages_are_attributed_to_the_deepest_root() -> None:
    layout = Layout(_tree())
    assert layout.classify("crates/core/lib/parser.rs").package == "crates/core"
    assert layout.classify("src/lib.rs").package == "."
    assert layout.classify("README.md").package == "."


def test_conventions_without_any_manifest() -> None:
    layout = Layout(MemoryTree({}))
    cases = {
        "tests/lib.rs": Role.TEST,
        "crates/x/tests/data/input.txt": Role.TEST,
        "src/tests.rs": Role.SOURCE,
        "benches/b.rs": Role.BENCH,
        "examples/e.rs": Role.EXAMPLE,
        "build.rs": Role.BUILD_SCRIPT,
        "docs/guide.md": Role.OTHER,
    }
    assert {path: layout.classify(path).role for path in cases} == cases
    assert layout.classify("tests/lib.rs").package is None


class CountingTree(MemoryTree):
    def __init__(self, files: dict[str, str]) -> None:
        super().__init__(files)
        self.reads: list[str] = []

    def read(self, path: str) -> str | None:
        self.reads.append(path)
        return super().read(path)


def test_walk_only_reads_files_on_the_way_to_wanted_paths() -> None:
    tree = CountingTree(
        {
            "Cargo.toml": '[package]\nname = "p"\n',
            "src/lib.rs": "mod a;\nmod b;\n",
            "src/a.rs": "mod inner;\n",
            "src/a/inner.rs": "",
            "src/b.rs": "mod deep;\n",
            "src/b/deep.rs": "",
            "tests/t.rs": "",
        }
    )
    layout = Layout(tree, ["src/a/inner.rs"])
    assert layout.classify("src/a/inner.rs").reason.startswith("module `inner` of lib")
    assert sorted(tree.reads) == ["Cargo.toml", "src/a.rs", "src/a/inner.rs", "src/lib.rs"]


def test_file_shared_by_lib_and_a_test_keeps_the_source_role() -> None:
    tree = MemoryTree(
        {
            "Cargo.toml": '[package]\nname = "p"\n',
            "src/lib.rs": "pub mod util;\n",
            "src/util.rs": "pub fn f() {}\n",
            "tests/t.rs": '#[path = "../src/util.rs"]\nmod util;\n',
            "src/bin/tool.rs": '#[cfg(test)]\n#[path = "../util.rs"]\nmod util;\n',
        }
    )
    info = Layout(tree).classify("src/util.rs")
    assert (info.role, info.test_code, info.target) == (Role.SOURCE, False, "lib")


def test_modules_that_do_not_resolve_and_build_false() -> None:
    tree = MemoryTree(
        {
            "Cargo.toml": '[package]\nname = "p"\nbuild = false\n[lib]\nname = "q"\n',
            "src/lib.rs": 'mod missing;\n#[path = "nowhere.rs"]\nmod gone;\nmod nested;\n',
            "src/nested/mod.rs": "mod leaf;\n",
            "src/nested/leaf.rs": "",
            "build.rs": "fn main() {}\n",
        }
    )
    layout = Layout(tree)
    assert [t.label for t in layout.packages[0].targets] == ["lib"]
    assert layout.packages[0].targets[0].name == "q"
    assert layout.classify("src/nested/leaf.rs").reason.endswith("src/nested/mod.rs:1")
    assert layout.classify("build.rs").target is None


def test_inline_path_attribute_inside_a_non_mod_rs_file() -> None:
    tree = MemoryTree(
        {
            "Cargo.toml": '[package]\nname = "p"\n',
            "src/lib.rs": "mod outer;\n",
            "src/outer.rs": 'mod inner {\n    #[path = "x.rs"]\n    mod x;\n}\n',
            "src/outer/inner/x.rs": "mod y;\n",
            "src/outer/inner/y.rs": "",
        }
    )
    layout = Layout(tree)
    assert layout.classify("src/outer/inner/x.rs").target == "lib"
    # #[path] files act as mod.rs files: their children are siblings.
    assert layout.classify("src/outer/inner/y.rs").target == "lib"


@pytest.mark.parametrize(
    ("pattern", "path", "matches"),
    [
        ("crates/*", "crates/core", True),
        ("crates/*", "crates/core/sub", False),
        ("crates/*", "x/crates/core", False),
        ("crates/**", "crates/a/b", True),
        ("**/cli", "tools/a/cli", True),
        ("./apps/web", "apps/web", True),
        ("crates/c?re", "crates/core", True),
        ("crates/[ab]*", "crates/core", False),
    ],
)
def test_glob_match(pattern: str, path: str, matches: bool) -> None:
    assert glob_match(pattern, path) is matches

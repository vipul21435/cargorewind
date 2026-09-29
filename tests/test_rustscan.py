from __future__ import annotations

import pytest

from cargorewind.rustscan import (
    Cfg,
    ModuleDecl,
    cfg_test_regions,
    in_regions,
    scan_source,
)

LIB = """\
pub fn add(a: i32, b: i32) -> i32 {
    a + b
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn adds() {
        assert_eq!(add(1, 2), 3);
    }
}
"""


def test_finds_inline_test_module() -> None:
    scan = scan_source(LIB)
    assert scan.spans == [(5, 13)]
    region = scan.regions[0]
    assert (region.kind, region.name, region.cfg) == ("module", "tests", "test")


def test_braces_in_strings_comments_and_chars_are_ignored() -> None:
    src = """\
#[cfg(test)]
mod tests {
    // a stray } in a comment
    /* block } /* nested { */ still comment } */
    const S: &str = "}}} \\" {";
    const R: &str = r#"raw } "quoted" {"#;
    const B: &[u8] = br"}";
    const C: char = '}';
    const Q: char = '\\'';
    const U: char = '\\u{7D}';
    fn f<'a>(x: &'a str) -> &'a str { x }
    fn g() { 'outer: loop { break 'outer; } }
}
fn after() {}
"""
    assert cfg_test_regions(src) == [(1, 13)]


def test_extra_attributes_visibility_and_doc_comments() -> None:
    src = (
        "#[cfg(test)]\n/// helpers { for tests\n#[allow(dead_code)]\npub(crate) mod helpers {\n}\n"
    )
    assert cfg_test_regions(src) == [(1, 5)]


@pytest.mark.parametrize(
    ("attr", "test_only"),
    [
        ("#[cfg(test)]", True),
        ('#[cfg(all(test, feature = "std"))]', True),
        ("#[cfg(all(unix, all(test, not(miri))))]", True),
        ("#[cfg(any(test, doctest))]", True),
        ('#[cfg(any(test, feature = "x"))]', False),
        ("#[cfg(not(test))]", False),
        ('#[cfg(feature = "test")]', False),
        ("#[cfg(any())]", False),
        ("#[cfg_attr(test, derive(Debug))]", False),
        ("#[cfg(unix)]\n#[cfg(test)]", True),
    ],
)
def test_compound_cfg_predicates(attr: str, test_only: bool) -> None:
    src = f"{attr}\nmod m {{\n}}\n"
    assert bool(cfg_test_regions(src)) is test_only


def test_rendered_predicate_is_reported() -> None:
    scan = scan_source('#[cfg(all(test, feature = "std"))]\nmod m {}\n')
    assert scan.regions[0].cfg == 'all(test, feature = "std")'
    assert Cfg("any", args=(Cfg("test"), Cfg("unix"))).render() == "any(test, unix)"


def test_out_of_line_module_declarations() -> None:
    src = """\
mod parser;
#[cfg(test)]
mod tests;
#[cfg(all(test, feature = "slow"))]
#[path = "slow_checks.rs"]
pub mod slow;
mod r#type;
let r#mod = x;
"""
    scan = scan_source(src)
    assert scan.modules == [
        ModuleDecl("parser", 1, (), None, False),
        ModuleDecl("tests", 3, (), None, True),
        ModuleDecl("slow", 6, (), "slow_checks.rs", True),
        ModuleDecl("type", 7, (), None, False),
    ]
    assert [(r.kind, r.name, r.start_line, r.end_line) for r in scan.regions] == [
        ("module-decl", "tests", 2, 3),
        ("module-decl", "slow", 4, 6),
    ]


def test_declarations_inside_inline_modules_record_their_path() -> None:
    src = """\
mod outer {
    mod inner;
    #[cfg(test)]
    mod checks {
        mod deep;
    }
}
"""
    scan = scan_source(src)
    assert scan.modules == [
        ModuleDecl("inner", 2, ("outer",), None, False),
        ModuleDecl("deep", 5, ("outer", "checks"), None, True),
    ]


def test_item_level_attributes() -> None:
    src = """\
pub struct Config {
    pub name: String,
    #[cfg(test)]
    pub probe: u8,
    pub size: u8,
}

#[cfg(test)]
impl Config {
    fn fake() -> Self {
        todo!()
    }
}

#[cfg(test)]
use std::collections::{HashMap,
    HashSet};

#[cfg(test)]
const TABLE: [u8; 3] = [1, 2, 3];

#[cfg(test)]
thread_local! {
    static X: u8 = 0;
}

fn run(x: u8) -> u8 {
    #[cfg(test)]
    {
        eprintln!("x = {}", x);
    }
    #[cfg(test)]
    let y = x;
    match x {
        #[cfg(test)]
        0 => {
            1
        }
        _ => x,
    }
}
"""
    scan = scan_source(src)
    assert [(r.kind, r.name, r.start_line, r.end_line) for r in scan.regions] == [
        ("item", "probe", 3, 4),
        ("item", "impl", 8, 13),
        ("item", "use", 15, 17),
        ("item", "const TABLE", 19, 20),
        ("item", "thread_local", 22, 25),
        ("block", "", 28, 31),
        ("item", "let", 32, 33),
        ("item", "0", 35, 38),
    ]


def test_item_labels_and_trailing_fields() -> None:
    src = """\
#[cfg(test)]
pub(crate) async unsafe fn helper() {}
#[cfg(test)]
extern "C" fn callback() {}
#[cfg(test)]
macro_rules! check { () => {} }
enum E {
    A,
    #[cfg(test)]
    B { x: u8 },
    #[cfg(test)]
    C
}
"""
    scan = scan_source(src)
    assert [(r.name, r.start_line, r.end_line) for r in scan.regions] == [
        ("fn helper", 1, 2),
        ("fn callback", 3, 4),
        ("macro_rules check", 5, 6),
        ("B", 9, 10),
        ("C", 11, 12),
    ]


def test_inner_attributes_mark_the_file_or_the_enclosing_module() -> None:
    whole = scan_source("//! Test helpers.\n#![cfg(test)]\n\nmod util;\nfn f() {}\n")
    assert whole.file_cfg_test
    assert whole.spans == [(1, 5)]
    assert whole.modules == [ModuleDecl("util", 4, (), None, True)]

    nested = scan_source("mod m {\n    #![cfg(test)]\n    fn f() {}\n}\nfn g() {}\n")
    assert not nested.file_cfg_test
    assert nested.spans == [(1, 4)]
    block = scan_source("fn g() {\n    #![cfg(test)]\n}\n")
    assert [(r.kind, r.start_line, r.end_line) for r in block.regions] == [("block", 1, 3)]


def test_other_cfgs_and_plain_modules_are_not_test_code() -> None:
    src = '#[cfg(feature = "x")]\nmod x {\n}\nmod y {\n}\n#[test]\nfn t() {}\n'
    assert cfg_test_regions(src) == []


def test_several_modules_and_unterminated_input() -> None:
    src = "#[cfg(test)]\nmod a {\n}\n#[cfg(test)]\nmod b {\n  fn f() {\n"
    assert cfg_test_regions(src) == [(1, 3), (4, 6)]
    assert cfg_test_regions("#[cfg(test)]") == []
    assert cfg_test_regions("#[cfg(test)]\nfn f() {") == [(1, 2)]
    assert cfg_test_regions("") == []
    assert cfg_test_regions("#[cfg(") == []


def test_malformed_cfg_is_not_test_only() -> None:
    assert cfg_test_regions('#[cfg("x")]\nmod m {}\n') == []
    assert cfg_test_regions("#[cfg(test, unix)]\nmod m {}\n") == []
    assert cfg_test_regions("#[cfg(feature =)]\nmod m {}\n") == []


def test_in_regions() -> None:
    assert in_regions(5, [(1, 3), (5, 9)])
    assert not in_regions(4, [(1, 3), (5, 9)])

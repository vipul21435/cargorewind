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


def _region_lines(src: str) -> list[tuple[str, str, int, int]]:
    return [(r.kind, r.name, r.start_line, r.end_line) for r in scan_source(src).regions]


def test_block_like_statements_end_at_their_closing_brace() -> None:
    # Rust ends an if/for/match/loop/while statement at its brace, even when the next
    # statement starts with an operator (review finding: the region swallowed it).
    src = """\
pub fn bump(c: &mut Counter, total: &mut u32) {
    #[cfg(test)]
    if c.fail_next {
        c.fail_next = false;
    }
    *total += 2;
    c.hits += 1;
    #[cfg(test)]
    for i in 0..3 {
        log(i);
    }
    -1i32;
    #[cfg(test)]
    match c.mode {
        Mode::A => {}
    }
    &total;
    #[cfg(test)]
    'outer: loop {
        break 'outer;
    }
    |x: u8| x;
    #[cfg(test)]
    if a {
        b();
    } else if c {
        d();
    } else {
        e();
    }
    <u8>::default();
    #[cfg(test)]
    unsafe {
        f();
    }
    *total = 5;
}
"""
    assert _region_lines(src) == [
        ("item", "if", 2, 5),
        ("item", "for", 8, 11),
        ("item", "match", 13, 16),
        ("item", "'outer", 18, 21),
        ("item", "if", 23, 30),
        ("block", "", 32, 35),
    ]
    scan = scan_source(
        "fn run(p: &mut u32) {\n    #[cfg(test)]\n    for i in 0..3 {\n        log(i);\n"
        "    }\n    *p = 5;\n}\n"
    )
    assert [(r.start_line, r.end_line) for r in scan.regions] == [(2, 5)]


def test_let_static_and_const_items_end_at_their_semicolon() -> None:
    src = """\
fn scale(x: u32) -> u32 {
    let y = x;
    #[cfg(test)]
    let _probe = if y > 100 {
        100
    } else {
        y
    };
    #[cfg(test)]
    let c = Config {
        a: 1,
    }
    .with_b(2);
    y * 3
}
#[cfg(test)]
static TABLE: [u8; 2] = {
    [1, 2]
};
#[cfg(test)]
const LIMIT: u32 = {
    3
} + 1;
#[cfg(test)]
const fn helper() -> u8 {
    1
}
#[cfg(test)]
use std::{
    fmt,
};
#[cfg(test)]
extern crate alloc;
"""
    assert _region_lines(src) == [
        ("item", "let", 3, 8),
        ("item", "let", 9, 13),
        ("item", "static TABLE", 16, 19),
        ("item", "const LIMIT", 20, 23),
        ("item", "const fn", 24, 27),
        ("item", "use", 28, 31),
        ("item", "crate", 32, 33),
    ]


def test_item_bodies_skip_braces_in_generics_and_signatures() -> None:
    src = """\
#[cfg(test)]
impl Foo<{ N }> {
    fn f() {}
}
#[cfg(test)]
impl<F: Fn() -> u8> Bar<{ M }> for F where F: Copy {
    fn g() {}
}
#[cfg(test)]
fn sized() -> [u8; { 3 }] {
    [0; 3]
}
#[cfg(test)]
struct Unit<const N: usize = { 1 }>;
#[cfg(test)]
struct Wrapper(u8);
fn next() {}
#[cfg(test)]
thread_local! {
    static X: u8 = 1;
}
#[cfg(test)]
std::thread_local! {
    static Y: u8 = 1;
}
*x;
#[cfg(test)]
unsafe impl Send for Foo {}
#[cfg(test)]
async fn later() {}
"""
    assert _region_lines(src) == [
        ("item", "impl", 1, 4),
        ("item", "impl", 5, 8),
        ("item", "fn sized", 9, 12),
        ("item", "struct Unit", 13, 14),
        ("item", "struct Wrapper", 15, 16),
        ("item", "thread_local", 18, 21),
        ("item", "std", 22, 25),
        ("item", "impl", 27, 28),
        ("item", "fn later", 29, 30),
    ]


def test_match_arms_with_block_bodies_end_at_the_body() -> None:
    src = """\
fn f(x: i32) -> i32 {
    match x {
        #[cfg(test)]
        0 => {
            1
        }
        -1 => 2,
        #[cfg(test)]
        1 => if x > 0 {
            3
        } else {
            4
        },
        &2 => 5,
        #[cfg(test)]
        3 => Foo { a: 1 }.get(),
        _ => 6,
    }
}
"""
    assert _region_lines(src) == [
        ("item", "0", 3, 6),
        ("item", "1", 8, 13),
        ("item", "3", 15, 16),
    ]


def test_declaration_spans_cover_attributes() -> None:
    scan = scan_source('mod a;\n#[allow(dead_code)]\n#[path = "b.rs"]\nmod b;\n')
    assert [m.span for m in scan.modules] == [(1, 1), (2, 4)]
    assert ModuleDecl("x", 3, (), None, False).span == (3, 3)

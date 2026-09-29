from __future__ import annotations

from cargorewind.rustscan import cfg_test_regions, in_regions, strip_comments_and_literals

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
    assert cfg_test_regions(LIB) == [(5, 13)]


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
    fn f<'a>(x: &'a str) -> &'a str { x }
}
fn after() {}
"""
    assert cfg_test_regions(src) == [(1, 11)]


def test_allows_extra_attributes_and_visibility() -> None:
    src = "#[cfg(test)]\n#[allow(dead_code)]\npub(crate) mod helpers {\n}\n"
    assert cfg_test_regions(src) == [(1, 4)]


def test_ignores_out_of_line_modules_and_other_cfgs() -> None:
    src = '#[cfg(test)]\nmod tests;\n#[cfg(feature = "x")]\nmod x {\n}\n'
    assert cfg_test_regions(src) == []


def test_several_modules_and_unterminated_input() -> None:
    src = "#[cfg(test)]\nmod a {\n}\n#[cfg(test)]\nmod b {\n  fn f() {\n"
    assert cfg_test_regions(src) == [(1, 3), (4, 6)]


def test_strip_keeps_line_structure() -> None:
    src = 'let s = "a\nb"; // c\n/* d\ne */ x'
    stripped = strip_comments_and_literals(src)
    assert stripped.count("\n") == src.count("\n")
    assert "x" in stripped
    assert "c" not in stripped
    assert "a" not in stripped.replace("let", "")


def test_unterminated_literals_are_blanked_to_the_end() -> None:
    assert strip_comments_and_literals('x "open').strip() == "x"
    assert strip_comments_and_literals("x /* open").strip() == "x"
    assert strip_comments_and_literals('x r#"open').strip() == "x"


def test_in_regions() -> None:
    assert in_regions(5, [(1, 3), (5, 9)])
    assert not in_regions(4, [(1, 3), (5, 9)])

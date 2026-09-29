from __future__ import annotations

import pytest

from cargorewind.rustlex import Token, TokenKind, string_value, tokenize

L, ID, P, T = TokenKind.LITERAL, TokenKind.IDENT, TokenKind.PUNCT, TokenKind.LIFETIME


def kinds(src: str) -> list[tuple[TokenKind, str]]:
    return [(t.kind, t.text) for t in tokenize(src)]


@pytest.mark.parametrize(
    ("src", "expected"),
    [
        # Strings: escapes, embedded braces and quotes, multi-line.
        ('"a } \\" {"', [(L, '"a } \\" {"')]),
        ('"back\\\\"}', [(L, '"back\\\\"'), (P, "}")]),
        # Raw strings with and without hashes; a quote-hash inside a longer fence.
        ('r"}"', [(L, 'r"}"')]),
        ('r#"say "hi" }"#', [(L, 'r#"say "hi" }"#')]),
        ('r##"a "# b"##;', [(L, 'r##"a "# b"##'), (P, ";")]),
        # Byte strings, raw byte strings, C strings and raw C strings.
        ('b"{"', [(L, 'b"{"')]),
        ('br#"}"#', [(L, 'br#"}"#')]),
        ('c"}"', [(L, 'c"}"')]),
        ('cr"{"', [(L, 'cr"{"')]),
        # Char and byte literals, including escapes and quote characters.
        ("'}'", [(L, "'}'")]),
        ("'\\''", [(L, "'\\''")]),
        ("'\\\\'", [(L, "'\\\\'")]),
        ("'\"'", [(L, "'\"'")]),
        ("'\\u{7D}'", [(L, "'\\u{7D}'")]),
        ("'\\x7b'", [(L, "'\\x7b'")]),
        ("b'{'", [(L, "b'{'")]),
        ("b'\\''", [(L, "b'\\''")]),
        ("'\u00e9'", [(L, "'\u00e9'")]),
        # Lifetimes and labels are not char literals.
        ("&'a str", [(P, "&"), (T, "'a"), (ID, "str")]),
        ("'static", [(T, "'static")]),
        ("<'a, 'b>", [(P, "<"), (T, "'a"), (P, ","), (T, "'b"), (P, ">")]),
        ("'outer: loop {}", [(T, "'outer"), (P, ":"), (ID, "loop"), (P, "{"), (P, "}")]),
        ("'_", [(T, "'_")]),
        # Raw identifiers are never keywords; plain prefixes stay identifiers.
        ("r#mod", [(ID, "r#mod")]),
        ("r#type r", [(ID, "r#type"), (ID, "r")]),
        ("br cr b c", [(ID, "br"), (ID, "cr"), (ID, "b"), (ID, "c")]),
        ("b'ab", [(ID, "b"), (T, "'ab")]),
        # Numbers with suffixes; a stray quote is punctuation.
        ("1_000u32 0xFF", [(L, "1_000u32"), (L, "0xFF")]),
        ("' ", [(P, "'")]),
    ],
)
def test_literals_lifetimes_and_identifiers(
    src: str, expected: list[tuple[TokenKind, str]]
) -> None:
    assert kinds(src) == expected


def test_comments_are_skipped_including_nested_block_comments() -> None:
    src = (
        "a // line } comment\n"
        "/// doc { comment\n"
        "b /* outer { /* inner } */ still } */ c\n"
        "/**/ d /*/ e */ f\n"
    )
    assert [t.text for t in tokenize(src)] == ["a", "b", "c", "d", "f"]


def test_lines_survive_multi_line_literals_and_comments() -> None:
    src = 'let s = "one\ntwo";\n/* x\ny */ z\r\nr#"a\nb"# w'
    lines = {t.text: t.line for t in tokenize(src)}
    assert lines["let"] == 1
    assert lines[";"] == 2
    assert lines["z"] == 4
    assert lines['r#"a\nb"#'] == 5
    assert lines["w"] == 6


@pytest.mark.parametrize(
    ("src", "last"),
    [
        ('x "open', '"open'),
        ("x /* open", "x"),
        ('x r#"open', 'r#"open'),
        ("x '\\n", "'\\n"),
    ],
)
def test_unterminated_input_runs_to_the_end(src: str, last: str) -> None:
    assert tokenize(src)[-1].text == last


def test_shebang_and_bom_are_skipped_but_inner_attributes_are_not() -> None:
    assert [t.text for t in tokenize("#!/usr/bin/env run\nfn")] == ["fn"]
    assert [t.text for t in tokenize("\ufefffn")] == ["fn"]
    assert [t.text for t in tokenize("#![cfg(test)]")][:3] == ["#", "!", "["]


def test_token_helpers() -> None:
    brace = Token(P, "{", 1)
    assert brace.is_punct("{}") and not brace.is_punct("()")
    word = Token(ID, "mod", 1)
    assert word.is_ident() and word.is_ident("mod", "fn") and not word.is_ident("fn")


@pytest.mark.parametrize(
    ("literal", "value"),
    [
        ('"tests.rs"', "tests.rs"),
        ('"a\\"b"', 'a"b'),
        ('r"x/y.rs"', "x/y.rs"),
        ('r#"x"y"#', 'x"y'),
        ("'c'", None),
        ('b"x"', None),
    ],
)
def test_string_value(literal: str, value: str | None) -> None:
    assert string_value(literal) == value

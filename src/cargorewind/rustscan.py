"""Locate ``#[cfg(test)] mod name { ... }`` regions inside a Rust source file.

Rust keeps unit tests in the same file as the code they test, so a fix commit often
touches both in one file. The scan first blanks comments and string/char literals
(keeping every newline, so line numbers survive), then matches the attribute and the
module header, then follows braces to the closing one.
"""

from __future__ import annotations

import re

_CHAR_LITERAL = re.compile(r"'(?:\\u\{[0-9a-fA-F]{1,6}\}|\\x[0-9a-fA-F]{2}|\\.|[^\\'\n])'")
_RAW_STRING = re.compile(r'b?r(#*)"')
_CFG_TEST_MOD = re.compile(
    r"#\s*\[\s*cfg\s*\(\s*test\s*\)\s*\]"  # the attribute
    r"(?:\s*#\s*\[[^\]]*\])*"  # further attributes, e.g. #[allow(...)]
    r"\s*(?:pub(?:\s*\([^)]*\))?\s+)?mod\s+[A-Za-z_][A-Za-z0-9_]*\s*\{"
)


def _blank(text: str) -> str:
    return "".join("\n" if ch == "\n" else " " for ch in text)


def _is_ident_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def _skip_block_comment(src: str, i: int) -> int:
    """Index just past the (possibly nested) block comment starting at ``i``."""
    depth = 0
    n = len(src)
    while i < n:
        if src.startswith("/*", i):
            depth += 1
            i += 2
        elif src.startswith("*/", i):
            depth -= 1
            i += 2
            if depth == 0:
                return i
        else:
            i += 1
    return n


def _skip_string(src: str, i: int) -> int:
    """Index just past the ordinary string literal whose opening quote is at ``i``."""
    i += 1
    n = len(src)
    while i < n:
        if src[i] == "\\":
            i += 2
        elif src[i] == '"':
            return i + 1
        else:
            i += 1
    return n


def strip_comments_and_literals(src: str) -> str:
    """Replace comments and literal contents with spaces; newlines are preserved."""
    out: list[str] = []
    i = 0
    n = len(src)
    while i < n:
        ch = src[i]
        prev = src[i - 1] if i else ""
        if src.startswith("//", i):
            end = src.find("\n", i)
            end = n if end == -1 else end
        elif src.startswith("/*", i):
            end = _skip_block_comment(src, i)
        elif ch in "br" and not _is_ident_char(prev) and (raw := _RAW_STRING.match(src, i)):
            closing = '"' + raw.group(1)
            found = src.find(closing, raw.end())
            end = n if found == -1 else found + len(closing)
        elif ch == '"':
            end = _skip_string(src, i)
        elif ch == "'" and (lit := _CHAR_LITERAL.match(src, i)):
            end = lit.end()
        else:
            out.append(ch)
            i += 1
            continue
        out.append(_blank(src[i:end]))
        i = end
    return "".join(out)


def _matching_brace(code: str, open_index: int) -> int:
    depth = 0
    for index in range(open_index, len(code)):
        if code[index] == "{":
            depth += 1
        elif code[index] == "}":
            depth -= 1
            if depth == 0:
                return index
    return len(code) - 1


def cfg_test_regions(src: str) -> list[tuple[int, int]]:
    """1-based inclusive line ranges of every ``#[cfg(test)]`` inline module."""
    code = strip_comments_and_literals(src)
    regions: list[tuple[int, int]] = []
    for match in _CFG_TEST_MOD.finditer(code):
        close = _matching_brace(code, match.end() - 1)
        start_line = code.count("\n", 0, match.start()) + 1
        end_line = code.count("\n", 0, close) + 1
        regions.append((start_line, end_line))
    return regions


def in_regions(line: int, regions: list[tuple[int, int]]) -> bool:
    return any(start <= line <= end for start, end in regions)

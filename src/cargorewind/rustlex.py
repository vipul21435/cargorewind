"""A small Rust lexer: just enough of the token grammar to find items and attributes.

It yields identifiers (raw identifiers keep their ``r#`` prefix, so ``r#mod`` is never
the ``mod`` keyword), lifetimes and labels, literals (strings, byte and C strings, raw
strings with any number of ``#``, char and byte literals, numbers) and one-character
punctuation. Line comments, doc comments and nested block comments are skipped. Every
token carries its 1-based line, so callers can map tokens back to diff lines.

The lexer never raises. Unterminated literals and comments run to the end of the input
(rustc reports an error there anyway), and a stray quote becomes punctuation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

_IDENT = re.compile(r"[^\W\d]\w*")
_NUMBER = re.compile(r"\d\w*")
_SPACE = re.compile(r"\s+")
_RAW_PREFIXES = frozenset({"r", "br", "cr"})
_QUOTED_PREFIXES = frozenset({"b", "c"})


class TokenKind(StrEnum):
    IDENT = "ident"
    LIFETIME = "lifetime"
    LITERAL = "literal"
    PUNCT = "punct"


@dataclass(frozen=True, slots=True)
class Token:
    kind: TokenKind
    text: str
    line: int

    def is_punct(self, chars: str) -> bool:
        """True for a punctuation token that is one of ``chars``."""
        return self.kind is TokenKind.PUNCT and self.text in chars

    def is_ident(self, *words: str) -> bool:
        return self.kind is TokenKind.IDENT and (not words or self.text in words)


def _block_comment_end(src: str, i: int) -> int:
    """Index just past the (possibly nested) block comment that starts at ``i``."""
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


def _string_end(src: str, quote: int) -> int:
    """Index just past the escaped string literal whose opening quote is at ``quote``."""
    i = quote + 1
    n = len(src)
    while i < n:
        ch = src[i]
        if ch == "\\":
            i += 2
        elif ch == '"':
            return i + 1
        else:
            i += 1
    return n


def _raw_string_end(src: str, start: int) -> int | None:
    """End of a raw string whose ``#``s or quote start at ``start``; None if not one."""
    hashes = 0
    n = len(src)
    while start + hashes < n and src[start + hashes] == "#":
        hashes += 1
    body = start + hashes
    if body >= n or src[body] != '"':
        return None
    closing = '"' + "#" * hashes
    found = src.find(closing, body + 1)
    return n if found == -1 else found + len(closing)


def _char_end(src: str, quote: int) -> int | None:
    """End of the char literal whose opening quote is at ``quote``; None if not one."""
    n = len(src)
    if quote + 1 < n and src[quote + 1] == "\\":
        # Escaped: '\n', '\'', '\\', '\x7f', '\u{1F600}'. Skip the escaped char, then
        # stop at the closing quote (or, unterminated, at the end of the line).
        i = quote + 3
        while i < n and src[i] not in "'\n":
            i += 1
        return i + 1 if i < n and src[i] == "'" else i
    if quote + 2 < n and src[quote + 2] == "'" and src[quote + 1] != "\n":
        return quote + 3
    return None


def _trivia_end(src: str, i: int) -> int:
    """End of the whitespace or comment at ``i``; ``i`` itself when there is none."""
    if space := _SPACE.match(src, i):
        return space.end()
    if src.startswith("//", i):
        newline = src.find("\n", i)
        return len(src) if newline == -1 else newline
    if src.startswith("/*", i):
        return _block_comment_end(src, i)
    return i


def _word_token(src: str, i: int, end: int) -> tuple[TokenKind, int]:
    """An identifier at ``i..end``, or the literal it prefixes (``r#"..."#``, ``b'x'``)."""
    text = src[i:end]
    nxt = src[end] if end < len(src) else ""
    if text in _RAW_PREFIXES and nxt in ('"', "#"):
        raw_end = _raw_string_end(src, end)
        if raw_end is not None:
            return TokenKind.LITERAL, raw_end
        if text == "r" and (ident := _IDENT.match(src, end + 1)):
            return TokenKind.IDENT, ident.end()  # raw identifier r#name
    if text in _QUOTED_PREFIXES and nxt == '"':
        return TokenKind.LITERAL, _string_end(src, end)
    if text == "b" and nxt == "'" and (byte_end := _char_end(src, end)) is not None:
        return TokenKind.LITERAL, byte_end
    return TokenKind.IDENT, end


def _token_at(src: str, i: int) -> tuple[TokenKind, int]:
    """Kind and end index of the token starting at ``i``."""
    if word := _IDENT.match(src, i):
        return _word_token(src, i, word.end())
    if number := _NUMBER.match(src, i):
        return TokenKind.LITERAL, number.end()
    ch = src[i]
    if ch == '"':
        return TokenKind.LITERAL, _string_end(src, i)
    if ch == "'":
        char_end = _char_end(src, i)
        if char_end is not None:
            return TokenKind.LITERAL, char_end
        if label := _IDENT.match(src, i + 1):
            return TokenKind.LIFETIME, label.end()
    return TokenKind.PUNCT, i + 1


def _start(src: str) -> int:
    """Index of the first token: past a byte order mark and a shebang line."""
    i = 1 if src.startswith("\ufeff") else 0
    if src.startswith("#!", i) and not src[i + 2 :].lstrip().startswith("["):
        newline = src.find("\n", i)
        return len(src) if newline == -1 else newline
    return i


def tokenize(src: str) -> list[Token]:
    """Tokens of ``src`` in order; comments and whitespace are dropped."""
    tokens: list[Token] = []
    i = _start(src)
    line = 1
    while i < len(src):
        end = _trivia_end(src, i)
        if end == i:
            kind, end = _token_at(src, i)
            tokens.append(Token(kind, src[i:end], line))
        line += src.count("\n", i, end)
        i = end
    return tokens


def string_value(literal: str) -> str | None:
    """Value of a plain or raw string literal token (``"a"``, ``r#"a"#``), else None."""
    if literal.startswith('"') and literal.endswith('"') and len(literal) >= 2:
        return re.sub(r"\\(.)", r"\1", literal[1:-1])
    if literal.startswith("r") and (raw := re.fullmatch(r'r(#*)"(.*)"\1', literal, re.S)):
        return raw.group(2)
    return None

"""Find test-only code inside a Rust source file.

Rust keeps unit tests next to the code they test, so a fix commit often touches both in
one file. This scanner works on the tokens of ``rustlex`` (so braces in comments,
strings, raw strings and char literals never count) and reports:

- regions compiled only under a test configuration: inline ``#[cfg(test)] mod name
  { ... }`` modules, out-of-line ``#[cfg(test)] mod name;`` declarations, single items
  or statements carrying the attribute, blocks with an inner ``#![cfg(test)]``, and the
  whole file when it starts with ``#![cfg(test)]``;
- every out-of-line ``mod name;`` declaration, with its enclosing inline modules and any
  ``#[path = "..."]``, so the caller can find the module file and know whether that
  whole file is test code.

A cfg predicate counts as test-only when it implies ``test`` (or ``doctest``):
``test``, ``all(test, ...)``, ``any(test, doctest)``. ``not(test)`` and
``any(test, feature = "x")`` do not, because that code also builds outside tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from cargorewind.rustlex import Token, TokenKind, string_value, tokenize

TEST_ATOMS = frozenset({"test", "doctest"})

# Items that end at a top-level ``;`` or at the ``}`` that closes their body. Anything
# else under an attribute (a field, variant, match arm or expression) also ends at a
# top-level comma.
ITEM_KEYWORDS = frozenset(
    {
        "async",
        "const",
        "crate",
        "enum",
        "extern",
        "fn",
        "impl",
        "let",
        "macro_rules",
        "mod",
        "static",
        "struct",
        "trait",
        "type",
        "union",
        "unsafe",
        "use",
    }
)
_NAMED_ITEMS = frozenset(
    {"const", "enum", "fn", "macro_rules", "mod", "static", "struct", "trait", "type", "union"}
)
_CONTINUES_AFTER_BRACE = frozenset(".?,;=|&+-*/%<>^")


@dataclass(frozen=True)
class Cfg:
    """A parsed ``cfg`` predicate: an option (``test``, ``feature = "x"``) or a combinator."""

    name: str
    value: str | None = None
    args: tuple[Cfg, ...] | None = None

    def render(self) -> str:
        if self.args is not None:
            return f"{self.name}({', '.join(arg.render() for arg in self.args)})"
        return self.name if self.value is None else f"{self.name} = {self.value}"

    def test_only(self) -> bool:
        """True when code under this predicate only builds in a test configuration."""
        if self.args is None:
            return self.value is None and self.name in TEST_ATOMS
        if self.name == "all":
            return any(arg.test_only() for arg in self.args)
        if self.name == "any":
            return bool(self.args) and all(arg.test_only() for arg in self.args)
        return False


@dataclass(frozen=True)
class TestRegion:
    """1-based inclusive lines that only build under a test configuration."""

    start_line: int
    end_line: int
    kind: str  # module, module-decl, item, block, file
    name: str
    cfg: str


@dataclass(frozen=True)
class ModuleDecl:
    """An out-of-line ``mod name;`` declaration."""

    name: str
    line: int
    inline_path: tuple[str, ...]
    path_attr: str | None
    cfg_test: bool


@dataclass
class FileScan:
    regions: list[TestRegion] = field(default_factory=list)
    modules: list[ModuleDecl] = field(default_factory=list)
    file_cfg_test: bool = False

    @property
    def spans(self) -> list[tuple[int, int]]:
        return [(r.start_line, r.end_line) for r in self.regions]


@dataclass
class _Attr:
    line: int
    cfg: Cfg | None = None
    path: str | None = None


@dataclass
class _Frame:
    line: int
    module: str | None = None
    cfg: Cfg | None = None  # outer #[cfg(test)] on an inline module
    inner_cfg: Cfg | None = None  # inner #![cfg(test)] inside the braces
    region_line: int = 0


def _parse_cfg(tokens: list[Token], i: int) -> tuple[Cfg, int]:
    """Parse one predicate starting at ``tokens[i]``; returns it and the next index."""
    if i >= len(tokens) or tokens[i].kind is not TokenKind.IDENT:
        return Cfg("?"), i + 1
    name = tokens[i].text
    i += 1
    if i < len(tokens) and tokens[i].is_punct("="):
        value = tokens[i + 1].text if i + 1 < len(tokens) else ""
        return Cfg(name, value=value), i + 2
    if i < len(tokens) and tokens[i].is_punct("("):
        args: list[Cfg] = []
        i += 1
        while i < len(tokens) and not tokens[i].is_punct(")"):
            if tokens[i].is_punct(","):
                i += 1
                continue
            arg, i = _parse_cfg(tokens, i)
            args.append(arg)
        return Cfg(name, args=tuple(args)), i + 1
    return Cfg(name), i


def _read_attr(tokens: list[Token], i: int) -> tuple[_Attr, int]:
    """Parse ``#[...]`` or ``#![...]`` at ``i``; returns it and the index after ``]``."""
    attr = _Attr(tokens[i].line)
    start = i + (3 if tokens[i + 1].is_punct("!") else 2)
    depth = 1
    j = start
    while j < len(tokens) and depth:
        if tokens[j].is_punct("["):
            depth += 1
        elif tokens[j].is_punct("]"):
            depth -= 1
        j += 1
    body = tokens[start : j - 1]
    if len(body) >= 2 and body[0].is_ident("cfg") and body[1].is_punct("("):
        attr.cfg, _ = _parse_cfg(body, 0)
        attr.cfg = attr.cfg.args[0] if attr.cfg.args and len(attr.cfg.args) == 1 else None
    elif len(body) == 3 and body[0].is_ident("path") and body[1].is_punct("="):
        attr.path = string_value(body[2].text)
    return attr, j


def _is_attr_start(tokens: list[Token], i: int) -> bool:
    if not tokens[i].is_punct("#") or i + 1 >= len(tokens):
        return False
    nxt = tokens[i + 1]
    if nxt.is_punct("["):
        return True
    return nxt.is_punct("!") and i + 2 < len(tokens) and tokens[i + 2].is_punct("[")


def _skip_visibility(tokens: list[Token], i: int) -> int:
    if i < len(tokens) and tokens[i].is_ident("pub"):
        i += 1
        if i < len(tokens) and tokens[i].is_punct("("):
            depth = 0
            while i < len(tokens):
                if tokens[i].is_punct("("):
                    depth += 1
                elif tokens[i].is_punct(")"):
                    depth -= 1
                    if depth == 0:
                        return i + 1
                i += 1
    return i


def _item_mode(tokens: list[Token], first: int) -> bool:
    head = tokens[first]
    if head.kind is not TokenKind.IDENT:
        return False
    if head.text in ITEM_KEYWORDS:
        return True
    return first + 1 < len(tokens) and tokens[first + 1].is_punct("!")  # macro call


def _item_end(tokens: list[Token], start: int) -> int:
    """Index of the last token of the item, statement or field starting at ``start``."""
    first = _skip_visibility(tokens, start)
    if first >= len(tokens):
        return len(tokens) - 1
    item = _item_mode(tokens, first)
    block = tokens[first].is_punct("{")
    depth = 0
    for k in range(start, len(tokens)):
        tok = tokens[k]
        if tok.kind is not TokenKind.PUNCT:
            continue
        if tok.text in "([{":
            depth += 1
        elif tok.text in ")]}":
            depth -= 1
            if depth < 0:
                return max(k - 1, start)
            if depth == 0 and tok.text == "}":
                if item or block:
                    return k
                nxt = tokens[k + 1] if k + 1 < len(tokens) else None
                continues = nxt is not None and (
                    (nxt.kind is TokenKind.PUNCT and nxt.text in _CONTINUES_AFTER_BRACE)
                    or nxt.is_ident("as", "else")
                )
                if not continues:
                    return k
        elif depth == 0 and (tok.text == ";" or (tok.text == "," and not item)):
            return k
    return len(tokens) - 1


def _item_label(tokens: list[Token], start: int) -> tuple[str, str]:
    """(kind, name) for the report: ``("item", "fn helper")``, ``("block", "")``."""
    first = _skip_visibility(tokens, start)
    while first < len(tokens) and tokens[first].is_ident("async", "unsafe", "extern"):
        first += 1
        if first < len(tokens) and tokens[first].kind is TokenKind.LITERAL:
            first += 1  # extern "C"
    if first >= len(tokens):
        return "item", ""
    head = tokens[first]
    if head.is_punct("{"):
        return "block", ""
    if head.is_ident(*_NAMED_ITEMS) and first + 1 < len(tokens):
        nxt = tokens[first + 1]
        if nxt.is_punct("!") and first + 2 < len(tokens):
            nxt = tokens[first + 2]  # macro_rules! name
        if nxt.kind is TokenKind.IDENT:
            return "item", f"{head.text} {nxt.text}"
    return "item", head.text


def _mod_at(tokens: list[Token], i: int) -> tuple[str, str] | None:
    """(name, "{" or ";") when ``tokens[i]`` starts ``mod name {`` or ``mod name;``."""
    if (
        i + 2 < len(tokens)
        and tokens[i].is_ident("mod")
        and tokens[i + 1].kind is TokenKind.IDENT
        and tokens[i + 2].is_punct("{;")
    ):
        return tokens[i + 1].text.removeprefix("r#"), tokens[i + 2].text
    return None


def _first_test_cfg(attrs: list[_Attr]) -> Cfg | None:
    return next((a.cfg for a in attrs if a.cfg is not None and a.cfg.test_only()), None)


class _Scanner:
    def __init__(self, src: str) -> None:
        self.tokens = tokenize(src)
        self.last_line = src.count("\n") + (0 if src.endswith("\n") or not src else 1)
        self.scan = FileScan()
        self.frames: list[_Frame] = []
        self.pending: _Frame | None = None
        self.file_cfg: Cfg | None = None

    def _inline_path(self) -> tuple[str, ...]:
        return tuple(f.module for f in self.frames if f.module is not None)

    def _region(self, start: int, end: int, kind: str, name: str, cfg: Cfg) -> None:
        self.scan.regions.append(TestRegion(start, end, kind, name, cfg.render()))

    def _module(self, i: int, attrs: list[_Attr]) -> int | None:
        """Handle ``mod`` at ``i``; returns the next index, or None when it is not one."""
        found = _mod_at(self.tokens, i)
        if found is None:
            return None
        name, delimiter = found
        cfg = _first_test_cfg(attrs)
        start = attrs[0].line if attrs else self.tokens[i].line
        if delimiter == ";":
            path = next((a.path for a in reversed(attrs) if a.path is not None), None)
            decl_line = self.tokens[i].line
            self.scan.modules.append(ModuleDecl(name, decl_line, self._inline_path(), path, False))
            if cfg is not None:
                self._region(start, self.tokens[i + 2].line, "module-decl", name, cfg)
        else:
            self.pending = _Frame(self.tokens[i + 2].line, name, cfg, region_line=start)
        return i + 2

    def _attributes(self, i: int) -> int:
        attrs: list[_Attr] = []
        while i < len(self.tokens) and _is_attr_start(self.tokens, i):
            inner = self.tokens[i + 1].is_punct("!")
            attr, i = _read_attr(self.tokens, i)
            if not inner:
                attrs.append(attr)
            elif attr.cfg is not None and attr.cfg.test_only():
                if self.frames:
                    self.frames[-1].inner_cfg = attr.cfg
                elif self.file_cfg is None:
                    self.file_cfg = attr.cfg
        if not attrs or i >= len(self.tokens):
            return i
        start = _skip_visibility(self.tokens, i)
        after_mod = self._module(start, attrs)
        if after_mod is not None:
            return after_mod
        cfg = _first_test_cfg(attrs)
        if cfg is not None:
            end = _item_end(self.tokens, i)
            kind, name = _item_label(self.tokens, i)
            self._region(attrs[0].line, self.tokens[end].line, kind, name, cfg)
        return i  # keep scanning inside the item for nested modules

    def _close(self, frame: _Frame, line: int) -> None:
        if frame.cfg is not None:
            self._region(frame.region_line, line, "module", frame.module or "", frame.cfg)
        if frame.inner_cfg is not None:
            start = frame.region_line or frame.line
            kind = "module" if frame.module else "block"
            self._region(start, line, kind, frame.module or "", frame.inner_cfg)

    def run(self) -> FileScan:
        tokens = self.tokens
        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if _is_attr_start(tokens, i):
                i = self._attributes(i)
                continue
            after_mod = self._module(i, [])
            if after_mod is not None:
                i = after_mod
                continue
            if tok.is_punct("{"):
                frame = self.pending or _Frame(tok.line)
                if frame.region_line == 0:
                    frame.region_line = tok.line
                self.frames.append(frame)
                self.pending = None
            elif tok.is_punct("}") and self.frames:
                self._close(self.frames.pop(), tok.line)
            i += 1
        while self.frames:
            self._close(self.frames.pop(), self.last_line)
        if self.file_cfg is not None:
            self.scan.file_cfg_test = True
            self._region(1, max(self.last_line, 1), "file", "", self.file_cfg)
        self.scan.regions.sort(key=lambda r: (r.start_line, -r.end_line))
        spans = self.scan.spans
        self.scan.modules = [
            ModuleDecl(
                m.name,
                m.line,
                m.inline_path,
                m.path_attr,
                self.scan.file_cfg_test or in_regions(m.line, spans),
            )
            for m in self.scan.modules
        ]
        return self.scan


def scan_source(src: str) -> FileScan:
    """Test-only regions and out-of-line module declarations of one source file."""
    return _Scanner(src).run()


def cfg_test_regions(src: str) -> list[tuple[int, int]]:
    """1-based inclusive line ranges that only build under a test configuration."""
    return scan_source(src).spans


def in_regions(line: int, regions: list[tuple[int, int]]) -> bool:
    return any(start <= line <= end for start, end in regions)

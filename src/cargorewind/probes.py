"""Sanity probes: prove that the fix is absent at the base commit and present after it.

A probe is an identifier that the patches define on an added line: a ``fn``, ``struct``,
``enum``, ``trait``, ``const`` or ``macro_rules!`` name, found with the Rust lexer, so
names inside comments, strings and doc examples do not count. Only names that occur
nowhere in the base commit's ``*.rs`` files as a whole word (``git grep -w``) are kept:
for those, a plain ``grep -w`` inside a container is exact. The others are reported as
skipped with the reason.

The probes are checked three times:

* on the host, against the exact files each stage receives: absent at base, the test
  patch's names defined in the before tree while the fix's names are not, and every
  name defined in the after tree;
* in the image build: a ``RUN grep`` step fails the build when any probe word exists in
  the checkout the image was built from (a wrong build context or commit);
* in the before and after runs: the stage script greps the checkout after unpacking the
  overlay and exits with ``PROBE_EXIT_CODE`` before cargo runs when a name is missing.

``git apply --check`` of test.patch at base and of fix.patch on top of it (from the
split) complete the probe report.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from cargorewind import __version__
from cargorewind.backend import Overlay
from cargorewind.dockerfile import PROBE_EXIT_CODE, PROBE_GLOB, PROBE_MARKER
from cargorewind.patchsplit import FileDiff, SplitResult
from cargorewind.rustlex import Token, TokenKind, tokenize

PROBE_SCHEMA = 1
PROBE_KINDS = ("fn", "struct", "enum", "trait", "const", "macro_rules")
MAX_PROBES = 16
__all__ = ["PROBE_EXIT_CODE", "PROBE_MARKER", "ProbeReport", "plan_probes"]

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# (words) -> {word: files of the base commit that contain it}
WordSearch = Callable[[list[str]], Mapping[str, list[str]]]


@dataclass(frozen=True)
class Definition:
    kind: str
    name: str
    line: int


def _token(tokens: list[Token], i: int) -> Token | None:
    return tokens[i] if 0 <= i < len(tokens) else None


def definitions(source: str) -> list[Definition]:
    """Item definitions of the probe kinds in Rust ``source``, with their lines.

    ``const`` counts only as ``const NAME:`` outside generic parameter lists (so neither
    ``const fn`` nor ``<const N: usize>`` nor ``*const T``). The lexer emits ``::`` as two
    ``:`` tokens, so a raw pointer to a path (``*const std::ffi::c_void``) is told apart
    by the ``*`` before it and by the second ``:``. Raw identifiers, ``_`` and non-ASCII
    names are left out, because a word search cannot match them reliably.
    """
    tokens = tokenize(source)
    found: list[Definition] = []

    def add(kind: str, name: Token | None) -> None:
        if name is None or name.kind is not TokenKind.IDENT or name.text == "_":
            return
        if _IDENTIFIER.fullmatch(name.text):
            found.append(Definition(kind, name.text, name.line))

    for i, tok in enumerate(tokens):
        if tok.kind is not TokenKind.IDENT:
            continue
        nxt, after, prev = _token(tokens, i + 1), _token(tokens, i + 2), _token(tokens, i - 1)
        if tok.text in ("fn", "struct", "enum", "trait"):
            add(tok.text, nxt)
        elif tok.text == "const":
            generic = prev is not None and prev.is_punct("<,")
            pointer = prev is not None and prev.is_punct("*")
            path = (third := _token(tokens, i + 3)) is not None and third.is_punct(":")
            if not (generic or pointer or path) and after is not None and after.is_punct(":"):
                add("const", nxt)
        elif tok.text == "macro_rules" and nxt is not None and nxt.is_punct("!"):
            add("macro_rules", after)
    return found


def added_lines(diff: FileDiff) -> set[int]:
    """New-side line numbers of the ``+`` lines of ``diff``."""
    lines: set[int] = set()
    for hunk in diff.hunks:
        number = hunk.new_start
        for line in hunk.lines:
            tag = line[:1]
            if tag == "+":
                lines.add(number)
            if tag in "+ ":
                number += 1
    return lines


@dataclass(frozen=True)
class Probe:
    kind: str
    name: str
    patch: str  # "test" or "fix": the patch whose added line defines it
    path: str
    line: int

    @property
    def label(self) -> str:
        return (
            f"macro_rules! {self.name}"
            if self.kind == "macro_rules"
            else f"{self.kind} {self.name}"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "patch": self.patch,
            "path": self.path,
            "line": self.line,
        }


@dataclass(frozen=True)
class Skipped:
    probe: Probe
    reason: str


@dataclass(frozen=True)
class HostCheck:
    stage: str
    expectation: str
    ok: bool
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "expectation": self.expectation,
            "ok": self.ok,
            "detail": self.detail,
        }


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace")


def candidates(files: list[FileDiff], patch: str, contents: Mapping[str, bytes]) -> list[Probe]:
    """Definitions on the added lines of ``files``; ``contents`` holds each new file."""
    found: list[Probe] = []
    for diff in files:
        data = contents.get(diff.path)
        if diff.is_deleted or not diff.path.endswith(".rs") or data is None:
            continue
        added = added_lines(diff)
        for item in definitions(_decode(data)):
            if item.line in added:
                found.append(Probe(item.kind, item.name, patch, diff.path, item.line))
    return found


def _defined(overlay: Overlay) -> dict[str, set[str]]:
    """Names defined in each ``*.rs`` file of ``overlay``."""
    return {
        path: {d.name for d in definitions(_decode(data))}
        for path, data in overlay.files.items()
        if path.endswith(".rs")
    }


@dataclass
class ProbeReport:
    probes: list[Probe] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)
    checks: list[HostCheck] = field(default_factory=list)
    patch_checks: dict[str, bool | None] = field(default_factory=dict)
    container: dict[str, str] = field(default_factory=dict)  # build/before/after -> status

    @property
    def words(self) -> tuple[str, ...]:
        return tuple(p.name for p in self.probes)

    def required(self, stage: str) -> tuple[str, ...]:
        """The words a stage's checkout must contain: the test patch's from ``before``."""
        if stage == "after":
            return self.words
        if stage == "before":
            return tuple(p.name for p in self.probes if p.patch == "test")
        return ()

    def record_stage(self, stage: str, output: str) -> None:
        if self.required(stage):
            self.container[stage] = "failed" if PROBE_MARKER in output else "passed"

    @property
    def ok(self) -> bool:
        applied = all(v is not False for v in self.patch_checks.values())
        failed = any(status == "failed" for status in self.container.values())
        return applied and all(c.ok for c in self.checks) and not failed

    def lines(self) -> list[str]:
        """What the probes are and what the host checks found, one line each."""
        lines = []
        for probe in self.probes:
            after = "defined before and after" if probe.patch == "test" else "defined after only"
            lines.append(
                f"probe     {probe.patch} {probe.label} ({probe.path}:{probe.line}): "
                f"absent at base, {after}"
            )
        for skip in self.skipped:
            lines.append(f"probe     skipped {skip.probe.label}: {skip.reason}")
        if not self.probes:
            lines.append("probe     no new identifier to probe; the flip is the only evidence")
            return lines
        failed = [c for c in self.checks if not c.ok]
        verdict = "every host check passed" if not failed else f"{len(failed)} host check(s) FAILED"
        lines.append(
            f"probe     {len(self.probes)} identifier(s), {verdict}; the image build and the "
            "before and after runs grep their checkouts too"
        )
        lines += [f"probe     FAILED {c.stage}: {c.expectation} ({c.detail})" for c in failed]
        return lines

    def document(self, source: str, base: str, fix: str) -> dict[str, Any]:
        """The probes.json document."""
        return {
            "schema_version": PROBE_SCHEMA,
            "generator": f"cargorewind {__version__}",
            "repo": source,
            "base_commit": base,
            "fix_commit": fix,
            "kinds": list(PROBE_KINDS),
            "word_search": f"grep -w over {PROBE_GLOB} files",
            "identifiers": [p.as_dict() for p in self.probes],
            "skipped": [{**s.probe.as_dict(), "reason": s.reason} for s in self.skipped],
            "patch_checks": self.patch_checks,
            "host_checks": [c.as_dict() for c in self.checks],
            "container_checks": self.container,
            "ok": self.ok,
        }


def _choose(
    found: list[Probe], occurrences: Mapping[str, list[str]], limit: int
) -> tuple[list[Probe], list[Skipped]]:
    chosen: dict[str, Probe] = {}
    skipped: list[Skipped] = []
    for probe in found:  # test candidates come first, so a shared name is a test probe
        first = chosen.get(probe.name)
        if first is not None:
            where = f"{first.patch}.patch ({first.path}:{first.line})"
            skipped.append(Skipped(probe, f"also defined by {where}"))
        elif probe.name in occurrences:
            files = occurrences[probe.name]
            shown = ", ".join(files[:3]) + (f" and {len(files) - 3} more" if len(files) > 3 else "")
            reason = f"occurs at base in {shown}; a word search cannot prove it absent"
            skipped.append(Skipped(probe, reason))
        else:
            chosen[probe.name] = probe
    # The fix's own names prove the most, so they are kept first when there are many.
    ordered = sorted(chosen.values(), key=lambda p: (p.patch != "fix", p.path, p.line))
    for probe in ordered[limit:]:
        skipped.append(Skipped(probe, f"over the limit of {limit} probes"))
    return ordered[:limit], skipped


def plan_probes(
    split: SplitResult,
    overlays: Mapping[str, Overlay],
    base_words: WordSearch,
    patch_checks: Mapping[str, bool | None] | None = None,
    limit: int = MAX_PROBES,
) -> ProbeReport:
    """Choose the probes of a split and run the host checks on the stage overlays."""
    before, after = overlays["before"], overlays["after"]
    found = candidates(split.test_files, "test", before.files)
    found += candidates(split.fix_files, "fix", after.files)
    occurrences = base_words(sorted({p.name for p in found}))
    probes, skipped = _choose(found, occurrences, limit)
    report = ProbeReport(probes, skipped, patch_checks=dict(patch_checks or {}))
    if probes:
        report.checks = host_checks(probes, before, after, occurrences)
    return report


def host_checks(
    probes: list[Probe], before: Overlay, after: Overlay, occurrences: Mapping[str, list[str]]
) -> list[HostCheck]:
    """Absent at base, test names in the before tree only, every name in the after tree."""
    at_base = sorted(p.name for p in probes if p.name in occurrences)
    before_defs, after_defs = _defined(before), _defined(after)
    in_before = set().union(*before_defs.values()) if before_defs else set()
    test_missing = sorted(
        p.name for p in probes if p.patch == "test" and p.name not in before_defs.get(p.path, set())
    )
    fix_early = sorted(p.name for p in probes if p.patch == "fix" and p.name in in_before)
    after_missing = sorted(p.name for p in probes if p.name not in after_defs.get(p.path, set()))
    word_search = f"git grep -w over the base commit's {PROBE_GLOB} files"
    return [
        HostCheck("base", "no probe identifier occurs at base", not at_base, word_search),
        HostCheck(
            "before",
            "test.patch identifiers defined",
            not test_missing,
            ", ".join(test_missing) or "checked in the before overlay",
        ),
        HostCheck(
            "before",
            "fix.patch identifiers not yet defined",
            not fix_early,
            ", ".join(fix_early) or "checked in the before overlay",
        ),
        HostCheck(
            "after",
            "every probe identifier defined",
            not after_missing,
            ", ".join(after_missing) or "checked in the after overlay",
        ),
    ]

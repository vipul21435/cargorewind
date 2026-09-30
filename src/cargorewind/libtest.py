"""Parse ``cargo test`` output: libtest's text format and its JSON format.

The text format is what every toolchain prints. One run holds several test binaries,
each announced by cargo (``Running target/debug/deps/demo-<hash>`` before cargo 1.5x,
``Running unittests src/lib.rs (target/debug/deps/demo-<hash>)`` and
``Running tests/it.rs (...)`` since, ``Doc-tests demo`` for doctests), then
``running N tests``, one ``test <name> ... <status>`` line per test and a
``test result:`` summary. Output of the tests themselves can land in between: with
``--nocapture`` or ``--test-threads=1`` libtest prints ``test <name> ... `` before the
test runs and the status after it, and a panic in a spawned thread (stderr) can split a
result line in two. A result line whose status is missing stays pending until a line
that is only a status.

The JSON format (``-- -Z unstable-options --format json``, nightly toolchains) has one
event object per line; cargo's ``Running`` lines and stray stderr output stay text, so
one parser reads both, line by line.

Names: the mode suffix libtest adds for display (`` - should panic``,
`` - compile fail``, `` - compile``) is not part of the name. Doctest names embed a line
number (``src/lib.rs - f (line 12)``) that shifts whenever a patch adds lines above it,
so the stable name replaces it with an ordinal per item and binary (``src/lib.rs - f``,
then ``src/lib.rs - f #2``; ``src/lib.rs - (crate)`` for the crate's own docs, which old
rustdoc prints as ``src/lib.rs -  (line 1)`` and newer as ``src/lib.rs - (line 1)``); the
raw name is kept for reruns.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Status(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    IGNORED = "ignored"
    COMPILE_ERROR = "compile-error"  # the run did not build, so no test ran
    TIMEOUT = "timeout"  # the run was stopped before the test reported
    MISSING = "missing"  # the run finished without reporting the test


# Statuses that say nothing about the test itself, only about the run.
NOT_RUN = frozenset({Status.COMPILE_ERROR, Status.TIMEOUT, Status.MISSING})
_RANK = {Status.FAILED: 2, Status.PASSED: 1, Status.IGNORED: 0}

# cargo right-aligns its status words, so its lines start with spaces; a test printing
# "Running ..." at the start of a line is not mistaken for a new binary.
_RUNNING = re.compile(r"^\s+Running (?P<rest>\S.*?)\s*$")
_RUNNING_NEW = re.compile(r"^(?P<unit>unittests\b\s*)?(?P<path>.*?)\s*\((?P<binary>[^()]+)\)$")
_DOC_TESTS = re.compile(r"^\s+Doc-tests (?P<crate>\S+)\s*$")
_RUNNING_N = re.compile(r"^running (?P<count>\d+) tests?$")
_SUMMARY = re.compile(
    r"^test result: (?P<verdict>ok|FAILED)\. (?P<passed>\d+) passed; (?P<failed>\d+) failed;"
    r"(?: (?P<allowed_fail>\d+) allowed to fail;)? (?P<ignored>\d+) ignored; "
    r"(?P<measured>\d+) measured; (?P<filtered_out>\d+) filtered out"
)
_NAME = r"(?P<name>\S+ - (?:.*? )?\(line \d+\)|\S+)"
_MODE = r"(?: - (?:should panic(?: with .*?)?|compile fail|compile))?"
_TEST = re.compile(rf"\btest {_NAME}{_MODE} \.\.\.(?: (?P<rest>.*))?$")
_STATUS = re.compile(r"(?P<status>ok|FAILED|ignored(?:, .*)?|bench: .*)(?: <[\d.]+s>)?")
# A doctest in the crate's own docs has no item: ``src/lib.rs - (line 8)``, or with the
# empty item still followed by its space, ``src/lib.rs -  (line 8)`` (rustdoc 1.39).
_DOCTEST = re.compile(r"^(?P<file>.+?) - (?:(?P<item>.*?) )?\(line (?P<line>\d+)\)$")
CRATE_DOCTEST = "(crate)"  # stands in for the missing item in the stable name
_HASH = re.compile(r"-[0-9a-f]{16}$")
_COMPILE_ERROR = re.compile(r"^error(?:\[E\d+\])?: |^error: could not compile", re.MULTILINE)


@dataclass(frozen=True)
class Suite:
    """One test binary of a run, as cargo announced it."""

    binary: str  # crate-style name: the binary without its hash, or the doctest crate
    doc: bool = False
    path: str | None = None  # source path newer cargo prints, relative to the package
    unittests: bool = False

    @property
    def label(self) -> str:
        if self.doc:
            return f"doctests of {self.binary}"
        return f"{self.path} ({self.binary})" if self.path else self.binary or "unknown binary"


UNKNOWN_SUITE = Suite("")


def crate_name(name: str) -> str:
    """The crate-style name cargo gives binaries and doctest crates (``-`` becomes ``_``)."""
    return name.replace("-", "_")


def suite_of(line: str) -> Suite | None:
    """The suite a ``Running ...`` or ``Doc-tests ...`` line announces, or None."""
    doc = _DOC_TESTS.match(line)
    if doc is not None:
        return Suite(crate_name(doc.group("crate")), doc=True)
    running = _RUNNING.match(line)
    if running is None:
        return None
    rest = running.group("rest")
    new = _RUNNING_NEW.match(rest)
    binary = new.group("binary") if new is not None else rest
    stem = _HASH.sub("", binary.rsplit("/", 1)[-1].removesuffix(".exe"))
    if new is None:
        return Suite(stem)
    return Suite(stem, path=new.group("path") or None, unittests=bool(new.group("unit")))


@dataclass(frozen=True)
class TestResult:
    __test__ = False  # not a pytest test class

    suite: Suite
    name: str  # stable name: doctest line numbers replaced by an ordinal
    raw: str  # the name libtest matches (doctests keep their line number)
    status: Status


@dataclass
class SuiteRun:
    suite: Suite
    planned: int | None = None  # "running N tests" or the JSON suite event
    summary: dict[str, int] | None = None  # "test result:" counts or the JSON suite event


@dataclass
class ParsedRun:
    results: list[TestResult] = field(default_factory=list)
    suites: list[SuiteRun] = field(default_factory=list)
    json_events: int = 0

    @property
    def started(self) -> bool:
        """At least one test binary started (so the run built)."""
        return bool(self.results) or any(s.planned is not None for s in self.suites)

    def by_name(self) -> dict[str, Status]:
        """Stable name -> status, ignoring which binary it came from (a failure wins)."""
        found: dict[str, Status] = {}
        for result in self.results:
            found[result.name] = merge(found.get(result.name), result.status)
        return dict(sorted(found.items()))


def merge(current: Status | None, new: Status) -> Status:
    """The same test reported twice: a failure anywhere wins."""
    if current is None or _RANK.get(new, 3) > _RANK.get(current, 3):
        return new
    return current


def _status(text: str) -> Status | None:
    match = _STATUS.fullmatch(text.strip())
    if match is None:
        return None
    word = match.group("status")
    if word == "FAILED":
        return Status.FAILED
    if word.startswith("ignored"):
        return Status.IGNORED
    return Status.PASSED  # "ok" and "bench: ..." (a bench run in test mode passed)


def _count(value: Any) -> int:
    """A JSON count (some old nightlies wrote numbers as strings)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


_JSON_EVENTS = {
    "ok": Status.PASSED,
    "failed": Status.FAILED,
    "ignored": Status.IGNORED,
    "allowed_failure": Status.FAILED,
}


class _Parser:
    def __init__(self, unfinished: Status) -> None:
        self.unfinished = unfinished
        self.run = ParsedRun()
        self.current = SuiteRun(UNKNOWN_SUITE)
        self.raw: list[tuple[Suite, str, Status]] = []
        self.pending: str | None = None  # text test line still waiting for its status
        self.started: dict[str, None] = {}  # JSON tests started but not finished

    def feed(self, line: str) -> None:
        if line.startswith("{") and self._json(line):
            return
        suite = suite_of(line)
        if suite is not None:
            self._close()
            self.current = SuiteRun(suite)
            return
        self._text(line.rstrip())

    def _close(self) -> None:
        """End the current suite. A test that started and never reported failed (the
        binary crashed) or timed out (the run was stopped)."""
        leftovers = list(self.started)
        if self.pending is not None:
            leftovers.append(self.pending)
        if self.current.summary is None:
            for name in leftovers:
                self.raw.append((self.current.suite, name, self.unfinished))
        self.pending = None
        self.started = {}
        if self.current.planned is not None or self.current.summary is not None:
            self.run.suites.append(self.current)

    def _text(self, line: str) -> bool:
        stripped = line.strip()
        if self.pending is not None:
            status = _status(stripped)
            if status is not None:
                self.raw.append((self.current.suite, self.pending, status))
                self.pending = None
                return True
        count = _RUNNING_N.match(stripped)
        if count is not None:
            self.current.planned = int(count.group("count"))
            return True
        summary = _SUMMARY.match(stripped)
        if summary is not None:
            self.current.summary = {
                key: int(value)
                for key, value in summary.groupdict().items()
                if key != "verdict" and value is not None
            }
            return True
        match = _TEST.match(line) or _TEST.search(line)
        if match is None:
            return False
        name = match.group("name")
        status = _status(match.group("rest") or "")
        if status is None:
            if self.pending is not None:  # the previous test never reported
                self.raw.append((self.current.suite, self.pending, self.unfinished))
            self.pending = name
        else:
            self.raw.append((self.current.suite, name, status))
        return True

    def _json(self, line: str) -> bool:
        try:
            event: Any = json.loads(line)
        except ValueError:
            return False
        if not isinstance(event, dict) or event.get("type") not in ("suite", "test", "bench"):
            return False
        self.run.json_events += 1
        kind, what = event.get("type"), event.get("event")
        if kind == "suite":
            if what == "started":
                self.current.planned = _count(event.get("test_count"))
            elif what in ("ok", "failed"):
                counts = {k: v for k, v in event.items() if isinstance(v, int)}
                self.current.summary = counts
            return True
        name = str(event.get("name", ""))
        if kind == "bench":
            self.raw.append((self.current.suite, name, Status.PASSED))
        elif what == "started":
            self.started[name] = None
        elif what in _JSON_EVENTS:
            self.started.pop(name, None)
            self.raw.append((self.current.suite, name, _JSON_EVENTS[what]))
        return True  # "timeout" is a warning about a slow test, not a result

    def finish(self) -> ParsedRun:
        self._close()
        self.run.results = _stable_names(self.raw)
        return self.run


def _stable_names(raw: list[tuple[Suite, str, Status]]) -> list[TestResult]:
    """Merge duplicates per binary and give doctests ordinal names."""
    merged: dict[tuple[Suite, str], Status] = {}
    for suite, name, status in raw:
        merged[(suite, name)] = merge(merged.get((suite, name)), status)
    doctests: dict[tuple[Suite, str], list[tuple[int, str]]] = defaultdict(list)
    results: list[TestResult] = []
    for (suite, name), status in merged.items():
        doc = _DOCTEST.match(name)
        if doc is None:
            results.append(TestResult(suite, name, name, status))
        else:
            key = f"{doc.group('file')} - {doc.group('item') or CRATE_DOCTEST}"
            doctests[(suite, key)].append((int(doc.group("line")), name))
    for (suite, key), entries in doctests.items():
        for ordinal, (_, name) in enumerate(sorted(entries), start=1):
            stable = key if ordinal == 1 else f"{key} #{ordinal}"
            results.append(TestResult(suite, stable, name, merged[(suite, name)]))
    return sorted(results, key=lambda r: (r.suite.binary, r.suite.doc, r.name))


def parse_libtest(text: str, *, timed_out: bool = False) -> ParsedRun:
    """Every test result in ``cargo test`` output, text or JSON, with its binary.

    ``timed_out`` says the run was stopped: a test that started and never reported is
    then a timeout instead of a failure.
    """
    parser = _Parser(Status.TIMEOUT if timed_out else Status.FAILED)
    for line in text.splitlines():
        parser.feed(line)
    return parser.finish()


def compile_failed(text: str, exit_code: int) -> bool:
    """The run stopped at a build error before any test binary started."""
    return exit_code != 0 and not parse_libtest(text).started and bool(_COMPILE_ERROR.search(text))


def doctest_item(raw: str) -> str | None:
    """The item path of a doctest name (``Foo::bar`` of ``src/lib.rs - Foo::bar (line 3)``),
    the file for a doctest of the crate's own docs, None for other tests."""
    match = _DOCTEST.match(raw)
    if match is None:
        return None
    item: str | None = match.group("item")
    return item or match.group("file")


def summarize(statuses: dict[str, Status] | list[Status]) -> dict[str, int]:
    """Counts of passed, failed and ignored tests."""
    values = list(statuses.values()) if isinstance(statuses, dict) else statuses
    return {s.value: values.count(s) for s in (Status.PASSED, Status.FAILED, Status.IGNORED)}

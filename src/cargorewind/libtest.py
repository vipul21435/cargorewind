"""Parse libtest text output and compute FAIL_TO_PASS and PASS_TO_PASS."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum

_RESULT = re.compile(r"^test (?P<name>.+?) \.\.\. (?P<status>ok|FAILED|ignored\b.*|bench:.*)$")
_DOCTEST = re.compile(r"^(?P<file>.+?) - (?P<item>.+?) \(line (?P<line>\d+)\)(?P<rest>.*)$")
_SHOULD_PANIC = " - should panic"


class Outcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    IGNORED = "ignored"


_RANK = {Outcome.FAILED: 2, Outcome.PASSED: 1, Outcome.IGNORED: 0}


def _outcome(status: str) -> Outcome:
    if status == "FAILED":
        return Outcome.FAILED
    if status.startswith("ignored"):
        return Outcome.IGNORED
    return Outcome.PASSED  # "ok" and "bench: ..." (a bench run in test mode passed)


def _merge(current: Outcome | None, new: Outcome) -> Outcome:
    """Same name twice (e.g. two test binaries): a failure anywhere wins."""
    if current is None or _RANK[new] > _RANK[current]:
        return new
    return current


def parse_libtest(text: str) -> dict[str, Outcome]:
    """Map test name -> outcome from ``cargo test`` text output.

    Doctest names embed a line number (``src/lib.rs - f (line 12)``) that shifts whenever
    a patch adds lines above it. The number is replaced by an ordinal per item
    (``src/lib.rs - f``, then ``src/lib.rs - f #2``), so names stay stable across runs.
    """
    outcomes: dict[str, Outcome] = {}
    doctests: dict[str, list[tuple[int, Outcome]]] = defaultdict(list)
    for raw in text.splitlines():
        match = _RESULT.match(raw.rstrip())
        if match is None:
            continue
        name = match.group("name").removesuffix(_SHOULD_PANIC)
        outcome = _outcome(match.group("status"))
        doc = _DOCTEST.match(name)
        if doc is not None:
            key = f"{doc.group('file')} - {doc.group('item')}{doc.group('rest')}"
            doctests[key].append((int(doc.group("line")), outcome))
            continue
        outcomes[name] = _merge(outcomes.get(name), outcome)
    for key, entries in doctests.items():
        for ordinal, (_, outcome) in enumerate(sorted(entries, key=lambda e: e[0]), start=1):
            name = key if ordinal == 1 else f"{key} #{ordinal}"
            outcomes[name] = _merge(outcomes.get(name), outcome)
    return dict(sorted(outcomes.items()))


@dataclass(frozen=True)
class Flip:
    """Classification of the three runs: base, before (test patch), after (both)."""

    fail_to_pass: list[str]
    pass_to_pass: list[str]
    regressions: list[str]
    still_failing: list[str]

    @property
    def verified(self) -> bool:
        return bool(self.fail_to_pass) and not self.regressions


def compute_flip(
    base: dict[str, Outcome], before: dict[str, Outcome], after: dict[str, Outcome]
) -> Flip:
    """FAIL_TO_PASS: passes after, and failed before (or was absent before, e.g. a compile
    error, and did not pass at base). PASS_TO_PASS: passes after, and passed before (or
    was absent before but passed at base). Regressions: passed before but not after, or
    passed at base and fails after. Still failing: fails after without having passed
    earlier. A test absent after that was also absent before (removed by the test
    patch) is in no list.
    """
    fail_to_pass: list[str] = []
    pass_to_pass: list[str] = []
    regressions: list[str] = []
    still_failing: list[str] = []
    for name in sorted(set(base) | set(before) | set(after)):
        was = before.get(name)
        passed_earlier = was is Outcome.PASSED or (was is None and base.get(name) is Outcome.PASSED)
        now = after.get(name)
        if now is Outcome.PASSED:
            if passed_earlier:
                pass_to_pass.append(name)
            elif was is Outcome.FAILED or was is None:
                fail_to_pass.append(name)
        elif was is Outcome.PASSED or (now is Outcome.FAILED and base.get(name) is Outcome.PASSED):
            regressions.append(name)
        elif now is Outcome.FAILED:
            still_failing.append(name)
    return Flip(fail_to_pass, pass_to_pass, regressions, still_failing)


def summarize(outcomes: dict[str, Outcome]) -> dict[str, int]:
    counts = {outcome.value: 0 for outcome in Outcome}
    for outcome in outcomes.values():
        counts[outcome.value] += 1
    return counts

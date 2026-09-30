"""FAIL_TO_PASS and PASS_TO_PASS from three runs, and flaky tests from reruns.

The three runs are base (no patches), before (test patch) and after (both patches).
Every test is identified by its cargo target and its stable name; its id in the lists
is the name alone, or ``name [target]`` when two targets have a test of that name.

Each FAIL_TO_PASS and PASS_TO_PASS candidate is then rerun by exact name, N times, in
the stages that decided it: after (it must pass), before (when the before run built and
reported it), and base (for a PASS_TO_PASS test that only the base run reported). A test
whose outcome changes between its stage run and any rerun is flaky: it leaves both
lists, and the reason lists the outcomes. A round that never got to run the test (the
rerun run hit its timeout first, or the command's per-test timeout ran out while cargo
was still building) is left out of that comparison instead of counting as a change.
"""

from __future__ import annotations

import re
import shlex
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field, replace

from cargorewind.backend import RunResult
from cargorewind.libtest import (
    NOT_RUN,
    Status,
    TestResult,
    compile_failed,
    merge,
    parse_libtest,
)
from cargorewind.testtargets import TargetMap, TestTarget, rerun_command

STAGES = ("base", "before", "after")
TIMEOUT_EXIT_CODES = frozenset({124, 137})  # coreutils timeout: TERM, then KILL
RERUN_KILL_AFTER = 10  # seconds between TERM and KILL for a rerun past its timeout

TestKey = tuple[TestTarget, str]

_BEGIN = re.compile(r"^--- cargorewind: rerun (\d+) (\d+) ---$")
_EXIT = re.compile(r"^--- cargorewind: exit (\d+) ---$")


class StageState:
    RAN = "ran"
    COMPILE_ERROR = "compile-error"
    TIMEOUT = "timeout"
    PROBE_FAILED = "probe-failed"


@dataclass
class StageTests:
    """What one stage run reported, per test."""

    stage: str
    state: str = StageState.RAN
    results: dict[TestKey, TestResult] = field(default_factory=dict)

    def status(self, key: TestKey) -> Status:
        """The test's status, or what the run's state says about a test it did not report."""
        result = self.results.get(key)
        if result is not None:
            return result.status
        if self.state == StageState.COMPILE_ERROR:
            return Status.COMPILE_ERROR
        if self.state == StageState.TIMEOUT:
            return Status.TIMEOUT
        return Status.MISSING

    def counts(self) -> dict[str, int]:
        statuses = [r.status for r in self.results.values()]
        return {s.value: statuses.count(s) for s in (Status.PASSED, Status.FAILED, Status.IGNORED)}


def stage_tests(
    stage: str, run: RunResult, targets: TargetMap, *, probe_failed: bool = False
) -> StageTests:
    """Parse one stage run and key its results by cargo target and stable name."""
    parsed = parse_libtest(run.output, timed_out=run.timed_out)
    results: dict[TestKey, TestResult] = {}
    for result in parsed.results:
        key = (targets.resolve(result.suite), result.name)
        earlier = results.get(key)
        if earlier is None:
            results[key] = result
        else:  # two binaries resolved to one target: a failure wins
            results[key] = replace(result, status=merge(earlier.status, result.status))
    if run.timed_out:
        state = StageState.TIMEOUT
    elif probe_failed:
        state = StageState.PROBE_FAILED
    elif compile_failed(run.output, run.exit_code):
        state = StageState.COMPILE_ERROR
    else:
        state = StageState.RAN
    return StageTests(stage, state, results)


def assign_ids(keys: Iterable[TestKey]) -> dict[TestKey, str]:
    """The id of each test: its name, qualified by the target when names collide."""
    unique = set(keys)
    counts = Counter(name for _, name in unique)
    return {key: key[1] if counts[key[1]] == 1 else f"{key[1]} [{key[0].label}]" for key in unique}


@dataclass(frozen=True)
class Flaky:
    id: str
    reason: str


@dataclass
class Flip:
    """Classification of the three runs (and of the reruns, once applied)."""

    fail_to_pass: list[str]
    pass_to_pass: list[str]
    regressions: list[str]
    still_failing: list[str]
    keys: dict[str, TestKey] = field(default_factory=dict)  # id -> (target, name)
    flaky: list[Flaky] = field(default_factory=list)
    reruns: dict[str, dict[str, list[Status]]] = field(default_factory=dict)  # id -> stage

    @property
    def verified(self) -> bool:
        return bool(self.fail_to_pass) and not self.regressions


def compute_flip(base: StageTests, before: StageTests, after: StageTests) -> Flip:
    """FAIL_TO_PASS: passes after, and failed before (or was not reported before, e.g. a
    compile error, and did not pass at base). PASS_TO_PASS: passes after, and passed
    before (or was not reported before but passed at base). Regressions: passed before
    but not after, or passed at base and fails after. Still failing: fails after without
    having passed earlier. A test absent after that was also absent before (removed by
    the test patch) is in no list.
    """
    ids = assign_ids([*base.results, *before.results, *after.results])
    flip = Flip([], [], [], [], keys={test_id: key for key, test_id in ids.items()})
    for key in sorted(ids, key=lambda k: ids[k]):
        test_id = ids[key]
        was, now, at_base = before.status(key), after.status(key), base.status(key)
        absent_before = was in NOT_RUN
        passed_earlier = was is Status.PASSED or (absent_before and at_base is Status.PASSED)
        if now is Status.PASSED:
            if passed_earlier:
                flip.pass_to_pass.append(test_id)
            elif was is Status.FAILED or absent_before:
                flip.fail_to_pass.append(test_id)
        elif was is Status.PASSED or (now is Status.FAILED and at_base is Status.PASSED):
            flip.regressions.append(test_id)
        elif now is Status.FAILED:
            flip.still_failing.append(test_id)
    return flip


# Reruns


@dataclass(frozen=True)
class Rerun:
    id: str
    key: TestKey
    raw: str  # the name as the stage reported it (doctests keep their line number)
    command: tuple[str, ...]


def rerun_plan(
    flip: Flip, stages: dict[str, StageTests], stage_command: tuple[str, ...]
) -> dict[str, list[Rerun]]:
    """The tests to rerun in each stage (only stages with at least one)."""
    plan: dict[str, list[Rerun]] = {stage: [] for stage in STAGES}
    for test_id in [*flip.fail_to_pass, *flip.pass_to_pass]:
        key = flip.keys[test_id]
        wanted = ["after"]
        if key in stages["before"].results:
            wanted.append("before")
        elif test_id in flip.pass_to_pass and key in stages["base"].results:
            wanted.append("base")  # its PASS_TO_PASS verdict came from the base run
        for stage in wanted:
            raw = stages[stage].results[key].raw
            command = rerun_command(key[0], raw, stage_command)
            plan[stage].append(Rerun(test_id, key, raw, command))
    return {stage: items for stage, items in plan.items() if items}


def rerun_script(items: list[Rerun], rounds: int, timeout: int) -> str:
    """A shell script that reruns every test ``rounds`` times, each under ``timeout``,
    with a marker before each command and its exit status after it."""
    lines = []
    for round_ in range(1, rounds + 1):
        for index, item in enumerate(items):
            lines.append(f"echo '--- cargorewind: rerun {round_} {index} ---'")
            lines.append(
                f"timeout -k {RERUN_KILL_AFTER} {timeout} {shlex.join(item.command)} 2>&1; "
                'echo "--- cargorewind: exit $? ---"'
            )
    return "\n".join(lines) + "\n"


def _segments(output: str) -> dict[tuple[int, int], tuple[str, int | None]]:
    """(round, index) -> (output, exit status or None when the script was cut off)."""
    found: dict[tuple[int, int], tuple[str, int | None]] = {}
    current: tuple[int, int] | None = None
    chunk: list[str] = []
    for line in output.splitlines():
        begin = _BEGIN.match(line)
        if begin is not None:
            if current is not None:
                found[current] = ("\n".join(chunk), None)
            current, chunk = (int(begin.group(1)), int(begin.group(2))), []
            continue
        end = _EXIT.match(line)
        if end is not None and current is not None:
            found[current] = ("\n".join(chunk), int(end.group(1)))
            current, chunk = None, []
            continue
        chunk.append(line)
    if current is not None:
        found[current] = ("\n".join(chunk), None)
    return found


def _rerun_status(
    segment: tuple[str, int | None] | None, item: Rerun, targets: TargetMap, timed_out: bool
) -> Status | None:
    """The outcome of one rerun command, or None when it never got to run the test."""
    if segment is None:  # never started: the whole run was stopped (or never ran)
        return None if timed_out else Status.MISSING
    text, code = segment
    stopped = code is None or code in TIMEOUT_EXIT_CODES
    parsed = parse_libtest(text, timed_out=stopped)
    for result in parsed.results:
        if result.raw == item.raw and targets.resolve(result.suite) == item.key[0]:
            return result.status
    cut_off = code is None and timed_out  # the run's own timeout stopped this command
    if cut_off or (stopped and not parsed.started):
        return None  # it never reached the test: cut off, or still building when stopped
    if stopped:
        return Status.TIMEOUT
    if compile_failed(text, code or 0):
        return Status.COMPILE_ERROR
    return Status.MISSING


def parse_reruns(
    output: str, items: list[Rerun], rounds: int, targets: TargetMap, *, timed_out: bool = False
) -> dict[TestKey, list[Status]]:
    """The status of each test in each round of a rerun script's output.

    A round that never got to run the test is left out, so a list can be shorter than
    ``rounds``: the round never started or was cut off because the whole run hit its
    timeout (``timed_out``), or its command reached the per-test timeout before any test
    binary started (cargo was still building). Such a round says nothing about the test,
    so it cannot make the test flaky.
    """
    segments = _segments(output)
    statuses: dict[TestKey, list[Status]] = {item.key: [] for item in items}
    for round_ in range(1, rounds + 1):
        for index, item in enumerate(items):
            segment = segments.get((round_, index))
            status = _rerun_status(segment, item, targets, timed_out)
            if status is not None:
                statuses[item.key].append(status)
    return statuses


def changed_outcomes(stage: StageTests, statuses: dict[TestKey, list[Status]]) -> int:
    """How many tests had a rerun outcome other than their stage run's."""
    return sum(1 for key, seen in statuses.items() if any(s != stage.status(key) for s in seen))


def apply_reruns(
    flip: Flip, stages: dict[str, StageTests], reruns: dict[str, dict[TestKey, list[Status]]]
) -> Flip:
    """Take the tests whose outcome changed out of both lists, with the reason."""
    flaky: list[Flaky] = []
    recorded: dict[str, dict[str, list[Status]]] = {}
    for test_id in [*flip.fail_to_pass, *flip.pass_to_pass]:
        key = flip.keys[test_id]
        changes = []
        for stage in STAGES:
            seen = reruns.get(stage, {}).get(key)
            if seen is None:
                continue
            recorded.setdefault(test_id, {})[stage] = seen
            outcomes = [stages[stage].status(key), *seen]
            if len(set(outcomes)) > 1:
                changes.append(f"{stage} {', '.join(outcomes)}")
        if changes:
            reason = "outcome changed between the stage run and its reruns: " + "; ".join(changes)
            flaky.append(Flaky(test_id, reason))
    unstable = {f.id for f in flaky}
    return replace(
        flip,
        fail_to_pass=[t for t in flip.fail_to_pass if t not in unstable],
        pass_to_pass=[t for t in flip.pass_to_pass if t not in unstable],
        flaky=flaky,
        reruns=recorded,
    )

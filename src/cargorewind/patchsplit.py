"""Parse a ``git diff`` and split it into a test patch and a fix patch.

Every changed file is classified by its role in the Cargo layout (see ``layout``).
Integration tests, their support files and module files that only build under
``#[cfg(test)]`` go to the test patch whole. Other Rust files are split line by line:
changed lines inside test-only regions (``#[cfg(test)]`` modules, items and
declarations, found by ``rustscan``) go to the test patch, the rest to the fix patch.
Everything else (sources without test changes, benches, examples, build scripts,
manifests, lockfiles, docs) goes to the fix patch. The test patch applies to the base
commit; the fix patch applies on top of it, and together they reproduce the fix commit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from cargorewind.layout import Layout, Package, Role, SourceTree
from cargorewind.rustscan import TestRegion, in_regions, scan_source

_HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
_DIFF_GIT = "diff --git "


class PatchError(ValueError):
    """The diff text could not be parsed."""


class Side(StrEnum):
    TEST = "test"
    FIX = "fix"


@dataclass
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    section: str
    lines: list[str]

    def range(self) -> str:
        """The header without the section text: ``@@ -1,3 +1,4 @@``."""
        return f"@@ -{self.old_start},{self.old_len} +{self.new_start},{self.new_len} @@"

    def header(self) -> str:
        return self.range() + self.section

    def render(self) -> list[str]:
        return [self.header(), *self.lines]


@dataclass
class FileDiff:
    old_path: str | None
    new_path: str | None
    header: list[str]
    hunks: list[Hunk] = field(default_factory=list)
    body: list[str] = field(default_factory=list)  # opaque content, e.g. a binary patch

    @property
    def path(self) -> str:
        path = self.new_path or self.old_path
        assert path is not None
        return path

    @property
    def is_new(self) -> bool:
        return self.old_path is None

    @property
    def is_deleted(self) -> bool:
        return self.new_path is None

    @property
    def paths(self) -> set[str]:
        return {p for p in (self.old_path, self.new_path) if p is not None}

    def render(self) -> list[str]:
        lines = list(self.header)
        for hunk in self.hunks:
            lines.extend(hunk.render())
        lines.extend(self.body)
        return lines


def _strip_prefix(path: str) -> str | None:
    if path == "/dev/null":
        return None
    return path[2:] if path[:2] in ("a/", "b/") else path


def _paths_from_diff_git(line: str) -> tuple[str, str]:
    rest = line[len(_DIFF_GIT) :]
    # "a/<path> b/<path>": with --no-renames both halves name the same path.
    half = (len(rest) - 1) // 2
    return rest[2:half], rest[half + 3 :]


def _parse_hunk(lines: list[str], i: int) -> tuple[Hunk, int]:
    match = _HUNK_HEADER.match(lines[i])
    if match is None:
        raise PatchError(f"bad hunk header: {lines[i]!r}")
    old_start, old_len, new_start, new_len, section = match.groups()
    hunk = Hunk(
        int(old_start),
        1 if old_len is None else int(old_len),
        int(new_start),
        1 if new_len is None else int(new_len),
        section,
        [],
    )
    old_left, new_left = hunk.old_len, hunk.new_len
    i += 1
    while i < len(lines) and (old_left > 0 or new_left > 0 or lines[i].startswith("\\")):
        line = lines[i]
        tag = line[:1]
        if tag == " ":
            old_left -= 1
            new_left -= 1
        elif tag == "-":
            old_left -= 1
        elif tag == "+":
            new_left -= 1
        elif tag != "\\":
            raise PatchError(f"unexpected line in hunk: {line!r}")
        hunk.lines.append(line)
        i += 1
    if old_left or new_left:
        raise PatchError(f"truncated hunk: {hunk.header()}")
    return hunk, i


def _parse_file(lines: list[str]) -> FileDiff:
    old_git, new_git = _paths_from_diff_git(lines[0])
    old_path: str | None = old_git
    new_path: str | None = new_git
    header: list[str] = []
    i = 0
    while i < len(lines) and not lines[i].startswith("@@ "):
        line = lines[i]
        if line.startswith("new file mode"):
            old_path = None
        elif line.startswith("deleted file mode"):
            new_path = None
        elif line.startswith("--- "):
            old_path = _strip_prefix(line[4:])
        elif line.startswith("+++ "):
            new_path = _strip_prefix(line[4:])
        header.append(line)
        i += 1
        if line.startswith("GIT binary patch"):
            header.pop()
            return FileDiff(old_path, new_path, header, body=lines[i - 1 :])
    diff = FileDiff(old_path, new_path, header)
    while i < len(lines):
        hunk, i = _parse_hunk(lines, i)
        diff.hunks.append(hunk)
    return diff


def parse_diff(text: str) -> list[FileDiff]:
    """Parse ``git diff`` output (``--no-renames``) into per-file diffs."""
    lines = text.splitlines()
    starts = [n for n, line in enumerate(lines) if line.startswith(_DIFF_GIT)]
    if lines and (not starts or starts[0] != 0):
        raise PatchError("diff text must start with 'diff --git'")
    bounds = [*starts, len(lines)]
    return [_parse_file(lines[bounds[k] : bounds[k + 1]]) for k in range(len(starts))]


def render_patch(files: list[FileDiff]) -> str:
    lines: list[str] = []
    for diff in files:
        lines.extend(diff.render())
    return "\n".join(lines) + "\n" if lines else ""


@dataclass(frozen=True)
class FileSplit:
    """Where one changed file went, and why."""

    path: str
    status: str  # added, deleted, modified
    role: Role
    package: str | None
    target: str | None
    patch: str  # test, fix or both
    reason: str


@dataclass(frozen=True)
class HunkSplit:
    """One hunk of a line-split Rust file and how its changed lines were divided."""

    path: str
    header: str
    old_start: int
    new_start: int
    test_lines: int
    fix_lines: int
    test_header: str | None
    fix_header: str | None


@dataclass(frozen=True)
class RegionReport:
    """A test-only region of a changed Rust file, at the fix (or, if deleted, base)."""

    path: str
    revision: str
    region: TestRegion


@dataclass
class SplitResult:
    test_files: list[FileDiff] = field(default_factory=list)
    fix_files: list[FileDiff] = field(default_factory=list)
    files: list[FileSplit] = field(default_factory=list)
    hunks: list[HunkSplit] = field(default_factory=list)
    regions: list[RegionReport] = field(default_factory=list)
    packages: list[Package] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def test_patch(self) -> str:
        return render_patch(self.test_files)

    @property
    def fix_patch(self) -> str:
        return render_patch(self.fix_files)

    @property
    def shared_files(self) -> list[str]:
        """Files whose changes landed in both patches."""
        test_paths = {d.path for d in self.test_files}
        return sorted(test_paths & {d.path for d in self.fix_files})

    @property
    def shared_hunks(self) -> list[HunkSplit]:
        """Every hunk of the files that landed in both patches."""
        shared = set(self.shared_files)
        return [h for h in self.hunks if h.path in shared]


def _pre(start: int, length: int) -> int:
    """Number of file lines before a hunk range (unified-diff zero-length convention)."""
    return start - 1 if length else start


def _start(pre: int, length: int) -> int:
    return pre + 1 if length else pre


def _line_sides(
    hunk: Hunk, base_regions: list[tuple[int, int]], fix_regions: list[tuple[int, int]]
) -> list[Side | None]:
    """Side of every changed line in ``hunk`` (None for context and markers)."""
    sides: list[Side | None] = []
    old_line, new_line = _pre(hunk.old_start, hunk.old_len), _pre(hunk.new_start, hunk.new_len)
    for line in hunk.lines:
        tag = line[:1]
        side: Side | None = None
        if tag == " ":
            old_line += 1
            new_line += 1
        elif tag == "-":
            old_line += 1
            side = Side.TEST if in_regions(old_line, base_regions) else Side.FIX
        elif tag == "+":
            new_line += 1
            side = Side.TEST if in_regions(new_line, fix_regions) else Side.FIX
        sides.append(side)
    return sides


def _project(lines: list[str], sides: list[Side | None], keep: Side) -> list[str]:
    """Hunk lines as seen by the patch for ``keep``.

    The intermediate tree (base plus test patch) holds context lines, removed fix lines
    and added test lines. Changes of the other side become context or disappear.
    """
    out: list[str] = []
    dropped = False
    for line, side in zip(lines, sides, strict=True):
        tag = line[:1]
        if tag == "\\":
            if not dropped:
                out.append(line)
            continue
        dropped = False
        if side is None or side is keep:
            out.append(line)
        elif (tag == "-" and keep is Side.TEST) or (tag == "+" and keep is Side.FIX):
            out.append(" " + line[1:])
        else:
            dropped = True
    return out


def _counts(lines: list[str]) -> tuple[int, int, bool]:
    old = sum(1 for line in lines if line[:1] in " -")
    new = sum(1 for line in lines if line[:1] in " +")
    changed = any(line[:1] in "+-" for line in lines)
    return old, new, changed


def _split_rust_file(
    diff: FileDiff,
    base_regions: list[tuple[int, int]],
    fix_regions: list[tuple[int, int]],
    result: SplitResult,
) -> tuple[FileDiff | None, FileDiff | None]:
    all_sides = [_line_sides(h, base_regions, fix_regions) for h in diff.hunks]
    flat = [s for sides in all_sides for s in sides if s is not None]
    if Side.TEST not in flat:
        return None, diff
    header = [line for line in diff.header if not line.startswith("index ")]
    test_diff = FileDiff(diff.old_path, diff.new_path, list(header))
    fix_diff = FileDiff(diff.old_path, diff.new_path, list(header))
    test_delta = 0
    for hunk, sides in zip(diff.hunks, all_sides, strict=True):
        pre_base = _pre(hunk.old_start, hunk.old_len)
        pre_mid = pre_base + test_delta
        pre_final = _pre(hunk.new_start, hunk.new_len)
        test_lines = _project(hunk.lines, sides, Side.TEST)
        fix_lines = _project(hunk.lines, sides, Side.FIX)
        t_old, t_new, t_changed = _counts(test_lines)
        f_old, f_new, f_changed = _counts(fix_lines)
        test_hunk = fix_hunk = None
        if t_changed:
            test_hunk = Hunk(
                _start(pre_base, t_old),
                t_old,
                _start(pre_mid, t_new),
                t_new,
                hunk.section,
                test_lines,
            )
            test_diff.hunks.append(test_hunk)
        if f_changed:
            fix_hunk = Hunk(
                _start(pre_mid, f_old),
                f_old,
                _start(pre_final, f_new),
                f_new,
                hunk.section,
                fix_lines,
            )
            fix_diff.hunks.append(fix_hunk)
        result.hunks.append(
            HunkSplit(
                diff.path,
                hunk.range(),
                hunk.old_start,
                hunk.new_start,
                sides.count(Side.TEST),
                sides.count(Side.FIX),
                test_hunk.range() if test_hunk else None,
                fix_hunk.range() if fix_hunk else None,
            )
        )
        test_delta += t_new - t_old
    return test_diff, (fix_diff if fix_diff.hunks else None)


def _status(diff: FileDiff) -> str:
    if diff.is_new:
        return "added"
    return "deleted" if diff.is_deleted else "modified"


def _route_rust(diff: FileDiff, base: SourceTree, fix: SourceTree, result: SplitResult) -> str:
    """Split one non-test Rust file; returns which patch(es) it went to."""
    revision, tree = ("base", base) if diff.is_deleted else ("fix", fix)
    current = tree.read(diff.path)
    scan = scan_source(current) if current is not None else None
    if scan is not None:
        result.regions.extend(RegionReport(diff.path, revision, r) for r in scan.regions)
    if diff.hunks and not diff.is_new and not diff.is_deleted and scan is not None:
        base_src = base.read(diff.path)
        if base_src is not None:
            test_part, fix_part = _split_rust_file(
                diff, scan_source(base_src).spans, scan.spans, result
            )
            if test_part is not None:
                result.test_files.append(test_part)
            if fix_part is not None:
                result.fix_files.append(fix_part)
            if test_part is not None and fix_part is not None:
                return "both"
            return "test" if test_part is not None else "fix"
    if diff.is_new and scan is not None and scan.regions:
        result.notes.append(f"{diff.path}: new file with test-only code kept whole in fix.patch")
    result.fix_files.append(diff)
    return "fix"


def split_diff(files: list[FileDiff], base: SourceTree, fix: SourceTree) -> SplitResult:
    """Route every file (or, for Rust sources, every changed line) to a side."""
    base_layout = Layout(base, [d.path for d in files if d.is_deleted])
    fix_layout = Layout(fix, [d.path for d in files if not d.is_deleted])
    result = SplitResult(packages=list(fix_layout.packages))
    result.notes.extend(fix_layout.notes)
    for diff in files:
        info = (base_layout if diff.is_deleted else fix_layout).classify(diff.path)
        if info.test_code:
            result.test_files.append(diff)
            patch = "test"
        elif diff.path.endswith(".rs"):
            patch = _route_rust(diff, base, fix, result)
        else:
            result.fix_files.append(diff)
            patch = "fix"
        result.files.append(
            FileSplit(
                diff.path,
                _status(diff),
                info.role,
                info.package,
                info.target,
                patch,
                info.reason,
            )
        )
    return result

"""The cargo target of each test binary, and the command that reruns one test.

cargo names a test binary after its target (``-`` becomes ``_``) and, since 1.5x, also
prints the target's source path. The layout of the checkout says which target that is:
the library (``--lib``), a binary (``--bin <name>``), an integration test
(``--test <name>``), a bench or an example, or the doctests (``--doc``). A test is
identified by that target and its name, so two binaries with a test of the same name
stay two tests.

Old cargo prints only the binary, and the library, a ``src/main.rs`` binary and a
``tests/<crate>.rs`` integration test named after the crate all build a binary of the
crate's name. Such a binary resolves to a ``shared`` target that names every candidate
(``lib demo or test demo``) and is rerun with ``--tests``, which runs every binary
``cargo test`` runs (but no doctests) and so reaches whichever of them has the test.

One test is rerun with ``cargo test <selector> -- --exact <name>``, which libtest
matches on every toolchain. Doctests are the exception: rustdoc splits its test
arguments on whitespace, so a doctest name (``src/lib.rs - f (line 3)``) cannot be
passed whole. A doctest is rerun with its item path as a substring filter
(``cargo test --doc -- f``), and its exact name is picked from the output.
"""

from __future__ import annotations

import posixpath
from collections.abc import Iterable
from dataclasses import dataclass

from cargorewind.layout import Target
from cargorewind.libtest import Suite, crate_name, doctest_item

TARGET_KINDS = ("lib", "bin", "test", "bench", "example")
SHARED = "shared"


@dataclass(frozen=True, order=True)
class TestTarget:
    __test__ = False  # not a pytest test class

    kind: str  # lib, bin, test, bench, example, doc, shared, or unknown
    name: str  # the cargo target name (the crate name for doc, the binary otherwise)
    members: tuple[TestTarget, ...] = ()  # shared: the targets that binary may be

    @property
    def label(self) -> str:
        if self.members:
            return " or ".join(member.label for member in self.members)
        return f"{self.kind} {self.name}" if self.name else self.kind

    @property
    def selector(self) -> tuple[str, ...]:
        """cargo test arguments that build and run only this target."""
        if self.kind == "lib":
            return ("--lib",)
        if self.kind == "doc":
            return ("--doc",)
        if self.kind == SHARED:
            return ("--tests",)  # every binary a stage run runs, but not the doctests
        if self.kind in TARGET_KINDS:
            return (f"--{self.kind}", self.name)
        return ()  # unknown: every binary runs, and the result is picked by binary name


UNKNOWN_TARGET = TestTarget("unknown", "")


def _from_path(suite: Suite) -> TestTarget | None:
    """The target of an auto-discovered source path that the layout does not list."""
    path = suite.path or ""
    parts = path.split("/")
    stem = posixpath.splitext(parts[-1])[0]
    name = parts[-2] if stem == "main" and len(parts) > 2 else stem
    if path == "src/lib.rs":
        return TestTarget("lib", suite.binary)
    if path == "src/main.rs":
        return TestTarget("bin", suite.binary)
    folders = {"tests": "test", "benches": "bench", "examples": "example"}
    if len(parts) > 1 and parts[0] in folders:
        return TestTarget(folders[parts[0]], name)
    if path.startswith("src/bin/"):
        return TestTarget("bin", name)
    return None


class TargetMap:
    """Resolves the binaries of a run to cargo targets of the checkout."""

    def __init__(self, targets: Iterable[Target] = ()) -> None:
        self.targets = sorted(
            {t for t in targets if t.kind in TARGET_KINDS},
            key=lambda t: (TARGET_KINDS.index(t.kind), t.path),
        )

    def resolve(self, suite: Suite) -> TestTarget:
        if suite.doc:
            return TestTarget("doc", suite.binary)
        if not suite.binary:
            return UNKNOWN_TARGET
        found = [t for t in self.targets if crate_name(t.name) == suite.binary]
        if suite.path:
            path = suite.path
            found = [t for t in found if t.path == path or t.path.endswith("/" + path)]
            if not found:
                guess = _from_path(suite)
                if guess is not None:
                    return guess
        members = tuple(dict.fromkeys(TestTarget(t.kind, t.name) for t in found))
        if len(members) == 1:
            return members[0]
        if members:  # old cargo printed no path, and several targets build this binary
            return TestTarget(SHARED, suite.binary, members)
        return TestTarget("unknown", suite.binary)


def rerun_command(target: TestTarget, raw: str, stage_command: tuple[str, ...]) -> tuple[str, ...]:
    """The command that reruns one test, built from the stage's test command (which keeps
    ``--offline`` and the JSON format flags; ``--no-fail-fast`` has no use for one test)."""
    if "--" in stage_command:
        cut = stage_command.index("--")
        head, tail = stage_command[:cut], stage_command[cut + 1 :]
    else:
        head, tail = stage_command, ()
    head = tuple(word for word in head if word != "--no-fail-fast")
    if target.kind == "doc":
        item = doctest_item(raw) or raw
        filters: tuple[str, ...] = (max(item.split() or [item], key=len),)
    else:
        filters = ("--exact", raw)
    return (*head, *target.selector, "--", *filters, *tail)

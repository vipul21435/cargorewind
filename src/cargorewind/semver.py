"""Cargo's semver versions and requirement syntax, for choosing crate versions offline.

This follows the ``semver`` crate that cargo uses:

- a bare version is a caret requirement: ``1.2.3`` means ``^1.2.3``;
- ``^1.2.3`` allows ``>=1.2.3, <2.0.0``; ``^0.2.3`` allows ``>=0.2.3, <0.3.0``; ``^0.0.3``
  allows only ``0.0.3``; ``^1.2`` and ``^1`` fill in zeros;
- ``~1.2.3`` allows ``>=1.2.3, <1.3.0``; ``~1`` allows any ``1.x``;
- ``*``, ``1.*`` and ``1.2.*`` (also ``x`` and ``X``) are wildcards;
- ``=``, ``>``, ``>=``, ``<``, ``<=`` compare, with missing parts meaning "any";
- comma-separated comparators must all match;
- a pre-release version only matches when some comparator names the same
  ``major.minor.patch`` with a pre-release of its own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_VERSION = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_COMPARATOR = re.compile(
    r"^(?P<op>\^|~|=|>=|<=|>|<)?\s*"
    r"(?P<major>\d+|[*xX])(?:\.(?P<minor>\d+|[*xX]))?(?:\.(?P<patch>\d+|[*xX]))?"
    r"(?:-(?P<pre>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_WILD = frozenset("*xX")

PreKey = tuple[tuple[int, int, str], ...]


class SemverError(ValueError):
    """A version or requirement that the semver grammar does not accept."""


def _pre_ids(text: str | None) -> tuple[str, ...]:
    return tuple(text.split(".")) if text else ()


def _pre_key(pre: tuple[str, ...]) -> PreKey:
    """Precedence of pre-release identifiers: numbers numerically, below alphanumerics."""
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre)


@dataclass(frozen=True)
class Version:
    major: int
    minor: int
    patch: int
    pre: tuple[str, ...] = ()
    build: str = ""

    @classmethod
    def parse(cls, text: str) -> Version:
        match = _VERSION.match(text.strip())
        if match is None:
            raise SemverError(f"not a semver version: {text!r}")
        major, minor, patch, pre, build = match.groups()
        return cls(int(major), int(minor), int(patch), _pre_ids(pre), build or "")

    def key(self) -> tuple[int, int, int, int, PreKey]:
        """Sort key by semver precedence (build metadata is ignored)."""
        return (self.major, self.minor, self.patch, 0 if self.pre else 1, _pre_key(self.pre))

    def __str__(self) -> str:
        text = f"{self.major}.{self.minor}.{self.patch}"
        if self.pre:
            text += "-" + ".".join(self.pre)
        return text + (f"+{self.build}" if self.build else "")


@dataclass(frozen=True)
class Comparator:
    op: str  # ^ ~ = > >= < <= or * (wildcard)
    major: int | None  # None only for a bare "*"
    minor: int | None = None
    patch: int | None = None
    pre: tuple[str, ...] = ()

    def _full(self) -> Version:
        return Version(self.major or 0, self.minor or 0, self.patch or 0, self.pre)

    def _cmp_partial(self, v: Version) -> int:
        """-1, 0 or 1: ``v`` against this comparator's given parts (missing parts are equal)."""
        mine = [p for p in (self.major, self.minor, self.patch) if p is not None]
        theirs = [v.major, v.minor, v.patch][: len(mine)]
        if theirs != mine:
            return -1 if theirs < mine else 1
        if self.patch is None:
            return 0
        a, b = v.key()[3:], self._full().key()[3:]
        return 0 if a == b else (-1 if a < b else 1)

    def matches(self, v: Version) -> bool:  # noqa: PLR0911 - one rule per operator
        if self.major is None:
            return True
        order = self._cmp_partial(v)
        if self.op in ("=", "*"):
            return order == 0
        if self.op == ">":
            return order > 0
        if self.op == ">=":
            return order >= 0
        if self.op == "<":
            return order < 0
        if self.op == "<=":
            return order <= 0
        if self.op == "~":
            same = (v.major, v.minor) == (
                self.major,
                self.minor if self.minor is not None else v.minor,
            )
            return same and order >= 0
        return self._caret(v, order)

    def _caret(self, v: Version, order: int) -> bool:
        if order < 0 or v.major != self.major:
            return False
        if self.major > 0 or self.minor is None:
            return True
        if v.minor != self.minor:
            return False
        if self.minor > 0 or self.patch is None:
            return True
        return v.patch == self.patch

    def __str__(self) -> str:
        if self.major is None:
            return "*"
        parts = [str(p) for p in (self.major, self.minor, self.patch) if p is not None]
        text = ".".join(parts) + ("-" + ".".join(self.pre) if self.pre else "")
        if self.op == "*":
            return text + ".*"
        return f"{self.op}{text}"


def _parse_comparator(text: str) -> Comparator:
    match = _COMPARATOR.match(text.strip())
    if match is None:
        raise SemverError(f"not a version requirement: {text!r}")
    op = match.group("op")
    raw = [match.group("major"), match.group("minor"), match.group("patch")]
    nums: list[int | None] = []
    wildcard = False
    for part in raw:
        if part is None or part in _WILD:
            wildcard = wildcard or part is not None
            break
        nums.append(int(part))
    pre = _pre_ids(match.group("pre"))
    if pre and len(nums) < 3:
        raise SemverError(f"a pre-release needs major.minor.patch: {text!r}")
    padded = (*nums, None, None, None)[:3]
    if not nums:
        return Comparator("*", None)
    if op is None:
        op = "*" if wildcard else "^"
    return Comparator(op, padded[0], padded[1], padded[2], pre)


@dataclass(frozen=True)
class VersionReq:
    comparators: tuple[Comparator, ...]

    @classmethod
    def parse(cls, text: str) -> VersionReq:
        text = text.strip()
        if text in ("", "*"):
            return cls(())
        return cls(tuple(_parse_comparator(part) for part in text.split(",")))

    def matches(self, v: Version) -> bool:
        if not all(c.matches(v) for c in self.comparators):
            return False
        if not v.pre:
            return True
        return any(
            c.pre and (c.major, c.minor, c.patch) == (v.major, v.minor, v.patch)
            for c in self.comparators
        )

    def __str__(self) -> str:
        return ", ".join(str(c) for c in self.comparators) or "*"


def matches(req: str, version: str) -> bool:
    """``VersionReq.parse(req).matches(Version.parse(version))``."""
    return VersionReq.parse(req).matches(Version.parse(version))

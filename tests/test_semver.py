from __future__ import annotations

import pytest

from cargorewind.semver import SemverError, Version, VersionReq, matches

# (requirement, versions that match, versions that do not)
CASES: list[tuple[str, list[str], list[str]]] = [
    # Caret, explicit and implied (a bare version is a caret requirement).
    ("^1.2.3", ["1.2.3", "1.2.4", "1.9.0"], ["1.2.2", "2.0.0", "0.9.9"]),
    ("1.2.3", ["1.2.3", "1.99.0"], ["2.0.0", "1.2.2"]),
    ("^1.2", ["1.2.0", "1.8.1"], ["1.1.9", "2.0.0"]),
    ("^1", ["1.0.0", "1.99.99"], ["0.99.0", "2.0.0"]),
    ("^0.2.3", ["0.2.3", "0.2.9"], ["0.2.2", "0.3.0", "1.0.0"]),
    ("^0.2", ["0.2.0", "0.2.99"], ["0.1.9", "0.3.0"]),
    ("^0.0.3", ["0.0.3"], ["0.0.2", "0.0.4", "0.1.0"]),
    ("^0.0", ["0.0.0", "0.0.9"], ["0.1.0"]),
    ("^0", ["0.0.1", "0.99.0"], ["1.0.0"]),
    # Tilde.
    ("~1.2.3", ["1.2.3", "1.2.9"], ["1.2.2", "1.3.0"]),
    ("~1.2", ["1.2.0", "1.2.7"], ["1.1.0", "1.3.0"]),
    ("~1", ["1.0.0", "1.9.0"], ["2.0.0", "0.9.0"]),
    ("~0.2.3", ["0.2.3", "0.2.5"], ["0.3.0"]),
    # Wildcards.
    ("*", ["0.0.1", "1.0.0", "99.0.0"], ["1.0.0-alpha"]),
    ("", ["3.1.4"], []),
    ("1.*", ["1.0.0", "1.9.9"], ["2.0.0", "0.9.0"]),
    ("1.2.*", ["1.2.0", "1.2.9"], ["1.3.0"]),
    ("1.x", ["1.4.0"], ["2.0.0"]),
    ("1.2.X", ["1.2.4"], ["1.3.0"]),
    # Comparisons, with missing parts meaning "any".
    ("=1.2.3", ["1.2.3"], ["1.2.4", "1.2.3-rc.1"]),
    ("=1.2", ["1.2.0", "1.2.9"], ["1.3.0"]),
    (">1.2.3", ["1.2.4", "2.0.0"], ["1.2.3"]),
    (">1.2", ["1.3.0"], ["1.2.9"]),
    (">1", ["2.0.0"], ["1.9.9"]),
    (">=1.2.3", ["1.2.3", "3.0.0"], ["1.2.2"]),
    (">=1.2", ["1.2.0"], ["1.1.9"]),
    ("<1.2.3", ["1.2.2", "0.1.0"], ["1.2.3"]),
    ("<1.2", ["1.1.9"], ["1.2.0"]),
    ("<=1.2.3", ["1.2.3"], ["1.2.4"]),
    ("<=1.2", ["1.2.9"], ["1.3.0"]),
    ("<=1", ["1.9.9"], ["2.0.0"]),
    # Several bounds must all hold; spaces around operators are allowed.
    (">=1.2, <1.5", ["1.2.0", "1.4.9"], ["1.5.0", "1.1.0"]),
    (">= 0.2.0, < 0.4", ["0.2.0", "0.3.9"], ["0.4.0", "0.1.9"]),
]


@pytest.mark.parametrize(("req", "yes", "no"), CASES, ids=[c[0] or "empty" for c in CASES])
def test_requirement_matching(req: str, yes: list[str], no: list[str]) -> None:
    parsed = VersionReq.parse(req)
    for version in yes:
        assert parsed.matches(Version.parse(version)), (req, version)
    for version in no:
        assert not parsed.matches(Version.parse(version)), (req, version)


def test_pre_releases_only_match_when_the_requirement_names_one() -> None:
    assert not matches("^1.0.0", "1.1.0-beta.1")
    assert not matches(">=1.0.0", "2.0.0-alpha")
    assert matches("^1.0.0-beta.1", "1.0.0-beta.2")
    assert matches("^1.0.0-beta.1", "1.0.0")
    assert matches("^1.0.0-beta.1", "1.3.0")
    assert not matches("^1.0.0-beta.1", "1.0.1-beta.1")  # another patch's pre-release
    assert not matches("^1.0.0-beta.2", "1.0.0-beta.1")
    assert matches(">=1.0.0-alpha, <1.0.0", "1.0.0-rc.1")
    assert matches("~1.2.3-alpha.1", "1.2.3-alpha.2")
    assert matches("=1.2.3-alpha", "1.2.3-alpha")


def test_version_precedence() -> None:
    order = [
        "1.0.0-alpha",
        "1.0.0-alpha.1",
        "1.0.0-alpha.beta",
        "1.0.0-beta",
        "1.0.0-beta.2",
        "1.0.0-beta.11",
        "1.0.0-rc.1",
        "1.0.0",
        "1.0.1",
        "1.10.0",
        "2.0.0",
    ]
    parsed = [Version.parse(v) for v in order]
    assert sorted(reversed(parsed), key=Version.key) == parsed
    assert Version.parse("1.0.0+build.5").key() == Version.parse("1.0.0").key()
    assert str(Version.parse("1.2.3-rc.1+b7")) == "1.2.3-rc.1+b7"


@pytest.mark.parametrize(
    ("req", "text"),
    [("^1.2.3", "^1.2.3"), ("1.2", "^1.2"), ("~0.2", "~0.2"), ("1.*", "1.*"), ("*", "*")],
)
def test_requirement_round_trip(req: str, text: str) -> None:
    assert str(VersionReq.parse(req)) == text
    assert str(VersionReq.parse(">=1.0.0-rc.1, <2")) == ">=1.0.0-rc.1, <2"


@pytest.mark.parametrize("bad", ["1.2.3.4", "01.2.3", "1.2.3-", "latest", "v1.2.3"])
def test_bad_versions(bad: str) -> None:
    with pytest.raises(SemverError):
        Version.parse(bad)


@pytest.mark.parametrize("bad", ["^^1", "1.2-beta", ">=", "1..2", "~> 1.2", "latest"])
def test_bad_requirements(bad: str) -> None:
    with pytest.raises(SemverError):
        VersionReq.parse(bad)

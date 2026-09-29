from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from cargorewind.crateindex import (
    USER_AGENT,
    CrateIndexError,
    DirectoryIndex,
    SparseIndex,
    candidates,
    index_path,
    parse_index_file,
)
from cargorewind.registry import HttpResponse
from cargorewind.semver import VersionReq
from tests.test_registry import FakeHttp

INDEX = Path(__file__).parent / "fixtures" / "crates-index"


def when(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


@pytest.mark.parametrize(
    ("name", "path"),
    [
        ("a", "1/a"),
        ("ab", "2/ab"),
        ("syn", "3/s/syn"),
        ("home", "ho/me/home"),
        ("Windows-Sys", "wi/nd/windows-sys"),
    ],
)
def test_index_path(name: str, path: str) -> None:
    assert index_path(name) == path


def test_recorded_entries_parse_with_publish_times_and_yanked_flags() -> None:
    home = DirectoryIndex(INDEX).versions("home")
    by_version = {v.vers: v for v in home}
    assert by_version["0.4.0"].yanked and by_version["0.5.2"].yanked
    assert not by_version["0.5.9"].yanked
    assert by_version["0.5.9"].pubtime == datetime(2023, 12, 15, 21, 0, 16, tzinfo=UTC)
    assert by_version["0.5.9"].rust_version == "1.70.0"
    assert by_version["0.5.9"].requirement_on("windows-sys") == ["^0.52"]
    assert by_version["0.5.9"].deps[0].target == "cfg(windows)"
    with pytest.raises(CrateIndexError, match="not in the recorded index"):
        DirectoryIndex(INDEX).versions("no-such-crate")


def test_parse_handles_renames_dev_deps_and_missing_fields() -> None:
    lines = [
        {
            "name": "tool",
            "vers": "1.0.0",
            "deps": [
                {"name": "rnd", "package": "rand", "req": "^0.8", "kind": "normal"},
                {"name": "tempfile", "req": "^3", "kind": "dev"},
                {"name": "cc", "req": "^1", "kind": "build", "optional": True},
                "junk",
            ],
            "yanked": False,
            "pubtime": "2024-01-02T03:04:05Z",
        },
        {"name": "tool", "vers": "not-semver", "yanked": False, "pubtime": "garbage"},
        {"name": "tool", "vers": "0.9.0", "yanked": True},
    ]
    text = "\n".join(json.dumps(line) for line in lines) + "\n\n"
    first, odd, old = parse_index_file(text)
    assert [d.name for d in first.deps] == ["rand", "tempfile", "cc"]
    assert first.requirement_on("rand") == ["^0.8"]
    assert first.requirement_on("tempfile") == []  # dev dependencies never resolve
    assert first.requirement_on("cc") == ["^1"]
    assert odd.version() is None and odd.pubtime is None
    assert old.pubtime is None and old.deps == ()
    with pytest.raises(CrateIndexError, match="not JSON"):
        parse_index_file("{nope\n")


def test_candidates_skip_yanked_late_and_pre_release_versions() -> None:
    targets = DirectoryIndex(INDEX).versions("windows-targets")
    caret = [VersionReq.parse("^0.52.0")]
    # 0.52.1 and 0.52.2 were published (and later yanked) before this cutoff.
    early = candidates(targets, caret, when("2024-02-22T16:00:00"))
    assert [v.vers for v in early] == ["0.52.0"]
    later = candidates(targets, caret, when("2024-03-01T00:00:00"))
    assert [v.vers for v in later] == ["0.52.4", "0.52.3", "0.52.0"]
    excluded = candidates(targets, caret, when("2024-03-01T00:00:00"), frozenset({"0.52.4"}))
    assert excluded[0].vers == "0.52.3"
    assert candidates(targets, caret, when("2023-01-01T00:00:00")) == []
    pre = parse_index_file(
        '{"name":"p","vers":"1.0.0-rc.1","yanked":false,"pubtime":"2020-01-01T00:00:00Z"}\n'
        '{"name":"p","vers":"0.9.0","yanked":false,"pubtime":"2019-01-01T00:00:00Z"}\n'
    )
    assert [v.vers for v in candidates(pre, [], when("2021-01-01T00:00:00"))] == ["0.9.0"]
    rc = [VersionReq.parse("^1.0.0-rc.1")]
    assert [v.vers for v in candidates(pre, rc, when("2021-01-01T00:00:00"))] == ["1.0.0-rc.1"]


def _http(body: bytes = b"", status: int = 200) -> FakeHttp:
    return FakeHttp({("GET", "ei/th/either"): HttpResponse(status, {}, body)})


def test_sparse_index_fetches_once_and_caches(tmp_path: Path) -> None:
    body = (INDEX / "ei" / "th" / "either").read_bytes()
    http = _http(body)
    clock = lambda: when("2026-09-30T00:00:00")  # noqa: E731
    index = SparseIndex(http, tmp_path, clock=clock)
    first = index.versions("either")
    assert index.fetched == ["either"]
    assert http.requests[0][2] == {"User-Agent": USER_AGENT}
    again = SparseIndex(http, tmp_path, fresh_after=when("2025-01-01T00:00:00"), clock=clock)
    assert again.versions("either") == first
    assert again.fetched == [] and len(http.requests) == 1
    cached = json.loads((tmp_path / "crates-index" / "ei" / "th" / "either.json").read_text())
    assert cached["fetched_at"] == "2026-09-30T00:00:00Z"


def test_sparse_index_refetches_a_cache_older_than_the_commit(tmp_path: Path) -> None:
    body = (INDEX / "ei" / "th" / "either").read_bytes()
    http = _http(body)
    SparseIndex(http, tmp_path, clock=lambda: when("2024-01-01T00:00:00")).versions("either")
    newer = SparseIndex(http, tmp_path, fresh_after=when("2024-06-01T00:00:00"))
    newer.versions("either")
    assert newer.fetched == ["either"] and len(http.requests) == 2


@pytest.mark.parametrize(
    ("cache", "refetch"),
    [
        ("not json", True),
        ('{"schema": 99}', True),
        ('{"schema": 1, "fetched_at": "2026-01-01T00:00:00Z"}', True),
        ('{"schema": 1, "fetched_at": "2026-01-01T00:00:00Z", "body": ""}', False),
    ],
)
def test_sparse_index_ignores_broken_cache_files(tmp_path: Path, cache: str, refetch: bool) -> None:
    path = tmp_path / "crates-index" / "ei" / "th" / "either.json"
    path.parent.mkdir(parents=True)
    path.write_text(cache)
    http = _http(b"")
    SparseIndex(http, tmp_path).versions("either")
    assert len(http.requests) == (1 if refetch else 0)


@pytest.mark.parametrize(
    ("status", "message"), [(404, "no such crate"), (500, "HTTP 500 from https://index")]
)
def test_sparse_index_errors(tmp_path: Path, status: int, message: str) -> None:
    with pytest.raises(CrateIndexError, match=message):
        SparseIndex(_http(status=status), tmp_path).versions("either")
    with pytest.raises(CrateIndexError, match="cannot reach"):
        SparseIndex(FakeHttp({}), tmp_path).versions("either")

from __future__ import annotations

from datetime import UTC, date, datetime
from itertools import pairwise

import pytest

from cargorewind.toolchain import (
    IMAGE_DIGESTS,
    STABLE_RELEASES,
    ToolchainError,
    base_image,
    newest_stable_before,
    normalize_version,
    parse_toolchain_file,
    release_date,
    resolve_toolchain,
)


def _key(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in version.split("."))


def test_table_is_sorted_by_version_and_date() -> None:
    versions = [v for v, _ in STABLE_RELEASES]
    assert versions[0] == "1.0.0"
    assert versions == sorted(versions, key=_key)
    assert len(set(versions)) == len(versions)
    days = [date.fromisoformat(d) for _, d in STABLE_RELEASES]
    assert days == sorted(days)


def test_minor_releases_follow_the_six_week_train() -> None:
    minors = [(v, date.fromisoformat(d)) for v, d in STABLE_RELEASES if v.endswith(".0")]
    assert [_key(v)[1] for v, _ in minors] == list(range(len(minors)))
    gaps = {(b - a).days for (_, a), (_, b) in pairwise(minors)}
    assert gaps <= {41, 42, 43}


def test_known_release_dates() -> None:
    assert release_date("1.0.0") == date(2015, 5, 15)
    assert release_date("1.39.0") == date(2019, 11, 7)
    assert release_date("1.40.0") == date(2019, 12, 19)
    with pytest.raises(ToolchainError):
        release_date("1.999.0")


def test_newest_stable_before() -> None:
    assert newest_stable_before(date(2019, 12, 13)) == "1.39.0"
    assert newest_stable_before(date(2019, 12, 19)) == "1.39.0"  # strictly before
    assert newest_stable_before(date(2019, 12, 20)) == "1.40.0"
    assert newest_stable_before(date(2017, 11, 23)) == "1.22.1"
    with pytest.raises(ToolchainError, match="no stable"):
        newest_stable_before(date(2015, 5, 15))


def test_normalize_version() -> None:
    assert normalize_version("1.39.0") == "1.39.0"
    assert normalize_version("1.22") == "1.22.1"
    assert normalize_version("stable") is None
    assert normalize_version("nightly-2020-01-01") is None
    with pytest.raises(ToolchainError):
        normalize_version("1.999")


@pytest.mark.parametrize(
    ("text", "channel"),
    [
        ("1.39.0\n", "1.39.0"),
        ("\n  stable  \n", "stable"),
        ('[toolchain]\nchannel = "1.70.0"\ncomponents = ["clippy"]\n', "1.70.0"),
    ],
)
def test_parse_toolchain_file(text: str, channel: str) -> None:
    assert parse_toolchain_file(text) == channel


@pytest.mark.parametrize("text", ["", "[toolchain]\ncomponents = []\n"])
def test_parse_toolchain_file_errors(text: str) -> None:
    with pytest.raises(ToolchainError):
        parse_toolchain_file(text)


FIX_TIME = datetime(2019, 12, 13, 2, 48, 41, tzinfo=UTC)


def test_resolve_by_release_date_without_files() -> None:
    chosen = resolve_toolchain(lambda name: None, FIX_TIME)
    assert chosen.version == "1.39.0"
    assert chosen.source == "release-date"
    assert "2019-12-13" in chosen.reason and "2019-11-07" in chosen.reason


def test_resolve_prefers_legacy_file_like_rustup() -> None:
    files = {"rust-toolchain": "1.56\n", "rust-toolchain.toml": '[toolchain]\nchannel = "1.70"\n'}
    chosen = resolve_toolchain(files.get, FIX_TIME)
    assert (chosen.version, chosen.source) == ("1.56.1", "rust-toolchain")


def test_resolve_toml_file_and_stable_channel() -> None:
    toml = {"rust-toolchain.toml": '[toolchain]\nchannel = "1.75.0"\n'}
    assert resolve_toolchain(toml.get, FIX_TIME).version == "1.75.0"
    stable = {"rust-toolchain": "stable\n"}
    assert resolve_toolchain(stable.get, FIX_TIME).source == "release-date"


def test_resolve_rejects_nightly_for_now() -> None:
    with pytest.raises(ToolchainError, match="only stable"):
        resolve_toolchain({"rust-toolchain": "nightly-2019-12-01\n"}.get, FIX_TIME)


def test_base_image_is_digest_pinned() -> None:
    assert base_image("1.39.0") == f"rust:1.39.0-slim@{IMAGE_DIGESTS['1.39.0']}"
    for version, digest in IMAGE_DIGESTS.items():
        assert version in {v for v, _ in STABLE_RELEASES}
        assert digest.startswith("sha256:") and len(digest) == 71
    with pytest.raises(ToolchainError, match="--image"):
        base_image("1.40.0")

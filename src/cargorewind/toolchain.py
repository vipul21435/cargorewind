"""Pick the Rust toolchain and the digest-pinned ``rust:<version>-slim`` base image.

Order of precedence (the core rules; MSRV and edition floors come in a later slice):

1. ``rust-toolchain`` (legacy file) or ``rust-toolchain.toml`` at the base commit. When
   both exist rustup uses the legacy file, and so do we. An exact version is used as is;
   ``stable`` falls through to the date rule.
2. Otherwise the newest stable release published strictly before the fix commit's date.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime

# Every stable release from 1.0.0, parsed from rust-lang/rust RELEASES.md
# ("Version X.Y.Z (YYYY-MM-DD)" headings), fetched 2026-09-30.
STABLE_RELEASES: tuple[tuple[str, str], ...] = (
    ("1.0.0", "2015-05-15"),
    ("1.1.0", "2015-06-25"),
    ("1.2.0", "2015-08-07"),
    ("1.3.0", "2015-09-17"),
    ("1.4.0", "2015-10-29"),
    ("1.5.0", "2015-12-10"),
    ("1.6.0", "2016-01-21"),
    ("1.7.0", "2016-03-03"),
    ("1.8.0", "2016-04-14"),
    ("1.9.0", "2016-05-26"),
    ("1.10.0", "2016-07-07"),
    ("1.11.0", "2016-08-18"),
    ("1.12.0", "2016-09-29"),
    ("1.12.1", "2016-10-20"),
    ("1.13.0", "2016-11-10"),
    ("1.14.0", "2016-12-22"),
    ("1.15.0", "2017-02-02"),
    ("1.15.1", "2017-02-09"),
    ("1.16.0", "2017-03-16"),
    ("1.17.0", "2017-04-27"),
    ("1.18.0", "2017-06-08"),
    ("1.19.0", "2017-07-20"),
    ("1.20.0", "2017-08-31"),
    ("1.21.0", "2017-10-12"),
    ("1.22.0", "2017-11-22"),
    ("1.22.1", "2017-11-22"),
    ("1.23.0", "2018-01-04"),
    ("1.24.0", "2018-02-15"),
    ("1.24.1", "2018-03-01"),
    ("1.25.0", "2018-03-29"),
    ("1.26.0", "2018-05-10"),
    ("1.26.1", "2018-05-29"),
    ("1.26.2", "2018-06-05"),
    ("1.27.0", "2018-06-21"),
    ("1.27.1", "2018-07-10"),
    ("1.27.2", "2018-07-20"),
    ("1.28.0", "2018-08-02"),
    ("1.29.0", "2018-09-13"),
    ("1.29.1", "2018-09-25"),
    ("1.29.2", "2018-10-11"),
    ("1.30.0", "2018-10-25"),
    ("1.30.1", "2018-11-08"),
    ("1.31.0", "2018-12-06"),
    ("1.31.1", "2018-12-20"),
    ("1.32.0", "2019-01-17"),
    ("1.33.0", "2019-02-28"),
    ("1.34.0", "2019-04-11"),
    ("1.34.1", "2019-04-25"),
    ("1.34.2", "2019-05-14"),
    ("1.35.0", "2019-05-23"),
    ("1.36.0", "2019-07-04"),
    ("1.37.0", "2019-08-15"),
    ("1.38.0", "2019-09-26"),
    ("1.39.0", "2019-11-07"),
    ("1.40.0", "2019-12-19"),
    ("1.41.0", "2020-01-30"),
    ("1.41.1", "2020-02-27"),
    ("1.42.0", "2020-03-12"),
    ("1.43.0", "2020-04-23"),
    ("1.43.1", "2020-05-07"),
    ("1.44.0", "2020-06-04"),
    ("1.44.1", "2020-06-18"),
    ("1.45.0", "2020-07-16"),
    ("1.45.1", "2020-07-30"),
    ("1.45.2", "2020-08-03"),
    ("1.46.0", "2020-08-27"),
    ("1.47.0", "2020-10-08"),
    ("1.48.0", "2020-11-19"),
    ("1.49.0", "2020-12-31"),
    ("1.50.0", "2021-02-11"),
    ("1.51.0", "2021-03-25"),
    ("1.52.0", "2021-05-06"),
    ("1.52.1", "2021-05-10"),
    ("1.53.0", "2021-06-17"),
    ("1.54.0", "2021-07-29"),
    ("1.55.0", "2021-09-09"),
    ("1.56.0", "2021-10-21"),
    ("1.56.1", "2021-11-01"),
    ("1.57.0", "2021-12-02"),
    ("1.58.0", "2022-01-13"),
    ("1.58.1", "2022-01-20"),
    ("1.59.0", "2022-02-24"),
    ("1.60.0", "2022-04-07"),
    ("1.61.0", "2022-05-19"),
    ("1.62.0", "2022-06-30"),
    ("1.62.1", "2022-07-19"),
    ("1.63.0", "2022-08-11"),
    ("1.64.0", "2022-09-22"),
    ("1.65.0", "2022-11-03"),
    ("1.66.0", "2022-12-15"),
    ("1.66.1", "2023-01-10"),
    ("1.67.0", "2023-01-26"),
    ("1.67.1", "2023-02-09"),
    ("1.68.0", "2023-03-09"),
    ("1.68.1", "2023-03-23"),
    ("1.68.2", "2023-03-28"),
    ("1.69.0", "2023-04-20"),
    ("1.70.0", "2023-06-01"),
    ("1.71.0", "2023-07-13"),
    ("1.71.1", "2023-08-03"),
    ("1.72.0", "2023-08-24"),
    ("1.72.1", "2023-09-19"),
    ("1.73.0", "2023-10-05"),
    ("1.74.0", "2023-11-16"),
    ("1.74.1", "2023-12-07"),
    ("1.75.0", "2023-12-28"),
    ("1.76.0", "2024-02-08"),
    ("1.77.0", "2024-03-21"),
    ("1.77.1", "2024-03-28"),
    ("1.77.2", "2024-04-09"),
    ("1.78.0", "2024-05-02"),
    ("1.79.0", "2024-06-13"),
    ("1.80.0", "2024-07-25"),
    ("1.80.1", "2024-08-08"),
    ("1.81.0", "2024-09-05"),
    ("1.82.0", "2024-10-17"),
    ("1.83.0", "2024-11-28"),
    ("1.84.0", "2025-01-09"),
    ("1.84.1", "2025-01-30"),
    ("1.85.0", "2025-02-20"),
    ("1.85.1", "2025-03-18"),
    ("1.86.0", "2025-04-03"),
    ("1.87.0", "2025-05-15"),
    ("1.88.0", "2025-06-26"),
    ("1.89.0", "2025-08-07"),
    ("1.90.0", "2025-09-18"),
    ("1.91.0", "2025-10-30"),
    ("1.91.1", "2025-11-10"),
    ("1.92.0", "2025-12-11"),
    ("1.93.0", "2026-01-22"),
    ("1.93.1", "2026-02-12"),
    ("1.94.0", "2026-03-05"),
    ("1.94.1", "2026-03-26"),
    ("1.95.0", "2026-04-16"),
    ("1.96.0", "2026-05-28"),
    ("1.96.1", "2026-06-30"),
    ("1.97.0", "2026-07-09"),
    ("1.97.1", "2026-07-16"),
    ("1.98.0", "2026-08-20"),
    ("1.98.1", "2026-09-03"),
)

# Multi-arch index digests of the official rust:<version>-slim images, resolved with
# `docker buildx imagetools inspect rust:<version>-slim` on 2026-09-30.
IMAGE_DIGESTS: dict[str, str] = {
    "1.39.0": "sha256:b47dd7b5f59bea2bc19ac18e81cc6b5b3cfe6c4e40082cab09604b296bca2652",
    "1.56.1": "sha256:cc2b5c03d4acf19be7fa8155cae8abbc8c3aa893a5d177ceb4921a3cbbb190da",
    "1.70.0": "sha256:d1f62de1372e7103b9973848c3f873abfb73a6668ce4b0af2fe57fd9e32178b8",
    "1.75.0": "sha256:70c2a016184099262fd7cee46f3d35fec3568c45c62f87e37f7f665f766b1f74",
    "1.80.1": "sha256:907ff4b3ee7df57149ffee04f606e0a08b9b2ed3507f00a19cf3c9c0f74b7681",
    "1.85.1": "sha256:9f841bbe9e7d8e37ceb96ed907265a3a0df7f44e3737d0b100e7907a679acb36",
    "1.90.0": "sha256:7fa728f3678acf5980d5db70960cf8491aff9411976789086676bdf0c19db39e",
    "1.98.1": "sha256:4cd829461bd5c4d511c32e269da9cb8929223b666519d8004e35fc8d1d771ab7",
}

_EXACT = re.compile(r"^(\d+)\.(\d+)(?:\.(\d+))?$")
TOOLCHAIN_FILES = ("rust-toolchain", "rust-toolchain.toml")

FileReader = Callable[[str], str | None]


class ToolchainError(ValueError):
    """The toolchain cannot be chosen or has no pinned image."""


@dataclass(frozen=True)
class Toolchain:
    version: str
    source: str
    reason: str


def _key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def release_date(version: str) -> date:
    for name, day in STABLE_RELEASES:
        if name == version:
            return date.fromisoformat(day)
    raise ToolchainError(f"unknown stable release: {version}")


def newest_stable_before(when: date) -> str:
    """Newest stable release published strictly before ``when``."""
    candidates = [v for v, day in STABLE_RELEASES if date.fromisoformat(day) < when]
    if not candidates:
        raise ToolchainError(f"no stable Rust release before {when.isoformat()}")
    return max(candidates, key=_key)


def normalize_version(channel: str) -> str | None:
    """``1.39`` -> newest ``1.39.x``; ``1.39.0`` -> itself; anything else -> None."""
    match = _EXACT.match(channel)
    if match is None:
        return None
    if match.group(3) is not None:
        release_date(channel)
        return channel
    prefix = f"{match.group(1)}.{match.group(2)}."
    patch_releases = [v for v, _ in STABLE_RELEASES if v.startswith(prefix)]
    if not patch_releases:
        raise ToolchainError(f"unknown stable release: {channel}")
    return max(patch_releases, key=_key)


def parse_toolchain_file(text: str) -> str:
    """Channel named by a rust-toolchain(.toml) file (TOML or a bare channel line)."""
    if "[toolchain]" in text:
        data = tomllib.loads(text)
        channel = data.get("toolchain", {}).get("channel")
        if not isinstance(channel, str):
            raise ToolchainError("toolchain file has no [toolchain] channel")
        return channel.strip()
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    raise ToolchainError("empty toolchain file")


def resolve_toolchain(read_base: FileReader, commit_time: datetime) -> Toolchain:
    commit_day = commit_time.date()
    for name in TOOLCHAIN_FILES:
        text = read_base(name)
        if text is None:
            continue
        channel = parse_toolchain_file(text)
        exact = normalize_version(channel)
        if exact is not None:
            return Toolchain(exact, name, f"{name} pins {channel}")
        if channel != "stable":
            raise ToolchainError(
                f"{name} asks for {channel!r}; only stable versions are supported so far"
            )
        break
    version = newest_stable_before(commit_day)
    return Toolchain(
        version,
        "release-date",
        f"newest stable before {commit_day.isoformat()} "
        f"({version} released {release_date(version).isoformat()})",
    )


def base_image(version: str, digests: dict[str, str] = IMAGE_DIGESTS) -> str:
    """Digest-pinned image reference for ``version`` from the offline table."""
    digest = digests.get(version)
    if digest is None:
        raise ToolchainError(
            f"no pinned digest for rust:{version}-slim; pass --image rust:{version}-slim@sha256:..."
        )
    return f"rust:{version}-slim@{digest}"

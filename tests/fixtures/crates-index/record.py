"""Record crates.io sparse index files for tests, keeping only the fields CargoRewind reads.

Usage:
  uv run python tests/fixtures/crates-index/record.py OUT_DIR CRATE [CRATE ...]
  uv run python tests/fixtures/crates-index/record.py OUT_DIR --from-cache CACHE_DIR [--minimal]

The second form copies every crate a live run fetched into its cache (the files under
CACHE_DIR/crates-index), so a replayed demo sees exactly the data the live run saw.
``--minimal`` also drops dev dependencies and each dependency's target and optional
fields, which the pin loop never reads (the demo index is a third of the size).

Each file keeps the sparse index layout (``ho/me/home``). Per version it keeps name,
vers, deps (name, req, kind, optional, target, package), yanked, pubtime and
rust_version, and drops checksums and feature maps, which are large and unused.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

from cargorewind.crateindex import INDEX_URL, USER_AGENT, index_path

DEP_KEYS = ("name", "req", "kind", "optional", "target", "package")
KEYS = ("name", "vers", "deps", "yanked", "pubtime", "rust_version")


MINIMAL_DEP_KEYS = ("name", "req", "kind", "package")


def trim(line: str, minimal: bool = False) -> str:
    raw = json.loads(line)
    deps = raw.get("deps", [])
    if minimal:
        deps = [d for d in deps if d.get("kind") != "dev"]
        raw["deps"] = [
            {k: d[k] for k in MINIMAL_DEP_KEYS if k in d and (k, d[k]) != ("kind", "normal")}
            for d in deps
        ]
    else:
        raw["deps"] = [{k: d[k] for k in DEP_KEYS if k in d} for d in deps]
    return json.dumps({k: raw[k] for k in KEYS if k in raw}, separators=(",", ":"))


def write(out: str, crate: str, lines: list[str], minimal: bool = False) -> None:
    target = Path(out) / index_path(crate)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("".join(trim(line, minimal) + "\n" for line in lines if line.strip()))
    print(f"{target}: {len(lines)} versions")


def main(out: str, crates: list[str]) -> None:
    for crate in crates:
        request = urllib.request.Request(
            f"{INDEX_URL}/{index_path(crate)}", headers={"User-Agent": USER_AGENT}
        )
        with urllib.request.urlopen(request, timeout=30) as resp:
            write(out, crate, resp.read().decode().splitlines())


def from_cache(out: str, cache: str, minimal: bool) -> None:
    for path in sorted((Path(cache) / "crates-index").rglob("*.json")):
        lines = json.loads(path.read_text())["body"].splitlines()
        write(out, path.name.removesuffix(".json"), lines, minimal)


if __name__ == "__main__":
    if sys.argv[2:3] == ["--from-cache"]:
        from_cache(sys.argv[1], sys.argv[3], "--minimal" in sys.argv[4:])
    else:
        main(sys.argv[1], sys.argv[2:])

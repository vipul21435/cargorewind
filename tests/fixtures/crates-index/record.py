"""Record crates.io sparse index files for tests, keeping only the fields CargoRewind reads.

Usage: uv run python tests/fixtures/crates-index/record.py OUT_DIR CRATE [CRATE ...]

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


def trim(line: str) -> str:
    raw = json.loads(line)
    raw["deps"] = [{k: d[k] for k in DEP_KEYS if k in d} for d in raw.get("deps", [])]
    return json.dumps({k: raw[k] for k in KEYS if k in raw}, separators=(",", ":"))


def main(out: str, crates: list[str]) -> None:
    for crate in crates:
        request = urllib.request.Request(
            f"{INDEX_URL}/{index_path(crate)}", headers={"User-Agent": USER_AGENT}
        )
        with urllib.request.urlopen(request, timeout=30) as resp:
            lines = resp.read().decode().splitlines()
        target = Path(out) / index_path(crate)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("".join(trim(line) + "\n" for line in lines if line.strip()))
        print(f"{target}: {len(lines)} versions")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])

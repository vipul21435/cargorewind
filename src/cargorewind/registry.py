"""Resolve ``rust:<version>-slim`` to a digest-pinned reference.

Offline (the default), only the reviewed table in ``toolchain.IMAGE_DIGESTS`` is used.
With the registry enabled, the resolver reads a JSON cache first, then asks the Docker
Hub registry (HTTP API v2) with a HEAD request for the tag's manifest. The answer's
``Docker-Content-Digest`` header is the multi-arch index digest, and a HEAD request does
not count as a pull. When the registry cannot answer, the offline table is the fallback.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from cargorewind.toolchain import IMAGE_DIGESTS, ToolchainError

REGISTRY_URL = "https://registry-1.docker.io"
AUTH_URL = "https://auth.docker.io/token"
AUTH_SERVICE = "registry.docker.io"
REPOSITORY = "library/rust"
MANIFEST_TYPES = (
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
)
INDEX_TYPES = MANIFEST_TYPES[:2]
CACHE_SCHEMA = 1
CACHE_FILE = "image-digests.json"
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")  # always fullmatch: "$" accepts a final newline


class RegistryError(RuntimeError):
    """The registry did not give a usable digest."""


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str]  # lower-cased names
    body: bytes = b""


class HttpClient(Protocol):
    def request(self, method: str, url: str, headers: Mapping[str, str]) -> HttpResponse: ...


def _lower(headers: Any) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in headers.items()} if headers else {}


class UrllibClient:
    """``HttpClient`` over urllib; HTTP error statuses are returned, not raised."""

    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout

    def request(self, method: str, url: str, headers: Mapping[str, str]) -> HttpResponse:
        req = urllib.request.Request(url, method=method, headers=dict(headers))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return HttpResponse(resp.status, _lower(resp.headers), resp.read())
        except urllib.error.HTTPError as exc:
            with exc:
                return HttpResponse(exc.code, _lower(exc.headers), exc.read())
        except (urllib.error.URLError, OSError) as exc:
            host = urllib.parse.urlsplit(url).netloc
            reason = getattr(exc, "reason", exc)
            raise RegistryError(f"cannot reach {host}: {reason}") from exc


@dataclass(frozen=True)
class RegistryAnswer:
    digest: str
    media_type: str
    url: str

    @property
    def multi_arch(self) -> bool:
        return self.media_type in INDEX_TYPES


class RegistryClient:
    """Docker registry HTTP API v2 client for one repository (anonymous pull token)."""

    def __init__(
        self,
        http: HttpClient,
        registry: str = REGISTRY_URL,
        auth: str | None = AUTH_URL,
        repository: str = REPOSITORY,
    ) -> None:
        self.http = http
        self.registry = registry.rstrip("/")
        self.auth = auth
        self.repository = repository
        self._token: str | None = None

    def _authorization(self) -> dict[str, str]:
        if self.auth is None:
            return {}
        if self._token is None:
            query = urllib.parse.urlencode(
                {"service": AUTH_SERVICE, "scope": f"repository:{self.repository}:pull"}
            )
            resp = self.http.request("GET", f"{self.auth}?{query}", {})
            if resp.status != 200:
                raise RegistryError(f"token request failed with HTTP {resp.status}")
            try:
                data = json.loads(resp.body)
                token = data.get("token") or data.get("access_token")
            except (ValueError, AttributeError) as exc:
                raise RegistryError("token response is not JSON") from exc
            if not isinstance(token, str) or not token:
                raise RegistryError("token response has no token")
            self._token = token
        return {"Authorization": f"Bearer {self._token}"}

    def digest(self, tag: str) -> RegistryAnswer:
        """Digest of ``<repository>:<tag>`` from a HEAD request for its manifest."""
        url = f"{self.registry}/v2/{self.repository}/manifests/{tag}"
        headers = {"Accept": ", ".join(MANIFEST_TYPES), **self._authorization()}
        resp = self.http.request("HEAD", url, headers)
        if resp.status == 404:
            raise RegistryError(f"{self.repository}:{tag} does not exist")
        if resp.status != 200:
            raise RegistryError(f"{self.repository}:{tag}: HTTP {resp.status} from the registry")
        digest = resp.headers.get("docker-content-digest", "")
        if _DIGEST.fullmatch(digest) is None:
            raise RegistryError(f"{self.repository}:{tag}: no valid Docker-Content-Digest")
        media_type = resp.headers.get("content-type", "").split(";")[0].strip()
        return RegistryAnswer(digest, media_type, url)


def default_cache_dir() -> Path:
    """``$CARGOREWIND_CACHE_DIR``, else ``$XDG_CACHE_HOME/cargorewind``, else ~/.cache."""
    explicit = os.environ.get("CARGOREWIND_CACHE_DIR")
    if explicit:
        return Path(explicit)
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "cargorewind"


class DigestCache:
    """Registry answers kept in one JSON file, so a tag is looked up once."""

    def __init__(self, directory: Path) -> None:
        self.path = directory / CACHE_FILE
        self.note = ""

    def _load(self) -> dict[str, dict[str, str]]:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            self.note = f"unreadable cache {self.path} ignored ({exc})"
            return {}
        if not isinstance(data, dict) or data.get("schema_version") != CACHE_SCHEMA:
            self.note = f"cache {self.path} has another schema; ignored"
            return {}
        images = data.get("images")
        return images if isinstance(images, dict) else {}

    def get(self, image: str) -> dict[str, str] | None:
        entry = self._load().get(image)
        if isinstance(entry, dict) and _DIGEST.fullmatch(str(entry.get("digest", ""))):
            return entry
        return None

    def put(self, image: str, answer: RegistryAnswer, resolved_at: datetime) -> bool:
        """Store ``answer``; False (with ``note`` set) when the cache cannot be written.

        The cache is only an optimization, so a read-only or misplaced cache directory
        never costs the registry's answer.
        """
        images = self._load()
        images[image] = {
            "digest": answer.digest,
            "media_type": answer.media_type,
            "url": answer.url,
            "resolved_at": resolved_at.isoformat(timespec="seconds"),
        }
        document = {"schema_version": CACHE_SCHEMA, "images": dict(sorted(images.items()))}
        partial = self.path.with_suffix(f".{os.getpid()}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            partial.write_text(json.dumps(document, indent=2) + "\n")
            os.replace(partial, self.path)
        except OSError as exc:
            self.note = f"cache {self.path} not written ({exc})"
            with contextlib.suppress(OSError):
                partial.unlink(missing_ok=True)
            return False
        return True


@dataclass(frozen=True)
class ImageChoice:
    reference: str  # rust:<version>-slim@sha256:...
    source: str  # offline-table, cache, registry, override
    reason: str

    def as_dict(self) -> dict[str, str]:
        return {"reference": self.reference, "source": self.source, "reason": self.reason}


class ImageResolver:
    """Pick the digest of ``rust:<version>-slim``; see the module docstring for the order."""

    def __init__(
        self,
        table: Mapping[str, str] = IMAGE_DIGESTS,
        registry: RegistryClient | None = None,
        cache: DigestCache | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.table = table
        self.registry = registry
        self.cache = cache
        self.now = now

    def resolve(self, version: str) -> ImageChoice:
        name = f"rust:{version}-slim"
        pinned = self.table.get(version)
        if self.registry is None:
            if pinned is None:
                raise ToolchainError(
                    f"no pinned digest for {name}; pass --registry to look it up "
                    f"or --image {name}@sha256:..."
                )
            return ImageChoice(f"{name}@{pinned}", "offline-table", "offline digest table")
        if self.cache is not None:
            hit = self.cache.get(name)
            if hit is not None:
                reason = (
                    f"registry answer cached {hit.get('resolved_at', '?')} in {self.cache.path}"
                )
                return ImageChoice(f"{name}@{hit['digest']}", "cache", reason)
        try:
            answer = self.registry.digest(f"{version}-slim")
        except RegistryError as exc:
            if pinned is None:
                raise ToolchainError(
                    f"{name}: {exc}, and the offline table has no digest; "
                    f"pass --image {name}@sha256:..."
                ) from exc
            return ImageChoice(
                f"{name}@{pinned}", "offline-table", f"registry lookup failed ({exc}); fallback"
            )
        reason = f"HEAD {answer.url} ({answer.media_type or 'no content type'})"
        if not answer.multi_arch:
            reason += "; single-platform manifest"
        if pinned is not None:
            same = pinned == answer.digest
            reason += "; matches the offline table" if same else "; differs from the offline table"
        if self.cache is not None and not self.cache.put(name, answer, self.now()):
            reason += f"; {self.cache.note}"
        return ImageChoice(f"{name}@{answer.digest}", "registry", reason)


def make_resolver(registry: bool, cache_dir: Path | None = None) -> ImageResolver:
    """The resolver the CLI uses: offline table, or cache + Docker Hub + table fallback."""
    if not registry:
        return ImageResolver()
    return ImageResolver(
        registry=RegistryClient(UrllibClient()),
        cache=DigestCache(cache_dir or default_cache_dir()),
    )

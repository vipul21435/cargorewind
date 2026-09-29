from __future__ import annotations

import json
import socket
import threading
from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from cargorewind.registry import (
    CACHE_FILE,
    DigestCache,
    HttpResponse,
    ImageResolver,
    RegistryAnswer,
    RegistryClient,
    RegistryError,
    UrllibClient,
    default_cache_dir,
    make_resolver,
)
from cargorewind.toolchain import IMAGE_DIGESTS, ToolchainError

FIXTURES = Path(__file__).parent / "fixtures" / "registry"
DIGEST_1390 = IMAGE_DIGESTS["1.39.0"]
OTHER = "sha256:" + "c" * 64
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def recorded(name: str) -> HttpResponse:
    data = json.loads((FIXTURES / name).read_text())
    body = json.dumps(data["body"]).encode() if "body" in data else b""
    return HttpResponse(data["status"], data["headers"], body)


class FakeHttp:
    """Answers by (method, url path) from recorded responses and logs every request."""

    def __init__(self, answers: dict[tuple[str, str], HttpResponse]) -> None:
        self.answers = answers
        self.requests: list[tuple[str, str, dict[str, str]]] = []

    def request(self, method: str, url: str, headers: Mapping[str, str]) -> HttpResponse:
        self.requests.append((method, url, dict(headers)))
        path = url.split("://", 1)[-1].split("/", 1)[-1].split("?", 1)[0]
        answer = self.answers.get((method, path))
        if answer is None:
            raise RegistryError(f"cannot reach test: no answer for {method} {path}")
        return answer


def docker_hub(**extra: HttpResponse) -> FakeHttp:
    answers = {
        ("GET", "token"): recorded("token.json"),
        ("HEAD", "v2/library/rust/manifests/1.39.0-slim"): recorded("rust-1.39.0-slim.head.json"),
        ("HEAD", "v2/library/rust/manifests/0.0.1-slim"): recorded("rust-0.0.1-slim.head.json"),
    }
    answers.update({("HEAD", f"v2/library/rust/manifests/{k}"): v for k, v in extra.items()})
    return FakeHttp(answers)


# Registry client


def test_registry_client_reads_the_index_digest_from_a_head_request() -> None:
    http = docker_hub()
    client = RegistryClient(http)
    answer = client.digest("1.39.0-slim")
    assert answer.digest == DIGEST_1390
    assert answer.multi_arch
    assert answer.url == "https://registry-1.docker.io/v2/library/rust/manifests/1.39.0-slim"
    token_call, head_call = http.requests
    assert token_call[0] == "GET"
    assert "scope=repository%3Alibrary%2Frust%3Apull" in token_call[1]
    assert head_call[0] == "HEAD"
    assert head_call[2]["Authorization"] == "Bearer fake-anonymous-pull-token"
    assert "application/vnd.oci.image.index.v1+json" in head_call[2]["Accept"]
    client.digest("1.39.0-slim")
    assert [r[0] for r in http.requests] == ["GET", "HEAD", "HEAD"]  # token reused


def test_registry_client_errors() -> None:
    client = RegistryClient(docker_hub())
    with pytest.raises(RegistryError, match="does not exist"):
        client.digest("0.0.1-slim")
    teapot = HttpResponse(418, {})
    with pytest.raises(RegistryError, match="HTTP 418"):
        RegistryClient(docker_hub(**{"1.40.0-slim": teapot})).digest("1.40.0-slim")
    no_digest = HttpResponse(200, {"content-type": "application/json"})
    with pytest.raises(RegistryError, match="no valid Docker-Content-Digest"):
        RegistryClient(docker_hub(**{"1.40.0-slim": no_digest})).digest("1.40.0-slim")


@pytest.mark.parametrize(
    ("token", "match"),
    [
        (HttpResponse(503, {}), "HTTP 503"),
        (HttpResponse(200, {}, b"<html>"), "not JSON"),
        (HttpResponse(200, {}, b"[]"), "not JSON"),
        (HttpResponse(200, {}, b'{"token": ""}'), "no token"),
    ],
)
def test_registry_token_errors(token: HttpResponse, match: str) -> None:
    http = docker_hub()
    http.answers[("GET", "token")] = token
    with pytest.raises(RegistryError, match=match):
        RegistryClient(http).digest("1.39.0-slim")


def test_registry_without_auth_and_single_platform_manifest() -> None:
    single = HttpResponse(
        200,
        {
            "docker-content-digest": OTHER,
            "content-type": "application/vnd.oci.image.manifest.v1+json; charset=utf-8",
        },
    )
    http = FakeHttp({("HEAD", "v2/library/rust/manifests/1.40.0-slim"): single})
    answer = RegistryClient(http, registry="http://mirror.test/", auth=None).digest("1.40.0-slim")
    assert answer == RegistryAnswer(
        OTHER,
        "application/vnd.oci.image.manifest.v1+json",
        "http://mirror.test/v2/library/rust/manifests/1.40.0-slim",
    )
    assert not answer.multi_arch
    assert "Authorization" not in http.requests[0][2]


# A real HTTP round trip against a local server (no network)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        if self.path.startswith("/token"):
            body = b'{"token": "local-token"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_HEAD(self) -> None:
        wanted = self.headers.get("Authorization") == "Bearer local-token"
        if self.path == "/v2/library/rust/manifests/1.39.0-slim" and wanted:
            self.send_response(200)
            self.send_header(
                "Content-Type", "application/vnd.docker.distribution.manifest.list.v2+json"
            )
            self.send_header("Docker-Content-Digest", DIGEST_1390)
            self.end_headers()
        else:
            self.send_response(404 if wanted else 401)
            self.end_headers()


@pytest.fixture
def local_registry() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_urllib_client_against_a_local_registry(local_registry: str) -> None:
    client = RegistryClient(UrllibClient(timeout=5), local_registry, f"{local_registry}/token")
    assert client.digest("1.39.0-slim").digest == DIGEST_1390
    with pytest.raises(RegistryError, match="does not exist"):
        client.digest("9.9.9-slim")
    bare = UrllibClient(timeout=5).request("GET", f"{local_registry}/missing", {})
    assert bare.status == 404


def test_urllib_client_reports_unreachable_hosts() -> None:
    with socket.socket() as sock:  # a port that nothing listens on once closed
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    with pytest.raises(RegistryError, match=f"cannot reach 127.0.0.1:{port}"):
        UrllibClient(timeout=2).request("HEAD", f"http://127.0.0.1:{port}/v2/", {})


# Cache


def test_cache_round_trip_and_atomic_file(tmp_path: Path) -> None:
    cache = DigestCache(tmp_path / "cache")
    assert cache.get("rust:1.39.0-slim") is None
    answer = RegistryAnswer(DIGEST_1390, "application/vnd.oci.image.index.v1+json", "u")
    cache.put("rust:1.39.0-slim", answer, NOW)
    cache.put("rust:1.40.0-slim", RegistryAnswer(OTHER, "t", "u2"), NOW)
    assert cache.get("rust:1.39.0-slim") == {
        "digest": DIGEST_1390,
        "media_type": "application/vnd.oci.image.index.v1+json",
        "url": "u",
        "resolved_at": "2026-09-30T12:00:00+00:00",
    }
    document = json.loads((tmp_path / "cache" / CACHE_FILE).read_text())
    assert document["schema_version"] == 1
    assert list(document["images"]) == ["rust:1.39.0-slim", "rust:1.40.0-slim"]
    assert [p.name for p in (tmp_path / "cache").iterdir()] == [CACHE_FILE]


@pytest.mark.parametrize(
    ("content", "note"),
    [
        ("{not json", "unreadable cache"),
        ('{"schema_version": 99, "images": {}}', "another schema"),
        ('{"schema_version": 1, "images": []}', ""),
        ('{"schema_version": 1, "images": {"rust:1.39.0-slim": {"digest": "md5:x"}}}', ""),
    ],
)
def test_cache_ignores_bad_files(tmp_path: Path, content: str, note: str) -> None:
    (tmp_path / CACHE_FILE).write_text(content)
    cache = DigestCache(tmp_path)
    assert cache.get("rust:1.39.0-slim") is None
    assert note in cache.note


def test_default_cache_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CARGOREWIND_CACHE_DIR", str(tmp_path / "explicit"))
    assert default_cache_dir() == tmp_path / "explicit"
    monkeypatch.delenv("CARGOREWIND_CACHE_DIR")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert default_cache_dir() == tmp_path / "xdg" / "cargorewind"
    monkeypatch.delenv("XDG_CACHE_HOME")
    assert default_cache_dir() == Path.home() / ".cache" / "cargorewind"


# Resolver


def test_offline_resolver_uses_only_the_table() -> None:
    choice = ImageResolver().resolve("1.39.0")
    assert choice.reference == f"rust:1.39.0-slim@{DIGEST_1390}"
    assert (choice.source, choice.reason) == ("offline-table", "offline digest table")
    with pytest.raises(ToolchainError, match="--registry"):
        ImageResolver().resolve("1.40.0")


def test_registry_answer_is_cached_and_reused(tmp_path: Path) -> None:
    http = docker_hub()
    cache = DigestCache(tmp_path)
    resolver = ImageResolver(registry=RegistryClient(http), cache=cache, now=lambda: NOW)
    first = resolver.resolve("1.39.0")
    assert first.source == "registry"
    assert first.reference == f"rust:1.39.0-slim@{DIGEST_1390}"
    assert first.reason.endswith(
        "(application/vnd.docker.distribution.manifest.list.v2+json); matches the offline table"
    )
    second = resolver.resolve("1.39.0")
    assert second.source == "cache"
    assert second.reference == first.reference
    assert "cached 2026-09-30T12:00:00+00:00" in second.reason
    assert len(http.requests) == 2  # token + one HEAD; the second lookup hit the cache


def test_registry_fills_gaps_and_flags_differences() -> None:
    newer = HttpResponse(
        200,
        {"docker-content-digest": OTHER, "content-type": "application/vnd.oci.image.index.v1+json"},
    )
    resolver = ImageResolver(registry=RegistryClient(docker_hub(**{"1.40.0-slim": newer})))
    gap = resolver.resolve("1.40.0")
    assert (gap.source, gap.reference) == ("registry", f"rust:1.40.0-slim@{OTHER}")
    table = {"1.40.0": DIGEST_1390}
    moved = ImageResolver(table, RegistryClient(docker_hub(**{"1.40.0-slim": newer})))
    assert moved.resolve("1.40.0").reason.endswith("differs from the offline table")


def test_single_platform_answer_is_flagged() -> None:
    single = HttpResponse(
        200,
        {
            "docker-content-digest": OTHER,
            "content-type": "application/vnd.docker.distribution.manifest.v2+json",
        },
    )
    resolver = ImageResolver(registry=RegistryClient(docker_hub(**{"1.41.0-slim": single})))
    assert "; single-platform manifest" in resolver.resolve("1.41.0").reason


def test_registry_failure_falls_back_to_the_table() -> None:
    down = FakeHttp({})
    resolver = ImageResolver(registry=RegistryClient(down))
    choice = resolver.resolve("1.39.0")
    assert choice.source == "offline-table"
    assert choice.reason.startswith("registry lookup failed (cannot reach test")
    assert choice.reason.endswith("; fallback")
    with pytest.raises(ToolchainError, match="offline table has no digest"):
        resolver.resolve("1.40.0")


def test_make_resolver(tmp_path: Path) -> None:
    offline = make_resolver(False)
    assert offline.registry is None and offline.cache is None
    online = make_resolver(True, tmp_path)
    assert online.registry is not None
    assert online.cache is not None and online.cache.path == tmp_path / CACHE_FILE

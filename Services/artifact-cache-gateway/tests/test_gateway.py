from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from artifact_cache_gateway.core.config import (
    ConfigError,
    GatewayConfig,
    SourceConfig,
    default_config,
    load_config,
)
from artifact_cache_gateway.core.ecosystems.cargo import (
    rewrite_cargo_sparse_config,
    rewrite_rustup_init,
)
from artifact_cache_gateway.core.ecosystems.dart import (
    rewrite_dart_pub_metadata,
)
from artifact_cache_gateway.core.ecosystems.julia import (
    rewrite_julia_registries,
)
from artifact_cache_gateway.core.ecosystems.node import rewrite_npm_metadata
from artifact_cache_gateway.core.source_naming import (
    apt_gateway_path,
    download_gateway_path,
    frozen_download_relative_path,
)
from artifact_cache_gateway.gateway.engine import Gateway
from artifact_cache_gateway.gateway.fetcher import (
    Fetcher,
    FetchError,
    FetchResult,
    SafeRedirectHandler,
)
from artifact_cache_gateway.gateway.inventory import cache_object
from artifact_cache_gateway.gateway.request_stats import request_module_for_ecosystem
from artifact_cache_gateway.gateway.rewrites import rewrite_index
from artifact_cache_gateway.gateway.server import GatewayHTTPServer, parse_range
from artifact_cache_gateway.gateway.status import status_document
from artifact_cache_gateway.harbor_tasks.preparer.direct_download import (
    refresh_downloads,
    warm_downloads,
)
from artifact_cache_gateway.storage.base import CacheEntry
from artifact_cache_gateway.storage.gpfs import FileStorage
from artifact_cache_gateway.ui.webui import webui_asset

WHEEL = b"fake-pytorch-wheel-content"
NPM_TARBALL = b"fake-npm-tarball-content"
FALLBACK_OBJECT = b"fallback-object-content"
DART_ARCHIVE = b"\x1f\x8bfake-dart-archive"
JULIA_OBJECT = b"\x1f\x8bfake-julia-object"
JULIA_UUID = "23338594-aafe-5451-b93e-139f81909106"
JULIA_HASH = "a" * 40
JULIA_REDIRECT_HASH = "b" * 40


class UpstreamHandler(BaseHTTPRequestHandler):
    counters: dict[str, int] = {}
    link_origin = "https://download-r2.example"

    def do_GET(self) -> None:
        type(self).counters[self.path] = type(self).counters.get(self.path, 0) + 1
        request_path = self.path.partition("?")[0]
        if self.path == "/whl/cpu/torch/":
            if self.headers.get("If-None-Match") == '"index-v1"':
                self.send_response(304)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            digest = hashlib.sha256(WHEEL).hexdigest()
            body = (
                f'<a href="{self.link_origin}/whl/cpu/'
                f'torch-1.0-cp310-cp310-manylinux_x86_64.whl#sha256={digest}">'
                "torch-1.0-cp310-cp310-manylinux_x86_64.whl</a>"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("ETag", '"index-v1"')
            self.end_headers()
            self.wfile.write(body)
            return
        if request_path == "/whl/cpu/torch-1.0-cp310-cp310-manylinux_x86_64.whl":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(WHEEL)))
            self.end_headers()
            self.wfile.write(WHEEL)
            return
        if self.path == "/pnpm":
            body = json.dumps(
                {
                    "name": "pnpm",
                    "dist-tags": {"latest": "1.0.0"},
                    "versions": {
                        "1.0.0": {
                            "name": "pnpm",
                            "version": "1.0.0",
                            "dist": {
                                "tarball": (
                                    f"http://127.0.0.1:{self.server.server_port}"
                                    "/pnpm/-/pnpm-1.0.0.tgz"
                                )
                            },
                        }
                    },
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/pnpm/-/pnpm-1.0.0.tgz":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(NPM_TARBALL)))
            self.end_headers()
            self.wfile.write(NPM_TARBALL)
            return
        if self.path in {"/dart/api/packages/melos", "/dart/api/packages/badpkg"}:
            archive_url = (
                "https://untrusted.example/melos.tar.gz"
                if self.path.endswith("badpkg")
                else (
                    f"http://127.0.0.1:{self.server.server_port}"
                    "/dart-archives/api/archives/melos-1.0.0.tar.gz"
                )
            )
            body = json.dumps(
                {
                    "name": self.path.rsplit("/", 1)[-1],
                    "latest": {
                        "version": "1.0.0",
                        "archive_url": archive_url,
                        "archive_sha256": hashlib.sha256(DART_ARCHIVE).hexdigest(),
                        "pubspec": {"name": "melos", "version": "1.0.0"},
                    },
                    "versions": [],
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.pub.v2+json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/dart-archives/api/archives/melos-1.0.0.tar.gz":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(DART_ARCHIVE)))
            self.end_headers()
            self.wfile.write(DART_ARCHIVE)
            return
        if self.path == "/julia/registries":
            body = f"/registry/{JULIA_UUID}/{JULIA_HASH}\n".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == f"/julia/registry/{JULIA_UUID}/{JULIA_HASH}":
            self.send_response(302)
            self.send_header(
                "Location", f"/julia/registry/{JULIA_UUID}/{JULIA_REDIRECT_HASH}"
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path in {
            f"/julia/registry/{JULIA_UUID}/{JULIA_REDIRECT_HASH}",
            f"/julia/package/{JULIA_UUID}/{JULIA_HASH}",
            f"/julia/artifact/{JULIA_HASH}",
        }:
            self.send_response(200)
            self.send_header("Content-Type", "binary/octet-stream")
            self.send_header("Content-Length", str(len(JULIA_OBJECT)))
            self.end_headers()
            self.wfile.write(JULIA_OBJECT)
            return
        if self.path in {"/primary/object", "/bad-a/object", "/bad-b/object"}:
            self.send_response(503)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/redirect/object":
            self.send_response(302)
            self.send_header("Location", "https://untrusted.example/object")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/secondary/object":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(FALLBACK_OBJECT)))
            self.end_headers()
            self.wfile.write(FALLBACK_OBJECT)
            return
        if self.path == "/slow/object":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(FALLBACK_OBJECT)))
            self.end_headers()
            threading.Event().wait(0.03)
            self.wfile.write(FALLBACK_OBJECT)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        pass


class ConfigTest(unittest.TestCase):
    def test_frozen_download_source_allows_any_path_but_no_query(self) -> None:
        source = SourceConfig.from_dict(
            {
                "name": "frozen-download-example",
                "kind": "frozen-download",
                "ecosystem": "download",
                "base_url": "https://example.com/",
                "allowed_redirect_origins": [],
                "allow_query": False,
                "proxy_mode": "configured",
            }
        )

        self.assertEqual(source.build_url("root"), "https://example.com/")
        self.assertEqual(
            source.build_url(frozen_download_relative_path("/@root")),
            "https://example.com/@root",
        )
        self.assertEqual(
            source.build_url(frozen_download_relative_path("/a%20b")),
            "https://example.com/a%20b",
        )
        with self.assertRaises(ConfigError):
            source.build_url(
                frozen_download_relative_path("/a"), "token=secret"
            )

    def test_transparent_download_supports_query_and_redirect(
        self,
    ) -> None:
        source = SourceConfig.from_dict(
            {
                "name": "download",
                "kind": "transparent-download",
                "ecosystem": "download",
                "base_url": "https://example.com/",
                "allow_query": True,
                "proxy_mode": "configured",
            }
        )
        relative = frozen_download_relative_path("/a%20b")
        self.assertEqual(
            source.build_url(relative, "version=2"),
            "https://example.com/a%20b?version=2",
        )
        self.assertTrue(
            source.allows_upstream_url(
                source.primary_upstream,
                "https://cdn.example.net/object?signature=opaque",
                candidate_url="https://example.com/a%20b?version=2",
            )
        )

    def test_versioned_route_reuses_cached_object_without_origin_config(self) -> None:
        source = SourceConfig.from_dict(
            {
                "name": "apt-objects-example",
                "kind": "static-objects",
                "ecosystem": "apt",
                "base_url": "https://example.com/",
                "allowed_exact_paths": ["tool.tar.gz"],
                "allow_query": False,
            }
        )
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        storage = FileStorage(Path(temporary.name))
        content = b"legacy-cached-object"
        entry = CacheEntry(
            url=source.build_url("tool.tar.gz"),
            digest=hashlib.sha256(content).hexdigest(),
            size=len(content),
            content_type="application/gzip",
            fetched_at=1,
        )
        stream, temp_path = storage.create_temp()
        with stream:
            stream.write(content)
        storage.publish(entry, temp_path)
        gateway = Gateway(
            GatewayConfig(sources={source.name: source}),
            storage,
            Fetcher(storage, None, 2, 1024),
            10**12,
        )
        relative_route = download_gateway_path(
            "https://example.com/tool.tar.gz"
        ).removeprefix("/v1/cache/download/")

        result = gateway.resolve_download_route(relative_route, "")

        self.assertEqual(result.state, "HIT")
        self.assertEqual(result.source.name, "download")

    def test_static_source_root_alias_maps_only_to_origin_root(self) -> None:
        source = SourceConfig.from_dict(
            {
                "name": "root-installer",
                "kind": "static-objects",
                "ecosystem": "download",
                "base_url": "https://get.example/",
                "allowed_redirect_origins": [],
                "allowed_exact_paths": ["@root"],
                "allow_query": False,
                "proxy_mode": "configured",
            }
        )

        self.assertEqual(source.build_url("@root"), "https://get.example/")
        self.assertEqual(
            source.fetch_candidates("@root"),
            ((source.primary_upstream, "https://get.example/"),),
        )
        self.assertTrue(
            source.allows_upstream_url(
                source.primary_upstream,
                "https://get.example/",
                candidate_url="https://get.example/",
            )
        )
        with self.assertRaises(ConfigError):
            source.build_url("anything-else")

    def test_webui_uses_explicit_high_contrast_light_palette(self) -> None:
        styles = webui_asset("/ui/styles.css")
        self.assertIsNotNone(styles)
        self.assertIn(b"color-scheme: light", styles.body)
        self.assertIn(b"--bg: #f4f6f8", styles.body)
        self.assertIn(b"--text: #172033", styles.body)
        self.assertNotIn(b"prefers-color-scheme", styles.body)

    def test_webui_groups_sources_by_explicit_ecosystem(self) -> None:
        index = webui_asset("/ui/")
        app = webui_asset("/ui/app.js")
        self.assertIsNotNone(index)
        self.assertIsNotNone(app)
        self.assertIn(b'id="source-groups"', index.body)
        self.assertIn(b'group.className = "source-group"', app.body)
        self.assertIn(b"group.dataset.ecosystem = ecosystem", app.body)
        self.assertIn(b'apt: "APT"', app.body)
        self.assertIn(b'pip: "pip / PyPI"', app.body)
        self.assertIn(b'node: "Node / npm"', app.body)
        self.assertIn(b'dart: "Dart Pub"', app.body)
        self.assertIn(b'julia: "Julia Pkg Server"', app.body)
        self.assertIn(b'download: "Direct downloads"', app.body)

    def test_webui_shows_every_request_module_separately(self) -> None:
        index = webui_asset("/ui/")
        app = webui_asset("/ui/app.js")
        self.assertIsNotNone(index)
        self.assertIsNotNone(app)
        self.assertIn(b'id="request-modules"', index.body)
        self.assertIn(b"renderModules", app.body)
        self.assertIn(b"counters.sources", app.body)
        self.assertIn(b"APT repository requests", app.body)
        self.assertIn(b"curl / wget direct downloads", app.body)
        self.assertIn(b"sourceLabels.get(source) || source", app.body)
        self.assertNotIn(b'document.createElement("a")', app.body)
        self.assertIn(b"GitHub counts as a single data source", index.body)
        self.assertIn(b"Not recorded by data source before upgrade", app.body)
        for module in (
            b"apt", b"pypi", b"npm", b"go", b"rust", b"dart", b"julia",
            b"curl", b"git_clone",
        ):
            self.assertIn(module, app.body)

    def test_webui_shows_source_config_update_metadata(self) -> None:
        app = webui_asset("/ui/app.js")
        self.assertIsNotNone(app)
        self.assertIn(b"Config updated", app.body)
        self.assertIn(b"Update policy", app.body)
        self.assertIn(b"Manual (admin)", app.body)
        self.assertIn(b"Expired", app.body)
        self.assertIn(b"review and update their config", app.body)

    def test_webui_explains_map_free_dynamic_apt_sources(self) -> None:
        app = webui_asset("/ui/app.js")
        self.assertIsNotNone(app)
        self.assertIn(b"auto-generates dynamic routes from the originating repository URL", app.body)
        self.assertIn(b"no need to register a source or maintain a mapping", app.body)
        self.assertNotIn(b"are not auto-registered", app.body)

    def test_status_exposes_plain_apt_url_display_names(self) -> None:
        repository = SourceConfig.from_dict(
            {
                "name": "apt-repo-example",
                "kind": "apt-repository",
                "ecosystem": "apt",
                "base_url": "https://packages.example/repository/",
                "allowed_path_prefixes": ["dists/", "pool/"],
                "mutable_path_prefixes": ["dists/"],
                "metadata_ttl_seconds": 300,
            }
        )
        key = SourceConfig.from_dict(
            {
                "name": "apt-objects-example",
                "kind": "static-objects",
                "ecosystem": "apt",
                "base_url": "https://packages.example/",
                "allowed_exact_paths": ["keys/repository.gpg"],
            }
        )
        with tempfile.TemporaryDirectory() as temporary:
            gateway = Gateway(
                GatewayConfig(
                    sources={item.name: item for item in (repository, key)}
                ),
                FileStorage(Path(temporary)),
                MagicMock(),
                300,
            )
            details = status_document(gateway)["source_details"]

        self.assertEqual(
            {item["name"]: item["display_name"] for item in details},
            {
                "apt-objects-example": (
                    "https://packages.example/keys/repository.gpg"
                ),
                "apt-repo-example": "https://packages.example/repository",
            },
        )

    def test_rebench_v2_bazel_key_uses_live_official_origin(self) -> None:
        config_path = Path(__file__).resolve().parents[1] / "config" / "sources.json"
        source = load_config(config_path).source("apt-objects-releases-bazel-build")
        self.assertEqual(source.base_url, "https://releases.bazel.build/")
        self.assertEqual(source.allowed_exact_paths, frozenset({"bazel-release.pub.gpg"}))

    def test_checked_in_sources_are_hashless_and_have_update_metadata(self) -> None:
        config_path = Path(__file__).resolve().parents[1] / "config" / "sources.json"
        config = load_config(config_path)
        for source in config.sources.values():
            self.assertNotRegex(source.name, r"-[0-9a-f]{8}$")
            expected_updated_at = {
                "go-proxy": "2026-09-15",
                "go-sumdb": "2026-09-15",
                "npm-registry": "2026-09-15",
                "pytorch": "2026-09-09",
            }.get(source.name, "2026-09-07")
            self.assertEqual(source.config_updated_at, expected_updated_at)
            self.assertEqual(source.config_update_policy, "manual")
            self.assertIsNone(source.config_expires_at)

    def test_source_config_supports_manual_and_expiring_review(self) -> None:
        manual = SourceConfig.from_dict(
            {
                "name": "manual-source",
                "base_url": "https://example.com/",
                "config_updated_at": "2026-09-07",
                "config_update_policy": "manual",
            }
        )
        self.assertEqual(manual.config_updated_at, "2026-09-07")
        expiring = SourceConfig.from_dict(
            {
                "name": "expiring-source",
                "base_url": "https://example.com/",
                "config_updated_at": "2026-09-07",
                "config_update_policy": "expires",
                "config_expires_at": "2026-12-01",
            }
        )
        self.assertEqual(expiring.config_expires_at, "2026-12-01")
        with self.assertRaisesRegex(ConfigError, "expires policy requires config_updated_at and config_expires_at"):
            SourceConfig.from_dict(
                {
                    "name": "invalid-expiry",
                    "base_url": "https://example.com/",
                    "config_update_policy": "expires",
                }
            )
        expired = SourceConfig.from_dict(
            {
                "name": "expired-source",
                "base_url": "https://example.com/",
                "config_updated_at": "2020-01-01",
                "config_update_policy": "expires",
                "config_expires_at": "2020-02-01",
            }
        )
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        storage = FileStorage(Path(temporary.name))
        gateway = Gateway(
            GatewayConfig(sources={expired.name: expired}),
            storage,
            Fetcher(storage, None, 2, 1024),
            300,
        )
        detail = status_document(gateway)["source_details"][0]
        self.assertEqual(detail["config_update_status"], "expired")

    def test_every_supported_ecosystem_maps_to_a_request_module(self) -> None:
        self.assertEqual(
            {
                ecosystem: request_module_for_ecosystem(ecosystem)
                for ecosystem in (
                    "apt", "pip", "node", "go", "cargo", "dart", "julia",
                    "download", "generic",
                )
            },
            {
                "apt": "apt",
                "pip": "pypi",
                "node": "npm",
                "go": "go",
                "cargo": "rust",
                "dart": "dart",
                "julia": "julia",
                "download": "curl",
                "generic": "curl",
            },
        )

    def test_webui_shows_sanitized_recent_failures(self) -> None:
        index = webui_asset("/ui/")
        app = webui_asset("/ui/app.js")
        self.assertIsNotNone(index)
        self.assertIsNotNone(app)
        self.assertIn(b'id="recent-failures"', index.body)
        self.assertIn(b"renderFailures", app.body)
        self.assertIn(b"attempt.http_status", app.body)

    def test_webui_distinguishes_strict_s3_hits_bypass_and_cache_admission(self) -> None:
        index = webui_asset("/ui/")
        app = webui_asset("/ui/app.js")
        self.assertIsNotNone(index)
        self.assertIsNotNone(app)
        self.assertIn(b"Strict S3 hit rate", index.body)
        self.assertIn(b"S3 reuse rate", index.body)
        self.assertIn(b'id="cache-fills"', index.body)
        self.assertIn(b'id="upstream-attempts"', index.body)
        self.assertIn(b'"bypass"', app.body)
        self.assertIn(b"requests.upstream_attempts", app.body)
        self.assertIn(b'[["success",', app.body)
        self.assertIn(b'["failure",', app.body)
        self.assertIn(b"configured_proxy", app.body)

    def test_webui_user_visible_copy_is_english(self) -> None:
        index = webui_asset("/ui/")
        app = webui_asset("/ui/app.js")
        self.assertIsNotNone(index)
        self.assertIsNotNone(app)
        for text in (
            "Artifact Cache Gateway",
            "Request cache states",
            "Domestic sources and Proxy results",
            "Objects written to S3",
            "Recent failures",
            "Configured sources",
            "Cached objects",
        ):
            self.assertIn(text.encode(), index.body)
        for text in (
            b"Request cache states",
            b"Recent failures",
            b"Cached objects",
            b">Refresh<",
            b"Loading cached objects",
        ):
            self.assertIn(text, index.body)
        self.assertIn(b"No upstream failures recorded in the current process.", app.body)

    def test_webui_renders_lazy_exact_object_inventory(self) -> None:
        index = webui_asset("/ui/")
        app = webui_asset("/ui/app.js")
        self.assertIsNotNone(index)
        self.assertIsNotNone(app)
        self.assertIn(b'id="object-inventory"', index.body)
        self.assertIn(b'page.level === "artifacts"', app.body)
        self.assertIn(b"item.python_tag", app.body)
        self.assertIn(b"item.abi_tag", app.body)
        self.assertIn(b"item.platform_tag", app.body)
        self.assertIn(b"item.label || item.key", app.body)

    def test_inventory_parser_preserves_wheel_and_deb_dimensions(self) -> None:
        wheel_source = SourceConfig.from_dict(
            {
                "name": "pypi-files",
                "base_url": "https://pypi.example/",
                "ecosystem": "pip",
            }
        )
        wheel_path = "packages/aa/torch-2.13.0-cp310-cp310-manylinux_2_28_x86_64.whl"
        wheel_url = wheel_source.build_url(wheel_path)
        wheel = cache_object(
            wheel_source,
            wheel_path,
            CacheEntry(
                url=wheel_url,
                digest="a" * 64,
                size=10,
                content_type="application/octet-stream",
                fetched_at=1,
            ),
        )
        self.assertEqual(wheel.package, "torch")
        self.assertEqual(wheel.version, "2.13.0")
        self.assertEqual(wheel.python_tag, "cp310")
        self.assertEqual(wheel.abi_tag, "cp310")
        self.assertEqual(wheel.platform_tag, "manylinux_2_28_x86_64")
        self.assertEqual(wheel.architecture, "x86_64")

        apt_source = SourceConfig.from_dict(
            {
                "name": "ubuntu",
                "kind": "apt-repository",
                "base_url": "https://apt.example/ubuntu/",
                "allowed_path_prefixes": ["dists/", "pool/"],
                "mutable_path_prefixes": ["dists/"],
                "metadata_ttl_seconds": 300,
            }
        )
        deb_path = "pool/main/c/curl/curl_7.81.0-1ubuntu1.20_amd64.deb"
        deb = cache_object(
            apt_source,
            deb_path,
            CacheEntry(
                url=apt_source.build_url(deb_path),
                digest="b" * 64,
                size=20,
                content_type="application/vnd.debian.binary-package",
                fetched_at=2,
            ),
        )
        self.assertEqual((deb.package, deb.version, deb.architecture), (
            "curl", "7.81.0-1ubuntu1.20", "amd64"
        ))

    def test_existing_file_cache_metadata_can_backfill_inventory(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        storage = FileStorage(Path(temporary.name))
        source = SourceConfig.from_dict(
            {
                "name": "pypi-files",
                "base_url": "https://pypi.example/",
                "ecosystem": "pip",
            }
        )
        path = "packages/aa/demo-1.0-cp310-cp310-manylinux_x86_64.whl"
        content = b"wheel"
        entry = CacheEntry(
            url=source.build_url(path),
            digest=hashlib.sha256(content).hexdigest(),
            size=len(content),
            content_type="application/octet-stream",
            fetched_at=1,
        )
        stream, temp_path = storage.create_temp()
        with stream:
            stream.write(content)
        storage.publish(entry, temp_path)
        gateway = Gateway(
            GatewayConfig(sources={source.name: source}),
            storage,
            Fetcher(storage, None, 2, 1024),
            300,
        )
        self.assertEqual(storage.list_inventory((), None, 10).children, ())
        self.assertEqual(
            gateway.backfill_inventory(),
            {
                "metadata": 1,
                "matched": 1,
                "unmatched": 0,
                "excluded": 0,
                "errors": 0,
                "sources": {
                    "pypi-files": {
                        "matched": 1,
                        "excluded": 0,
                        "errors": 0,
                    }
                },
                "error_reasons": {},
            },
        )
        self.assertEqual(storage.list_inventory((), None, 10).children, ("pip",))

    def test_backfill_excludes_metadata_outside_current_allowlist(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        storage = FileStorage(Path(temporary.name))
        content = b"old signing key"
        entry = CacheEntry(
            url="https://keys.example/old.asc",
            digest=hashlib.sha256(content).hexdigest(),
            size=len(content),
            content_type="application/octet-stream",
            fetched_at=1,
        )
        stream, temp_path = storage.create_temp()
        with stream:
            stream.write(content)
        storage.publish(entry, temp_path)
        source = SourceConfig.from_dict(
            {
                "name": "vendor-key",
                "kind": "static-objects",
                "ecosystem": "apt",
                "base_url": "https://keys.example/",
                "allowed_exact_paths": ["current.asc"],
            }
        )
        gateway = Gateway(
            GatewayConfig(sources={source.name: source}),
            storage,
            Fetcher(storage, None, 2, 1024),
            300,
        )
        report = gateway.backfill_inventory()
        self.assertEqual(report["metadata"], 1)
        self.assertEqual(report["excluded"], 1)
        self.assertEqual(report["errors"], 0)
        self.assertEqual(storage.list_inventory((), None, 10).children, ())

    def test_inventory_leaf_rechecks_current_source_allowlist(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        storage = FileStorage(Path(temporary.name))
        old_source = SourceConfig.from_dict(
            {
                "name": "vendor-key",
                "kind": "static-objects",
                "ecosystem": "apt",
                "base_url": "https://keys.example/",
                "allowed_exact_paths": ["old.asc"],
            }
        )
        entry = CacheEntry(
            url=old_source.build_url("old.asc"),
            digest="a" * 64,
            size=10,
            content_type="application/octet-stream",
            fetched_at=1,
        )
        storage.ensure_inventory(cache_object(old_source, "old.asc", entry))
        current_source = SourceConfig.from_dict(
            {
                "name": "vendor-key",
                "kind": "static-objects",
                "ecosystem": "apt",
                "base_url": "https://keys.example/",
                "allowed_exact_paths": ["current.asc"],
            }
        )
        gateway = Gateway(
            GatewayConfig(sources={current_source.name: current_source}),
            storage,
            Fetcher(storage, None, 2, 1024),
            300,
        )
        page = gateway.inventory_document(
            ecosystem="apt",
            source_name="vendor-key",
            package="@keys",
            version="@unversioned",
        )
        self.assertEqual(page["items"], [])

    def test_legacy_hashed_inventory_uses_canonical_source_identity(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        storage = FileStorage(Path(temporary.name))
        legacy_source = SourceConfig.from_dict(
            {
                "name": "download-objects-example-com-1234abcd",
                "kind": "static-objects",
                "ecosystem": "download",
                "base_url": "https://example.com/",
                "allowed_exact_paths": ["tool-1.2.3.tar.gz"],
            }
        )
        relative_path = "tool-1.2.3.tar.gz"
        entry = CacheEntry(
            url=legacy_source.build_url(relative_path),
            digest="a" * 64,
            size=10,
            content_type="application/gzip",
            fetched_at=1,
        )
        record = cache_object(legacy_source, relative_path, entry)
        storage.ensure_inventory(record)
        stale_source = SourceConfig.from_dict(
            {
                "name": "download-objects-stale-example-87654321",
                "kind": "static-objects",
                "ecosystem": "download",
                "base_url": "https://stale.example/",
                "allowed_exact_paths": ["stale.tar.gz"],
            }
        )
        stale_entry = CacheEntry(
            url=stale_source.build_url("stale.tar.gz"),
            digest="b" * 64,
            size=10,
            content_type="application/gzip",
            fetched_at=1,
        )
        storage.ensure_inventory(
            cache_object(stale_source, "stale.tar.gz", stale_entry)
        )
        current_source = SourceConfig.from_dict(
            {
                "name": "download-objects-example-com",
                "kind": "static-objects",
                "ecosystem": "download",
                "base_url": "https://example.com/",
                "allowed_exact_paths": [relative_path],
            }
        )
        gateway = Gateway(
            GatewayConfig(sources={current_source.name: current_source}),
            storage,
            Fetcher(storage, None, 2, 1024),
            300,
        )

        sources = gateway.inventory_document(ecosystem="download")
        self.assertEqual(
            sources["items"],
            [
                {
                    "key": legacy_source.name,
                    "label": current_source.name,
                }
            ],
        )
        packages = gateway.inventory_document(
            ecosystem="download", source_name=legacy_source.name
        )
        self.assertEqual(
            packages["items"],
            [{"key": record.package, "label": record.package}],
        )
        versions = gateway.inventory_document(
            ecosystem="download",
            source_name=legacy_source.name,
            package=record.package,
        )
        self.assertEqual(
            versions["items"],
            [{"key": record.version, "label": record.version}],
        )
        artifacts = gateway.inventory_document(
            ecosystem="download",
            source_name=legacy_source.name,
            package=record.package,
            version=record.version,
        )
        self.assertEqual(len(artifacts["items"]), 1)
        self.assertEqual(artifacts["items"][0]["source"], current_source.name)

        storage.ensure_inventory(cache_object(current_source, relative_path, entry))
        sources = gateway.inventory_document(ecosystem="download")
        self.assertEqual(
            sources["items"],
            [{"key": current_source.name, "label": current_source.name}],
        )

    def test_status_document_exposes_policy_without_upstream_urls(self) -> None:
        source = SourceConfig.from_dict(
            {
                "name": "private-route",
                "base_url": "https://user:sensitive-value@example.com/repository/",
                "proxy_mode": "direct",
            }
        )
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        storage = FileStorage(Path(temporary.name))
        gateway = Gateway(
            GatewayConfig(sources={source.name: source}),
            storage,
            Fetcher(storage, None, 2, 1024),
            300,
        )
        document = status_document(gateway)

        encoded = json.dumps(document)
        self.assertNotIn("sensitive-value", encoded)
        self.assertNotIn("example.com", encoded)
        self.assertEqual(
            document["source_details"],
            [
                {
                    "name": "private-route",
                    "route": "/v1/cache/private-route/",
                    "kind": "generic",
                    "ecosystem": "generic",
                    "proxy_mode": "direct",
                    "fallback_count": 0,
                    "upstream_chain": [
                        {
                            "position": 1,
                            "label": "primary",
                            "proxy_mode": "direct",
                            "attempt_timeout_seconds": None,
                            "slow_after_seconds": None,
                            "min_bytes_per_second": None,
                        }
                    ],
                    "metadata_ttl_seconds": None,
                    "allow_query": True,
                    "config_updated_at": None,
                    "config_update_policy": "manual",
                    "config_expires_at": None,
                    "config_update_status": "unknown",
                }
            ],
        )
        self.assertEqual(document["recent_failures"], [])

    def test_build_url_and_reject_path_traversal(self) -> None:
        source = SourceConfig.from_dict(
            {"name": "example", "base_url": "https://example.com/files"}
        )
        self.assertEqual(
            source.build_url("wheel.whl", "download=1"),
            "https://example.com/files/wheel.whl?download=1",
        )
        with self.assertRaises(ConfigError):
            source.build_url("%2e%2e/secret")

    def test_redirect_allowlist_is_exact_origin(self) -> None:
        source = SourceConfig.from_dict(
            {"name": "example", "base_url": "https://example.com/"}
        )
        self.assertTrue(source.allows_url("https://example.com/a"))
        self.assertFalse(source.allows_url("http://example.com/a"))
        self.assertFalse(source.allows_url("https://cdn.example.com/a"))

    def test_default_pytorch_source_rewrites_r2_links(self) -> None:
        source = default_config().source("pytorch")
        self.assertIn(
            "https://download-r2.pytorch.org", source.html_rewrite_origins
        )

    def test_apt_repository_allowlist_and_metadata_ttl(self) -> None:
        source = SourceConfig.from_dict(
            {
                "name": "apt-vendor",
                "kind": "apt-repository",
                "base_url": "https://repo.example/apt/ubuntu",
                "allowed_path_prefixes": ["dists/", "pool/"],
                "mutable_path_prefixes": ["dists/"],
                "metadata_ttl_seconds": 60,
                "allow_query": False,
            }
        )
        self.assertEqual(
            source.build_url("dists/jammy/InRelease"),
            "https://repo.example/apt/ubuntu/dists/jammy/InRelease",
        )
        self.assertEqual(
            source.freshness_ttl("dists/jammy/InRelease", "application/octet-stream", 300),
            60,
        )
        self.assertIsNone(
            source.freshness_ttl("pool/main/p/package.deb", "application/octet-stream", 300)
        )
        self.assertIsNone(
            source.freshness_ttl(
                "dists/jammy/vendor/binary-amd64/package_1.0_amd64.deb",
                "application/vnd.debian.binary-package",
                300,
            )
        )
        with self.assertRaises(ConfigError):
            source.build_url("private/secret")
        with self.assertRaises(ConfigError):
            source.build_url("dists/jammy/InRelease", "token=secret")
        self.assertFalse(source.allows_url("https://repo.example/private/secret"))

    def test_static_source_requires_exact_object_path(self) -> None:
        source = SourceConfig.from_dict(
            {
                "name": "apt-key",
                "kind": "static-objects",
                "base_url": "https://keys.example/",
                "allowed_exact_paths": ["vendor.asc"],
                "allow_query": False,
            }
        )
        self.assertEqual(
            source.build_url("vendor.asc"), "https://keys.example/vendor.asc"
        )
        with self.assertRaises(ConfigError):
            source.build_url("other.asc")

    def test_proxy_mode_defaults_to_configured_for_legacy_sources(self) -> None:
        source = SourceConfig.from_dict(
            {"name": "legacy", "base_url": "https://example.com/"}
        )
        self.assertEqual(source.proxy_mode, "configured")

    def test_invalid_proxy_mode_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "proxy_mode"):
            SourceConfig.from_dict(
                {
                    "name": "example",
                    "base_url": "https://example.com/",
                    "proxy_mode": "ambient",
                }
            )

    def test_upstream_attempt_policy_is_strict_and_ordered(self) -> None:
        source = SourceConfig.from_dict(
            {
                "name": "ordered",
                "base_url": "https://one.example/",
                "upstream_label": "domestic-one",
                "attempt_timeout_seconds": 15,
                "slow_after_seconds": 5,
                "min_bytes_per_second": 1024,
                "fallback_upstreams": [
                    {
                        "base_url": "https://two.example/",
                        "label": "domestic-two",
                        "proxy_mode": "direct",
                    },
                    {
                        "base_url": "https://official.example/",
                        "label": "official",
                        "proxy_mode": "configured",
                    },
                ],
            }
        )
        self.assertEqual(
            [item.label for item in (source.primary_upstream, *source.fallback_upstreams)],
            ["domestic-one", "domestic-two", "official"],
        )
        self.assertEqual(source.primary_upstream.attempt_timeout_seconds, 15)
        self.assertEqual(source.primary_upstream.slow_after_seconds, 5)
        self.assertEqual(source.primary_upstream.min_bytes_per_second, 1024)

        with self.assertRaisesRegex(
            ConfigError, "slow_after_seconds and min_bytes_per_second must be configured together"
        ):
            SourceConfig.from_dict(
                {
                    "name": "incomplete-policy",
                    "base_url": "https://example.com/",
                    "slow_after_seconds": 5,
                }
            )
        with self.assertRaisesRegex(ConfigError, "label"):
            SourceConfig.from_dict(
                {
                    "name": "duplicate-labels",
                    "base_url": "https://one.example/",
                    "upstream_label": "same",
                    "fallback_upstreams": [
                        {"base_url": "https://two.example/", "label": "same"}
                    ],
                }
            )

    def test_source_ecosystem_is_strict_and_apt_is_backward_compatible(self) -> None:
        apt = SourceConfig.from_dict(
            {
                "name": "legacy-apt",
                "kind": "apt-repository",
                "base_url": "https://example.com/apt/",
                "allowed_path_prefixes": ["dists/", "pool/"],
                "mutable_path_prefixes": ["dists/"],
                "metadata_ttl_seconds": 300,
            }
        )
        self.assertEqual(apt.ecosystem, "apt")
        with self.assertRaisesRegex(ConfigError, "ecosystem"):
            SourceConfig.from_dict(
                {
                    "name": "unknown-ecosystem",
                    "base_url": "https://example.com/",
                    "ecosystem": "rubygems",
                }
            )

    def test_distribution_sources_are_strict_direct_apt_repositories(self) -> None:
        config_path = Path(__file__).resolve().parents[1] / "config" / "sources.json"
        config = load_config(config_path)
        expected = {
            "ubuntu": "https://mirrors.tuna.tsinghua.edu.cn/ubuntu/",
            "debian": "https://mirrors.tuna.tsinghua.edu.cn/debian/",
            "debian-security": "https://mirrors.tuna.tsinghua.edu.cn/debian-security/",
        }
        for name, base_url in expected.items():
            source = config.source(name)
            self.assertEqual(source.base_url, base_url)
            self.assertEqual(source.proxy_mode, "direct")
            self.assertEqual(source.allowed_path_prefixes, ("dists/", "pool/"))
            self.assertEqual(source.mutable_path_prefixes, ("dists/",))
            self.assertFalse(source.allow_query)

        docker_ce = config.source("docker-ce")
        self.assertEqual(docker_ce.proxy_mode, "direct")
        self.assertEqual(
            docker_ce.allowed_exact_paths,
            frozenset({"linux/ubuntu/gpg", "linux/debian/gpg"}),
        )
        self.assertEqual(
            docker_ce.mutable_path_prefixes,
            ("linux/ubuntu/dists/", "linux/debian/dists/"),
        )
        with self.assertRaises(ConfigError):
            docker_ce.build_url("linux/ubuntu/private/object")

    def test_pypi_sources_use_domestic_primary_and_configured_fallback(self) -> None:
        config_path = Path(__file__).resolve().parents[1] / "config" / "sources.json"
        config = load_config(config_path)
        simple = config.source("pypi-simple")
        files = config.source("pypi-files")
        self.assertEqual(simple.ecosystem, "pip")
        self.assertEqual(simple.proxy_mode, "direct")
        self.assertEqual(len(simple.fallback_upstreams), 1)
        self.assertEqual(simple.fallback_upstreams[0].proxy_mode, "configured")
        self.assertFalse(simple.allow_query)
        self.assertTrue(simple.rewrite_html)
        self.assertEqual(
            simple.html_rewrite_routes,
            (
                ("https://files.pythonhosted.org", "pypi-files"),
                ("https://pypi.tuna.tsinghua.edu.cn", "pypi-files"),
            ),
        )
        self.assertEqual(
            simple.html_rewrite_relative_routes,
            (("packages/", "pypi-files"),),
        )
        self.assertEqual(files.ecosystem, "pip")
        self.assertEqual(files.proxy_mode, "direct")
        self.assertEqual(len(files.fallback_upstreams), 1)
        self.assertEqual(files.fallback_upstreams[0].proxy_mode, "configured")
        self.assertEqual(files.allowed_path_prefixes, ("packages/",))
        self.assertEqual(
            files.build_url("packages/aa/bb/example.whl"),
            "https://pypi.tuna.tsinghua.edu.cn/packages/aa/bb/example.whl",
        )
        with self.assertRaises(ConfigError):
            files.build_url("arbitrary/path")

        result = MagicMock(source=simple)
        rewritten = rewrite_index(
            b'<a href="https://files.pythonhosted.org/packages/aa/bb/example.whl">x</a>'
            b'<a href="https://pypi.tuna.tsinghua.edu.cn/packages/cc/dd/example.whl">y</a>'
            b'<a href="../../packages/ee/ff/example.whl">z</a>',
            result,
        )
        self.assertEqual(
            rewritten,
            b'<a href="/v1/cache/pypi-files/packages/aa/bb/example.whl">x</a>'
            b'<a href="/v1/cache/pypi-files/packages/cc/dd/example.whl">y</a>'
            b'<a href="/v1/cache/pypi-files/packages/ee/ff/example.whl">z</a>',
        )

    def test_pytorch_absolute_links_are_not_rewritten_twice(self) -> None:
        config_path = Path(__file__).resolve().parents[1] / "config" / "sources.json"
        source = load_config(config_path).source("pytorch")
        self.assertEqual(
            source.base_url, "https://mirrors.aliyun.com/pytorch-wheels/"
        )
        self.assertEqual(source.proxy_mode, "configured")
        self.assertEqual(source.allowed_path_prefixes, ())
        self.assertEqual(source.metadata_ttl_seconds, 60)
        self.assertEqual(
            source.flat_index_packages,
            frozenset({"torch", "torchaudio", "torchvision"}),
        )
        self.assertEqual(
            source.root_artifact_packages,
            frozenset({"setuptools", "triton"}),
        )
        candidates = source.fetch_candidates("cpu/torch/")
        self.assertEqual(
            candidates[0][1],
            "https://mirrors.aliyun.com/pytorch-wheels/cpu/",
        )
        self.assertEqual(
            candidates[1][1],
            "https://download.pytorch.org/whl/cpu/torch/",
        )
        cu129_candidates = source.fetch_candidates("cu129/torch/")
        self.assertEqual(
            cu129_candidates[0][1],
            "https://mirrors.aliyun.com/pytorch-wheels/cu129/",
        )
        self.assertEqual(
            cu129_candidates[1][1],
            "https://download.pytorch.org/whl/cu129/torch/",
        )
        for upstream, candidate_url in cu129_candidates:
            self.assertTrue(
                source.allows_upstream_url(
                    upstream,
                    candidate_url,
                    candidate_url=candidate_url,
                )
            )
        self.assertTrue(
            source.allows_upstream_url(
                cu129_candidates[1][0],
                "https://download-r2.pytorch.org/whl/cu129/torch/",
                candidate_url=cu129_candidates[1][1],
            )
        )
        self.assertFalse(
            source.allows_upstream_url(
                cu129_candidates[0][0],
                "https://download.pytorch.org/whl/cu129/torch/",
                candidate_url=cu129_candidates[0][1],
            )
        )
        self.assertEqual(
            source.fetch_candidates("cu130/torchaudio/")[0][1],
            "https://mirrors.aliyun.com/pytorch-wheels/cu130/",
        )
        self.assertEqual(
            source.build_url(
                "cu132/torch-2.0.0+cu132-cp312-cp312-manylinux_2_28_x86_64.whl"
            ),
            "https://mirrors.aliyun.com/pytorch-wheels/"
            "cu132/torch-2.0.0+cu132-cp312-cp312-manylinux_2_28_x86_64.whl",
        )
        self.assertEqual(
            source.fetch_candidates("cu999/torch/")[0][1],
            "https://mirrors.aliyun.com/pytorch-wheels/cu999/",
        )
        self.assertEqual(
            source.fetch_candidates("rocm7.2/torch/")[0][1],
            "https://mirrors.aliyun.com/pytorch-wheels/rocm7.2/",
        )
        self.assertEqual(
            source.fetch_candidates("xpu/torch/")[0][1],
            "https://mirrors.aliyun.com/pytorch-wheels/xpu/",
        )
        with self.assertRaises(ConfigError):
            source.build_url("cuda129/torch/")
        with self.assertRaises(ConfigError):
            source.build_url("nightly/torch/")
        self.assertEqual(
            source.fetch_candidates("cpu/fsspec/")[0][1],
            "https://download.pytorch.org/whl/cpu/fsspec/",
        )
        self.assertEqual(len(source.fetch_candidates("cpu/fsspec/")), 1)
        root_wheel = "setuptools-78.1.0-py3-none-any.whl"
        self.assertEqual(
            source.fetch_candidates(root_wheel)[0][1],
            f"https://download.pytorch.org/whl/{root_wheel}",
        )
        self.assertEqual(len(source.fetch_candidates(root_wheel)), 1)
        self.assertTrue(
            source.fetch_candidates(root_wheel)[0][0].allows_url(
                f"https://download.pytorch.org/whl/{root_wheel}"
            )
        )
        triton_wheel = (
            "triton-3.6.0-cp312-cp312-manylinux_2_27_x86_64."
            "manylinux_2_28_x86_64.whl"
        )
        triton_candidates = source.fetch_candidates(triton_wheel)
        self.assertEqual(len(triton_candidates), 1)
        self.assertEqual(
            triton_candidates[0][1],
            f"https://download.pytorch.org/whl/{triton_wheel}",
        )
        self.assertTrue(
            source.allows_upstream_url(
                triton_candidates[0][0],
                f"https://download-r2.pytorch.org/whl/{triton_wheel}",
                candidate_url=triton_candidates[0][1],
            )
        )
        with self.assertRaises(ConfigError):
            source.build_url("unreviewed-1.0-py3-none-any.whl")
        with self.assertRaises(ConfigError):
            source.build_url("whl/cpu/torch/")
        self.assertEqual(source.fallback_upstreams[0].proxy_mode, "configured")
        self.assertEqual(
            source.fallback_upstreams[0].base_url,
            "https://download.pytorch.org/whl/",
        )
        result = MagicMock(source=source)
        rewritten = rewrite_index(
            b'<a href="https://download-r2.pytorch.org/whl/cpu/torch.whl">x</a>'
            b'<a href="/pytorch-wheels/cpu/torchvision.whl">y</a>'
            b'<a href="torch-2.0.0&#43;cpu-cp310-cp310-linux_x86_64.whl">z</a>'
            b'<a href="https://files.pythonhosted.org/packages/aa/fsspec.whl">f</a>',
            result,
        )
        self.assertEqual(
            rewritten,
            b'<a href="/v1/cache/pytorch/cpu/torch.whl">x</a>'
            b'<a href="/v1/cache/pytorch/cpu/torchvision.whl">y</a>'
            b'<a href="../torch-2.0.0&#43;cpu-cp310-cp310-linux_x86_64.whl">z</a>'
            b'<a href="/v1/cache/pypi-files/packages/aa/fsspec.whl">f</a>',
        )

    def test_reviewed_sglang_and_flashinfer_release_sources_allow_any_version(
        self,
    ) -> None:
        config_path = Path(__file__).resolve().parents[1] / "config" / "sources.json"
        config = load_config(config_path)
        sglang = config.source("sglang-wheel-files")
        flashinfer = config.source("flashinfer-wheel-files")
        self.assertEqual(sglang.kind, "generic")
        self.assertEqual(sglang.ecosystem, "download")
        self.assertEqual(sglang.proxy_mode, "configured")
        self.assertEqual(sglang.allowed_path_prefixes, ())
        self.assertEqual(sglang.allowed_exact_paths, frozenset())
        self.assertEqual(
            sglang.build_url("v0.4.6/wheel.whl"),
            "https://github.com/sgl-project/whl/releases/download/"
            "v0.4.6/wheel.whl",
        )
        self.assertEqual(
            sglang.build_url("v99.0.0/future-wheel.whl"),
            "https://github.com/sgl-project/whl/releases/download/"
            "v99.0.0/future-wheel.whl",
        )
        self.assertEqual(
            flashinfer.build_url("v0.6.18/wheel.whl"),
            "https://github.com/flashinfer-ai/flashinfer/releases/download/"
            "v0.6.18/wheel.whl",
        )
        for source in (sglang, flashinfer):
            self.assertTrue(
                source.allows_url(
                    "https://release-assets.githubusercontent.com/"
                    "github-production-release-asset/example?sig=reviewed-upstream"
                )
            )
            with self.assertRaises(ConfigError):
                source.build_url("../other-project/wheel.whl")

    def test_rustup_init_allows_http_gateway_without_disabling_https(self) -> None:
        content = b"curl --proto '=https' URL\ncurl --proto \"=https\" URL\n"
        self.assertEqual(
            rewrite_rustup_init(content),
            b"curl --proto '=http,https' URL\n"
            b'curl --proto "=http,https" URL\n',
        )

    def test_npm_registry_source_is_fixed_and_rewrites_tarballs(self) -> None:
        config_path = Path(__file__).resolve().parents[1] / "config" / "sources.json"
        source = load_config(config_path).source("npm-registry")
        self.assertEqual(source.kind, "npm-registry")
        self.assertEqual(source.ecosystem, "node")
        self.assertEqual(source.proxy_mode, "configured")
        self.assertFalse(source.allow_query)
        self.assertEqual(
            source.allowed_redirect_origins,
            frozenset(
                {
                    "https://registry.npmmirror.com",
                    "https://cdn.npmmirror.com",
                }
            ),
        )
        self.assertEqual(len(source.fallback_upstreams), 1)
        self.assertEqual(source.fallback_upstreams[0].proxy_mode, "configured")
        self.assertEqual(
            source.build_url("@scope%2Fpackage/-/package-1.2.3.tgz"),
            "https://registry.npmmirror.com/@scope%2Fpackage/-/package-1.2.3.tgz",
        )
        for invalid in ("-/v1/search", "package/private/object", "../secret"):
            with self.subTest(invalid=invalid), self.assertRaises(ConfigError):
                source.build_url(invalid)
        with self.assertRaises(ConfigError):
            source.build_url("package", "write=true")

        result = MagicMock(source=source)
        rewritten = json.loads(
            rewrite_npm_metadata(
                json.dumps(
                    {
                        "dist": {
                            "tarball": "https://registry.npmmirror.com/pnpm/-/pnpm-10.33.1.tgz"
                        },
                        "fallback": "https://registry.npmjs.org/pnpm/-/pnpm-10.33.1.tgz",
                        "homepage": "https://example.com/project",
                    }
                ).encode(),
                result,
                "http://gateway.internal",
            )
        )
        expected = (
            "http://gateway.internal/v1/cache/npm-registry/"
            "pnpm/-/pnpm-10.33.1.tgz"
        )
        self.assertEqual(rewritten["dist"]["tarball"], expected)
        self.assertEqual(rewritten["fallback"], expected)
        self.assertEqual(rewritten["homepage"], "https://example.com/project")

    def test_dart_pub_sources_are_protocol_scoped_and_rewrite_archives(self) -> None:
        config_path = Path(__file__).resolve().parents[1] / "config" / "sources.json"
        config = load_config(config_path)
        metadata = config.source("dart-pub")
        archives = config.source("dart-pub-archives")
        self.assertEqual((metadata.kind, metadata.ecosystem), ("dart-pub", "dart"))
        self.assertEqual(metadata.proxy_mode, "direct")
        self.assertEqual(metadata.fallback_upstreams[0].proxy_mode, "configured")
        self.assertEqual(
            metadata.build_url("api/packages/melos"),
            "https://pub.flutter-io.cn/api/packages/melos",
        )
        self.assertEqual(
            metadata.build_url("api/packages/melos/versions/7.3.0"),
            "https://pub.flutter-io.cn/api/packages/melos/versions/7.3.0",
        )
        self.assertEqual(
            metadata.build_url("api/packages/melos/advisories"),
            "https://pub.flutter-io.cn/api/packages/melos/advisories",
        )
        self.assertEqual(
            archives.build_url("api/archives/melos-8.6.0.tar.gz"),
            (
                "https://storage.flutter-io.cn/dartlang-pub-exported-api/latest/"
                "api/archives/melos-8.6.0.tar.gz"
            ),
        )
        self.assertEqual(
            metadata.freshness_ttl("api/packages/melos", "application/json", 1),
            300,
        )
        self.assertIsNone(
            archives.freshness_ttl(
                "api/archives/melos-8.6.0.tar.gz",
                "application/octet-stream",
                1,
            )
        )
        for source, invalid in (
            (metadata, "api/packages/Bad-Name"),
            (metadata, "api/packages/melos/private"),
            (archives, "api/archives/../../secret"),
            (archives, "api/archives/melos.zip"),
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ConfigError):
                source.build_url(invalid)

        result = MagicMock(source=metadata)
        rewritten = json.loads(
            rewrite_dart_pub_metadata(
                json.dumps(
                    {
                        "latest": {
                            "archive_url": (
                                "https://storage.flutter-io.cn/"
                                "dartlang-pub-exported-api/latest/api/archives/"
                                "melos-8.6.0.tar.gz"
                            )
                        },
                        "versions": [
                            {
                                "archive_url": (
                                    "https://pub.dev/api/archives/"
                                    "melos-8.5.0.tar.gz"
                                )
                            }
                        ],
                        "homepage": "https://pub.dev/packages/melos",
                    }
                ).encode(),
                result,
                archives,
                "http://gateway.internal",
            )
        )
        route = "http://gateway.internal/v1/cache/dart-pub-archives/"
        self.assertEqual(
            rewritten["latest"]["archive_url"],
            route + "api/archives/melos-8.6.0.tar.gz",
        )
        self.assertEqual(
            rewritten["versions"][0]["archive_url"],
            route + "api/archives/melos-8.5.0.tar.gz",
        )
        self.assertEqual(rewritten["homepage"], "https://pub.dev/packages/melos")
        with self.assertRaises(ConfigError):
            rewrite_dart_pub_metadata(
                b'{"archive_url":"https://evil.example/object.tar.gz"}',
                result,
                archives,
                "http://gateway.internal",
            )

    def test_julia_pkg_source_is_content_addressed_and_redirect_scoped(self) -> None:
        config_path = Path(__file__).resolve().parents[1] / "config" / "sources.json"
        source = load_config(config_path).source("julia-pkg")
        registry = f"registry/{JULIA_UUID}/{JULIA_HASH}"
        package = f"package/{JULIA_UUID}/{JULIA_HASH}"
        artifact = f"artifact/{JULIA_HASH}"
        for path in ("registries", registry, package, artifact):
            with self.subTest(path=path):
                self.assertEqual(source.build_url(path), source.base_url + path)
        self.assertEqual(
            source.freshness_ttl("registries", "text/plain", 1), 300
        )
        self.assertIsNone(
            source.freshness_ttl(registry, "application/octet-stream", 1)
        )
        for invalid in (
            "registry/not-a-uuid/" + JULIA_HASH,
            f"package/{JULIA_UUID}/short",
            f"artifact/{JULIA_HASH}-old",
            "meta",
            "../registry",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ConfigError):
                source.build_url(invalid)
        candidate = source.build_url(registry)
        self.assertTrue(
            source.allows_upstream_url(
                source.primary_upstream,
                "https://storage.julialang.net/" + registry,
                candidate_url=candidate,
            )
        )
        self.assertFalse(
            source.allows_upstream_url(
                source.primary_upstream,
                "https://evil.example/" + registry,
                candidate_url=candidate,
            )
        )
        result = MagicMock(source=source)
        self.assertEqual(
            rewrite_julia_registries(
                (
                    f"/{registry}\n"
                    f"https://pkg.julialang.org/{registry}\n"
                    f"https://in.pkg.julialang.org/{registry}\n"
                ).encode(),
                result,
            ),
            f"/{registry}\n/{registry}\n/{registry}\n".encode(),
        )
        with self.assertRaises(ConfigError):
            rewrite_julia_registries(
                b"https://evil.example/registry/object\n", result
            )
        registry_entry = CacheEntry(
            url=source.build_url(registry),
            digest="c" * 64,
            size=1,
            content_type="application/octet-stream",
            fetched_at=1,
        )
        record = cache_object(source, registry, registry_entry)
        self.assertEqual(
            record.object_id,
            hashlib.sha256(source.build_url(registry).encode()).hexdigest(),
        )

    def test_go_and_rust_sources_are_protocol_scoped_with_proxy_fallbacks(self) -> None:
        config_path = Path(__file__).resolve().parents[1] / "config" / "sources.json"
        config = load_config(config_path)

        go = config.source("go-proxy")
        self.assertEqual(
            (go.kind, go.ecosystem, go.proxy_mode),
            ("go-proxy", "go", "configured"),
        )
        self.assertEqual(go.fallback_upstreams[0].proxy_mode, "configured")
        valid_go_paths = (
            "google.golang.org/protobuf/@v/list",
            "google.golang.org/protobuf/@v/v1.28.0.mod",
            "google.golang.org/protobuf/@v/v1.28.0.zip",
        )
        for path in valid_go_paths:
            with self.subTest(path=path):
                self.assertTrue(go.build_url(path).startswith("https://goproxy.cn/"))
        for path in ("search", "google.golang.org/protobuf/private", "../secret"):
            with self.subTest(path=path), self.assertRaises(ConfigError):
                go.build_url(path)

        sumdb = config.source("go-sumdb")
        self.assertEqual(
            (sumdb.kind, sumdb.ecosystem, sumdb.proxy_mode),
            ("go-sumdb", "go", "configured"),
        )
        self.assertEqual(sumdb.fallback_upstreams[0].proxy_mode, "configured")
        valid_sumdb_paths = (
            "latest",
            "lookup/google.golang.org/protobuf@v1.28.0",
            "tile/8/1/926.p/93",
            "tile/8/3/007",
            "tile/8/0/x237/154.p/172",
            "tile/8/data/x001/x234/067.p/1",
        )
        for path in valid_sumdb_paths:
            with self.subTest(path=path):
                self.assertTrue(
                    sumdb.build_url(path).startswith("https://sum.golang.google.cn/")
                )
        for path in (
            "supported",
            "tile/31/1/926",
            "tile/8/64/926",
            "tile/8/0/x000/154",
            "tile/8/0/1234",
            "tile/8/0/x237/154.p/256",
            "lookup/no-version",
            "../secret",
        ):
            with self.subTest(path=path), self.assertRaises(ConfigError):
                sumdb.build_url(path)

        rustup = config.source("rustup-init")
        self.assertEqual(rustup.build_url("rustup-init.sh"), "https://rsproxy.cn/rustup-init.sh")
        self.assertEqual(rustup.fallback_upstreams[0].proxy_mode, "configured")
        with self.assertRaises(ConfigError):
            rustup.build_url("arbitrary.sh")

    def test_cargo_sparse_config_routes_crates_back_through_gateway(self) -> None:
        config_path = Path(__file__).resolve().parents[1] / "config" / "sources.json"
        config = load_config(config_path)
        index = config.source("cargo-index")
        crates = config.source("cargo-crates")
        self.assertEqual(
            crates.allowed_redirect_origins,
            frozenset(
                {
                    "https://rsproxy.cn",
                    "https://lf3-static.rsproxy.cn",
                    "https://lf6-static.rsproxy.cn",
                    "https://lf9-static.rsproxy.cn",
                }
            ),
        )
        for path in ("config.json", "se/rd/serde", "3/l/log"):
            with self.subTest(path=path):
                index.build_url(path)
        for path in ("api/v1/search", "wrong/shard/serde"):
            with self.subTest(path=path), self.assertRaises(ConfigError):
                index.build_url(path)
        self.assertEqual(
            crates.build_url("serde/1.0.228/download"),
            "https://rsproxy.cn/api/v1/crates/serde/1.0.228/download",
        )
        with self.assertRaises(ConfigError):
            crates.build_url("serde/latest")

        rewritten = json.loads(
            rewrite_cargo_sparse_config(
                b'{"dl":"https://rsproxy.cn/api/v1/crates","api":"https://rsproxy.cn"}',
                "http://gateway.internal",
            )
        )
        self.assertEqual(
            rewritten["dl"],
            "http://gateway.internal/v1/cache/cargo-crates",
        )
        self.assertEqual(rewritten["api"], "https://rsproxy.cn")

    def test_inventory_parser_preserves_go_and_cargo_versions(self) -> None:
        config_path = Path(__file__).resolve().parents[1] / "config" / "sources.json"
        config = load_config(config_path)
        go = config.source("go-proxy")
        go_path = "google.golang.org/protobuf/@v/v1.28.0.zip"
        go_record = cache_object(
            go,
            go_path,
            CacheEntry(
                url=go.build_url(go_path),
                digest="d" * 64,
                size=123,
                content_type="application/zip",
                fetched_at=1.0,
            ),
        )
        self.assertEqual(
            (go_record.package, go_record.version),
            ("google.golang.org/protobuf", "v1.28.0"),
        )

        sumdb = config.source("go-sumdb")
        lookup_path = "lookup/google.golang.org/protobuf@v1.28.0"
        lookup_record = cache_object(
            sumdb,
            lookup_path,
            CacheEntry(
                url=sumdb.build_url(lookup_path),
                digest="a" * 64,
                size=234,
                content_type="text/plain",
                fetched_at=1.0,
            ),
        )
        self.assertEqual(
            (lookup_record.package, lookup_record.version, lookup_record.object_type),
            ("google.golang.org/protobuf", "v1.28.0", "metadata"),
        )

        crates = config.source("cargo-crates")
        crate_path = "serde/1.0.228/download"
        crate_record = cache_object(
            crates,
            crate_path,
            CacheEntry(
                url=crates.build_url(crate_path),
                digest="e" * 64,
                size=456,
                content_type="application/x-gzip",
                fetched_at=1.0,
            ),
        )
        self.assertEqual((crate_record.package, crate_record.version), ("serde", "1.0.228"))
        self.assertEqual(crate_record.filename, "serde-1.0.228.crate")

    def test_inventory_parser_preserves_npm_tarball_version(self) -> None:
        source = SourceConfig.from_dict(
            {
                "name": "npm-registry",
                "kind": "npm-registry",
                "ecosystem": "node",
                "base_url": "https://registry.npmmirror.com/",
                "metadata_ttl_seconds": 300,
                "allow_query": False,
            }
        )
        path = "@scope/package/-/package-1.2.3.tgz"
        record = cache_object(
            source,
            path,
            CacheEntry(
                url=source.build_url(path),
                digest="c" * 64,
                size=123,
                content_type="application/octet-stream",
                fetched_at=1.0,
                etag=None,
                last_modified=None,
            ),
        )
        self.assertEqual(record.package, "@scope/package")
        self.assertEqual(record.version, "1.2.3")
        self.assertEqual(record.object_type, "artifact")

    def test_html_rewrite_route_requires_a_configured_target_source(self) -> None:
        source = SourceConfig.from_dict(
            {
                "name": "simple",
                "base_url": "https://index.example/simple/",
                "rewrite_html": True,
                "html_rewrite_routes": {
                    "https://files.example": "missing-files"
                },
            }
        )
        with self.assertRaisesRegex(ConfigError, "target"):
            GatewayConfig(sources={source.name: source})


class FetcherProxyPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.fetcher = Fetcher(
            storage=FileStorage(Path(self.temp.name)),
            proxy_url="http://proxy.example:7890",
            timeout=2,
            max_object_bytes=1024,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def assert_proxy_policy(self, proxy_mode: str, expected: dict[str, str]) -> None:
        source = SourceConfig.from_dict(
            {
                "name": "example",
                "base_url": "https://example.com/",
                "proxy_mode": proxy_mode,
            }
        )
        with patch("artifact_cache_gateway.gateway.fetcher.build_opener") as opener:
            opener.side_effect = RuntimeError("stop after opener construction")
            with self.assertRaisesRegex(RuntimeError, "opener construction"):
                self.fetcher.fetch(source, source.build_url("object"))
        proxy_handler = opener.call_args.args[0]
        redirect_handler = opener.call_args.args[1]
        self.assertIsInstance(proxy_handler, ProxyHandler)
        self.assertEqual(proxy_handler.proxies, expected)
        self.assertIsInstance(redirect_handler, SafeRedirectHandler)
        self.assertIs(redirect_handler.source, source)
        self.assertEqual(redirect_handler.upstream.proxy_mode, proxy_mode)

    def test_same_origin_redirect_keeps_protocol_path_allowlist(self) -> None:
        source = SourceConfig.from_dict(
            {
                "name": "go-proxy",
                "base_url": "https://proxy.example/",
                "kind": "go-proxy",
                "ecosystem": "go",
                "allow_query": False,
                "metadata_ttl_seconds": 60,
            }
        )
        candidate_url = source.build_url("example.com/mod/@v/v1.0.0.mod")
        handler = SafeRedirectHandler(
            source,
            source.primary_upstream,
            candidate_url,
        )

        with self.assertRaisesRegex(FetchError, "outside source allowlist"):
            handler.redirect_request(
                None,
                None,
                302,
                "Found",
                {},
                "https://proxy.example/admin/debug",
            )

    def test_explicit_external_redirect_origin_remains_allowed(self) -> None:
        source = SourceConfig.from_dict(
            {
                "name": "npm",
                "base_url": "https://registry.example/",
                "allowed_redirect_origins": ["https://objects.example"],
                "kind": "npm-registry",
                "ecosystem": "node",
                "allow_query": False,
                "metadata_ttl_seconds": 60,
            }
        )
        candidate_url = source.build_url("pkg/-/pkg-1.0.0.tgz")
        self.assertTrue(
            source.allows_upstream_url(
                source.primary_upstream,
                "https://objects.example/blob/reviewed-token",
                candidate_url=candidate_url,
            )
        )
        self.assertTrue(
            source.allows_upstream_url(
                source.primary_upstream,
                "https://objects.example/blob/reviewed-token?signature=dynamic",
                candidate_url=candidate_url,
            )
        )

    def test_reviewed_static_candidate_allows_same_origin_canonical_redirect(self) -> None:
        source = SourceConfig.from_dict(
            {
                "name": "legacy-download",
                "base_url": "https://files.example/packages/",
                "allowed_redirect_origins": [],
                "allowed_exact_paths": ["source/tool-1.0.tar.gz"],
                "kind": "static-objects",
                "ecosystem": "download",
                "allow_query": False,
            }
        )
        candidate_url = source.build_url("source/tool-1.0.tar.gz")
        self.assertTrue(
            source.allows_upstream_url(
                source.primary_upstream,
                "https://files.example/packages/ab/cd/tool-1.0.tar.gz",
                candidate_url=candidate_url,
            )
        )
        self.assertFalse(
            source.allows_upstream_url(
                source.primary_upstream,
                "https://files.example/private/tool-1.0.tar.gz",
                candidate_url=candidate_url,
            )
        )

    def test_direct_source_does_not_pass_global_proxy_to_opener(self) -> None:
        self.assert_proxy_policy("direct", {})

    def test_configured_source_passes_global_proxy_to_opener(self) -> None:
        self.assert_proxy_policy(
            "configured",
            {
                "http": "http://proxy.example:7890",
                "https": "http://proxy.example:7890",
            },
        )

    def test_preferred_fallback_uses_fixed_configured_upstream_first(self) -> None:
        source = SourceConfig.from_dict(
            {
                "name": "example",
                "base_url": "https://mirror.example/",
                "proxy_mode": "direct",
                "fallback_upstreams": [
                    {
                        "base_url": "https://upstream.example/",
                        "proxy_mode": "configured",
                    }
                ],
            }
        )
        with patch("artifact_cache_gateway.gateway.fetcher.build_opener") as opener:
            opener.side_effect = RuntimeError("stop after opener construction")
            with self.assertRaisesRegex(RuntimeError, "opener construction"):
                self.fetcher.fetch(
                    source,
                    source.build_url("object"),
                    relative_path="object",
                    prefer_fallback=True,
                )
        proxy_handler = opener.call_args.args[0]
        self.assertEqual(
            proxy_handler.proxies,
            {
                "http": "http://proxy.example:7890",
                "https": "http://proxy.example:7890",
            },
        )

    def test_invalid_fallback_proxy_mode_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "fallback proxy_mode"):
            SourceConfig.from_dict(
                {
                    "name": "example",
                    "base_url": "https://mirror.example/",
                    "fallback_upstreams": [
                        {
                            "base_url": "https://upstream.example/",
                            "proxy_mode": "ambient",
                        }
                    ],
                }
            )

    def test_fallback_keeps_base_path_and_origin_boundary(self) -> None:
        source = SourceConfig.from_dict(
            {
                "name": "example",
                "base_url": "https://mirror.example/repo/",
                "fallback_upstreams": [
                    {"base_url": "https://upstream.example/repo/"}
                ],
            }
        )
        fallback = source.fallback_upstreams[0]
        self.assertTrue(fallback.allows_url("https://upstream.example/repo/pool/a"))
        self.assertFalse(fallback.allows_url("https://upstream.example/private/a"))
        self.assertFalse(fallback.allows_url("https://other.example/repo/pool/a"))


class RangeTest(unittest.TestCase):
    def test_normal_and_suffix_ranges(self) -> None:
        self.assertEqual(parse_range("bytes=2-5", 10), (2, 5))
        self.assertEqual(parse_range("bytes=-3", 10), (7, 9))
        self.assertEqual(parse_range("bytes=8-", 10), (8, 9))

    def test_invalid_range(self) -> None:
        with self.assertRaises(ValueError):
            parse_range("bytes=10-11", 10)
        with self.assertRaises(ValueError):
            parse_range("bytes=1-2,4-5", 10)


class GatewayIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        UpstreamHandler.counters = {}
        self.temp = tempfile.TemporaryDirectory()
        self.upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        self.upstream_thread = threading.Thread(
            target=self.upstream.serve_forever, daemon=True
        )
        self.upstream_thread.start()

        source = SourceConfig.from_dict(
            {
                "name": "pytorch",
                "base_url": f"http://127.0.0.1:{self.upstream.server_port}/",
                "html_rewrite_origins": [UpstreamHandler.link_origin],
                "rewrite_html": True,
                "ecosystem": "pip",
            }
        )
        npm_source = SourceConfig.from_dict(
            {
                "name": "npm-registry",
                "kind": "npm-registry",
                "ecosystem": "node",
                "base_url": f"http://127.0.0.1:{self.upstream.server_port}/",
                "metadata_ttl_seconds": 300,
                "allow_query": False,
            }
        )
        dart_source = SourceConfig.from_dict(
            {
                "name": "dart-pub",
                "kind": "dart-pub",
                "ecosystem": "dart",
                "base_url": f"http://127.0.0.1:{self.upstream.server_port}/dart/",
                "allowed_path_prefixes": ["api/packages/"],
                "metadata_ttl_seconds": 300,
                "allow_query": False,
            }
        )
        dart_archive_source = SourceConfig.from_dict(
            {
                "name": "dart-pub-archives",
                "kind": "dart-pub",
                "ecosystem": "dart",
                "base_url": (
                    f"http://127.0.0.1:{self.upstream.server_port}/dart-archives/"
                ),
                "allowed_path_prefixes": ["api/archives/", "packages/"],
                "allow_query": False,
            }
        )
        julia_source = SourceConfig.from_dict(
            {
                "name": "julia-pkg",
                "kind": "julia-pkg",
                "ecosystem": "julia",
                "base_url": f"http://127.0.0.1:{self.upstream.server_port}/julia/",
                "allowed_path_prefixes": ["registry/", "package/", "artifact/"],
                "allowed_exact_paths": ["registries"],
                "mutable_exact_paths": ["registries"],
                "metadata_ttl_seconds": 300,
                "allow_query": False,
            }
        )
        fallback_source = SourceConfig.from_dict(
            {
                "name": "fallback",
                "base_url": f"http://127.0.0.1:{self.upstream.server_port}/primary/",
                "upstream_label": "domestic-primary",
                "proxy_mode": "direct",
                "fallback_upstreams": [
                    {
                        "base_url": f"http://127.0.0.1:{self.upstream.server_port}/secondary/",
                        "label": "domestic-secondary",
                        "proxy_mode": "direct",
                    }
                ],
            }
        )
        redirect_source = SourceConfig.from_dict(
            {
                "name": "redirect-fallback",
                "base_url": f"http://127.0.0.1:{self.upstream.server_port}/redirect/",
                "upstream_label": "redirecting-primary",
                "proxy_mode": "direct",
                "fallback_upstreams": [
                    {
                        "base_url": f"http://127.0.0.1:{self.upstream.server_port}/secondary/",
                        "label": "safe-secondary",
                        "proxy_mode": "direct",
                    }
                ],
            }
        )
        slow_source = SourceConfig.from_dict(
            {
                "name": "slow-fallback",
                "base_url": f"http://127.0.0.1:{self.upstream.server_port}/slow/",
                "upstream_label": "slow-domestic",
                "proxy_mode": "direct",
                "slow_after_seconds": 0.01,
                "min_bytes_per_second": 100_000_000,
                "fallback_upstreams": [
                    {
                        "base_url": f"http://127.0.0.1:{self.upstream.server_port}/secondary/",
                        "label": "fast-secondary",
                        "proxy_mode": "direct",
                    }
                ],
            }
        )
        failed_source = SourceConfig.from_dict(
            {
                "name": "failed",
                "base_url": f"http://127.0.0.1:{self.upstream.server_port}/bad-a/",
                "upstream_label": "domestic",
                "proxy_mode": "direct",
                "fallback_upstreams": [
                    {
                        "base_url": f"http://127.0.0.1:{self.upstream.server_port}/bad-b/",
                        "label": "official",
                        "proxy_mode": "configured",
                    }
                ],
            }
        )
        frozen_download_source = SourceConfig.from_dict(
            {
                "name": "frozen-download-local",
                "kind": "frozen-download",
                "ecosystem": "download",
                "base_url": f"http://127.0.0.1:{self.upstream.server_port}/",
                "allowed_redirect_origins": [],
                "allow_query": False,
                "proxy_mode": "direct",
            }
        )
        config = GatewayConfig(
            sources={
                item.name: item
                for item in (
                    source,
                    npm_source,
                    dart_source,
                    dart_archive_source,
                    julia_source,
                    fallback_source,
                    redirect_source,
                    slow_source,
                    failed_source,
                    frozen_download_source,
                )
            }
        )
        storage = FileStorage(Path(self.temp.name))
        fetcher = Fetcher(
            storage=storage,
            proxy_url=None,
            timeout=2,
            max_object_bytes=1024 * 1024,
        )
        gateway = Gateway(
            config=config,
            storage=storage,
            fetcher=fetcher,
            index_ttl_seconds=3600,
            cache_mode="all",
        )
        self.gateway = GatewayHTTPServer(("127.0.0.1", 0), gateway)
        self.gateway_thread = threading.Thread(
            target=self.gateway.serve_forever, daemon=True
        )
        self.gateway_thread.start()
        self.origin = f"http://127.0.0.1:{self.gateway.server_port}"
        self.opener = build_opener(ProxyHandler({}))

    def tearDown(self) -> None:
        self.gateway.shutdown()
        self.gateway.server_close()
        self.upstream.shutdown()
        self.upstream.server_close()
        self.temp.cleanup()

    def request(self, path: str, headers=None, method: str = "GET"):
        request = Request(
            self.origin + path,
            headers=headers or {},
            method=method,
        )
        return self.opener.open(request, timeout=2)

    def test_versioned_download_route_caches_first_successful_object(self) -> None:
        upstream_url = (
            f"http://127.0.0.1:{self.upstream.server_port}/"
            "whl/cpu/torch-1.0-cp310-cp310-manylinux_x86_64.whl"
        )
        route = download_gateway_path(upstream_url)
        with self.request(route) as first:
            self.assertEqual(first.headers["X-Artifact-Cache"], "MISS")
            self.assertEqual(first.read(), WHEEL)
        with self.request(route) as second:
            self.assertEqual(second.headers["X-Artifact-Cache"], "HIT")
            self.assertEqual(second.read(), WHEEL)
        self.assertEqual(
            UpstreamHandler.counters[
                "/whl/cpu/torch-1.0-cp310-cp310-manylinux_x86_64.whl"
            ],
            1,
        )

    def test_versioned_download_route_accepts_query_without_origin_plan(self) -> None:
        upstream_url = (
            f"http://127.0.0.1:{self.upstream.server_port}/"
            "whl/cpu/torch-1.0-cp310-cp310-manylinux_x86_64.whl?version=2"
        )
        route = download_gateway_path(upstream_url)
        with self.request(route) as response:
            self.assertEqual(response.headers["X-Artifact-Cache"], "MISS")
            self.assertEqual(response.read(), WHEEL)
        self.assertEqual(
            UpstreamHandler.counters[
                "/whl/cpu/torch-1.0-cp310-cp310-manylinux_x86_64.whl?version=2"
            ],
            1,
        )
        with self.request("/v1/objects?ecosystem=download") as inventory:
            sources = json.loads(inventory.read())["items"]
        self.assertIn(
            {"key": "download", "label": "download"},
            sources,
        )

    def test_dynamic_apt_route_appends_repository_paths_without_source_config(
        self,
    ) -> None:
        source_url = (
            f"http://127.0.0.1:{self.upstream.server_port}/whl/cpu"
        )
        route = (
            apt_gateway_path(source_url)
            + "/torch-1.0-cp310-cp310-manylinux_x86_64.whl"
        )
        with self.request(route) as first:
            self.assertEqual(first.headers["X-Artifact-Cache"], "MISS")
            self.assertEqual(first.read(), WHEEL)
        with self.request(route) as second:
            self.assertEqual(second.headers["X-Artifact-Cache"], "HIT")
            self.assertEqual(second.read(), WHEEL)
        with self.request("/v1/objects?ecosystem=apt") as inventory:
            sources = json.loads(inventory.read())["items"]
        self.assertIn({"key": "apt", "label": "apt"}, sources)
        apt_stats = self.gateway.gateway.stats()["modules"]["apt"]["sources"]
        self.assertIn(source_url, apt_stats)
        self.assertEqual(apt_stats[source_url]["request_total"], 2)

    def test_proxy_only_bypasses_s3_for_successful_direct_upstream(self) -> None:
        root = Path(self.temp.name) / "proxy-only-direct"
        storage = FileStorage(root)
        source = SourceConfig.from_dict(
            {
                "name": "domestic",
                "base_url": f"http://127.0.0.1:{self.upstream.server_port}/",
                "proxy_mode": "direct",
            }
        )
        gateway = Gateway(
            GatewayConfig(sources={source.name: source}),
            storage,
            Fetcher(storage, "http://proxy.invalid:7890", 2, 1024 * 1024),
            300,
            cache_mode="proxy-only",
        )
        path = "whl/cpu/torch-1.0-cp310-cp310-manylinux_x86_64.whl"
        result = gateway.resolve(source.name, path, "")
        self.assertEqual(result.state, "BYPASS")
        self.assertIsNone(storage.load(source.build_url(path)))
        with gateway.open_result_blob(result, 0, result.entry.size - 1) as stream:
            self.assertEqual(stream.read(), WHEEL)
        gateway.release_result(result)
        self.assertFalse(result.temp_path.exists())
        stats = gateway.stats()
        self.assertEqual(stats["bypass"], 1)
        self.assertEqual(stats["cache_fills"]["direct"]["objects"], 0)
        self.assertEqual(
            stats["upstream_attempts"]["direct"],
            {"success": 1, "failure": 0},
        )

    def test_frozen_download_publishes_even_in_proxy_only_mode(self) -> None:
        root = Path(self.temp.name) / "proxy-only-frozen"
        storage = FileStorage(root)
        source = SourceConfig.from_dict(
            {
                "name": "frozen-download-direct",
                "kind": "frozen-download",
                "ecosystem": "download",
                "base_url": f"http://127.0.0.1:{self.upstream.server_port}/",
                "allow_query": False,
                "proxy_mode": "direct",
            }
        )
        gateway = Gateway(
            GatewayConfig(sources={source.name: source}),
            storage,
            Fetcher(storage, None, 2, 1024 * 1024),
            300,
            cache_mode="proxy-only",
        )
        upstream_url = (
            f"http://127.0.0.1:{self.upstream.server_port}/"
            "whl/cpu/torch-1.0-cp310-cp310-manylinux_x86_64.whl"
        )
        relative_route = download_gateway_path(upstream_url).removeprefix(
            "/v1/cache/download/"
        )

        first = gateway.resolve_download_route(relative_route, "")
        second = gateway.resolve_download_route(relative_route, "")

        self.assertEqual((first.state, second.state), ("MISS", "HIT"))
        self.assertIsNotNone(storage.load(upstream_url))

    def test_proxy_only_caches_success_that_actually_used_proxy(self) -> None:
        root = Path(self.temp.name) / "proxy-only-proxy"
        storage = FileStorage(root)
        source = SourceConfig.from_dict(
            {
                "name": "official",
                "base_url": "https://official.example/",
                "ecosystem": "pip",
                "proxy_mode": "configured",
            }
        )
        content = b"proxy-only-object"

        def fetched(*args, **kwargs):
            stream, temp_path = storage.create_temp()
            with stream:
                stream.write(content)
            return FetchResult(
                entry=CacheEntry(
                    url=source.build_url("object"),
                    digest=hashlib.sha256(content).hexdigest(),
                    size=len(content),
                    content_type="application/octet-stream",
                    fetched_at=1,
                ),
                temp_path=temp_path,
                upstream_label="official",
                proxy_mode="configured",
                used_proxy=True,
                size=len(content),
            )

        fetcher = MagicMock()
        fetcher.fetch.side_effect = fetched
        gateway = Gateway(
            GatewayConfig(sources={source.name: source}),
            storage,
            fetcher,
            300,
            cache_mode="proxy-only",
        )
        first = gateway.resolve(source.name, "object", "")
        self.assertEqual(first.state, "MISS")
        self.assertIsNotNone(storage.load(source.build_url("object")))
        second = gateway.resolve(source.name, "object", "")
        self.assertEqual(second.state, "HIT")
        self.assertEqual(fetcher.fetch.call_count, 1)
        stats = gateway.stats()
        self.assertEqual(stats["hit"], 1)
        self.assertEqual(stats["miss"], 1)
        self.assertEqual(
            stats["cache_fills"]["configured_proxy"],
            {"objects": 1, "bytes": len(content)},
        )
        self.assertEqual(
            stats["upstream_attempts"]["configured_proxy"],
            {"success": 1, "failure": 0},
        )
        pypi = stats["modules"]["pypi"]
        self.assertEqual((pypi["hit"], pypi["miss"]), (1, 1))
        self.assertEqual(
            pypi["cache_fills"]["configured_proxy"],
            {"objects": 1, "bytes": len(content)},
        )
        self.assertEqual(
            pypi["upstream_attempts"]["configured_proxy"],
            {"success": 1, "failure": 0},
        )
        source_stats = pypi["sources"][source.name]
        self.assertEqual((source_stats["hit"], source_stats["miss"]), (1, 1))
        self.assertEqual(
            source_stats["cache_fills"]["configured_proxy"],
            {"objects": 1, "bytes": len(content)},
        )
        self.assertEqual(
            source_stats["upstream_attempts"]["configured_proxy"],
            {"success": 1, "failure": 0},
        )

    def test_sources_in_same_module_are_counted_independently(self) -> None:
        root = Path(self.temp.name) / "per-source-stats"
        storage = FileStorage(root)
        sources = tuple(
            SourceConfig.from_dict(
                {
                    "name": name,
                    "base_url": f"https://{name}.example/",
                    "ecosystem": "pip",
                    "proxy_mode": "configured",
                }
            )
            for name in ("pypi-simple", "pypi-files")
        )

        def fetched(source, url, **kwargs):
            content = source.name.encode()
            stream, temp_path = storage.create_temp()
            with stream:
                stream.write(content)
            return FetchResult(
                entry=CacheEntry(
                    url=url,
                    digest=hashlib.sha256(content).hexdigest(),
                    size=len(content),
                    content_type="application/octet-stream",
                    fetched_at=time.time(),
                ),
                temp_path=temp_path,
                upstream_label="official",
                proxy_mode="configured",
                used_proxy=True,
                size=len(content),
            )

        fetcher = MagicMock()
        fetcher.fetch.side_effect = fetched
        gateway = Gateway(
            GatewayConfig(sources={source.name: source for source in sources}),
            storage,
            fetcher,
            300,
            cache_mode="all",
        )

        gateway.resolve("pypi-simple", "simple/project/", "")
        gateway.resolve("pypi-simple", "simple/project/", "")
        gateway.resolve("pypi-files", "packages/project.whl", "")

        pypi = gateway.stats()["modules"]["pypi"]
        self.assertEqual((pypi["hit"], pypi["miss"]), (1, 2))
        self.assertEqual(
            (
                pypi["sources"]["pypi-simple"]["hit"],
                pypi["sources"]["pypi-simple"]["miss"],
            ),
            (1, 1),
        )
        self.assertEqual(
            (
                pypi["sources"]["pypi-files"]["hit"],
                pypi["sources"]["pypi-files"]["miss"],
            ),
            (0, 1),
        )
        self.assertEqual(
            pypi["sources"]["pypi-simple"]["cache_fills"][
                "configured_proxy"
            ]["objects"],
            1,
        )
        self.assertEqual(
            pypi["sources"]["pypi-files"]["cache_fills"][
                "configured_proxy"
            ]["objects"],
            1,
        )

    def test_index_and_wheel_are_cached_and_index_links_are_rewritten(self) -> None:
        index_path = "/v1/cache/pytorch/whl/cpu/torch/"
        with self.request(index_path) as first:
            body = first.read()
            self.assertEqual(first.headers["X-Artifact-Cache"], "MISS")
        self.assertIn(
            b'/v1/cache/pytorch/whl/cpu/torch-1.0-cp310-cp310-manylinux_x86_64.whl#sha256=',
            body,
        )
        self.assertNotIn(UpstreamHandler.link_origin.encode(), body)

        with self.request(index_path) as second:
            second.read()
            self.assertEqual(second.headers["X-Artifact-Cache"], "HIT")
        self.assertEqual(
            UpstreamHandler.counters[index_path.removeprefix("/v1/cache/pytorch")],
            1,
        )

        wheel_path = "/v1/cache/pytorch/whl/cpu/torch-1.0-cp310-cp310-manylinux_x86_64.whl"
        with self.request(wheel_path) as first_wheel:
            self.assertEqual(first_wheel.read(), WHEEL)
            self.assertEqual(first_wheel.headers["X-Artifact-Cache"], "MISS")
            self.assertEqual(
                first_wheel.headers["X-Content-SHA256"],
                hashlib.sha256(WHEEL).hexdigest(),
            )
        with self.request(wheel_path) as second_wheel:
            self.assertEqual(second_wheel.read(), WHEEL)
            self.assertEqual(second_wheel.headers["X-Artifact-Cache"], "HIT")
        self.assertEqual(
            UpstreamHandler.counters[
                "/whl/cpu/torch-1.0-cp310-cp310-manylinux_x86_64.whl"
            ],
            1,
        )

        paths = (
            "/v1/objects",
            "/v1/objects?ecosystem=pip",
            "/v1/objects?ecosystem=pip&source=pytorch",
            "/v1/objects?ecosystem=pip&source=pytorch&package=torch",
            "/v1/objects?ecosystem=pip&source=pytorch&package=torch&version=1.0",
        )
        expected_levels = (
            "ecosystems", "sources", "packages", "versions", "artifacts"
        )
        pages = []
        for path, level in zip(paths, expected_levels):
            with self.request(path) as response:
                page = json.load(response)
            self.assertEqual(page["level"], level)
            pages.append(page)
        artifact = pages[-1]["items"][0]
        self.assertEqual(artifact["filename"], wheel_path.rsplit("/", 1)[-1])
        self.assertEqual(artifact["python_tag"], "cp310")
        self.assertEqual(artifact["abi_tag"], "cp310")
        self.assertEqual(artifact["platform_tag"], "manylinux_x86_64")
        encoded = json.dumps(pages)
        self.assertNotIn(self.upstream.server_name, encoded)
        self.assertNotIn("http://", encoded)

    def test_npm_metadata_routes_exact_tarball_back_through_gateway(self) -> None:
        metadata_path = "/v1/cache/npm-registry/pnpm"
        with self.request(metadata_path) as first:
            metadata = json.load(first)
            self.assertEqual(first.headers["X-Artifact-Cache"], "MISS")
        tarball_url = metadata["versions"]["1.0.0"]["dist"]["tarball"]
        self.assertEqual(
            tarball_url,
            self.origin
            + "/v1/cache/npm-registry/pnpm/-/pnpm-1.0.0.tgz",
        )
        with self.opener.open(tarball_url, timeout=2) as first_tarball:
            self.assertEqual(first_tarball.read(), NPM_TARBALL)
            self.assertEqual(first_tarball.headers["X-Artifact-Cache"], "MISS")
        with self.opener.open(tarball_url, timeout=2) as second_tarball:
            self.assertEqual(second_tarball.read(), NPM_TARBALL)
            self.assertEqual(second_tarball.headers["X-Artifact-Cache"], "HIT")
        self.assertEqual(UpstreamHandler.counters["/pnpm"], 1)
        self.assertEqual(
            UpstreamHandler.counters["/pnpm/-/pnpm-1.0.0.tgz"], 1
        )

    def test_dart_metadata_and_archive_stay_on_gateway_and_hit_cache(self) -> None:
        with self.request("/v1/cache/dart-pub/api/packages/melos") as response:
            metadata = json.load(response)
            self.assertEqual(response.headers["X-Artifact-Cache"], "MISS")
            self.assertTrue(
                response.headers["Content-Type"].startswith(
                    "application/vnd.pub.v2+json"
                )
            )
        archive_url = metadata["latest"]["archive_url"]
        self.assertEqual(
            archive_url,
            self.origin
            + "/v1/cache/dart-pub-archives/api/archives/melos-1.0.0.tar.gz",
        )
        with self.opener.open(archive_url, timeout=2) as first:
            self.assertEqual(first.read(), DART_ARCHIVE)
            self.assertEqual(first.headers["X-Artifact-Cache"], "MISS")
            self.assertEqual(first.headers["Accept-Ranges"], "bytes")
        with self.opener.open(archive_url, timeout=2) as second:
            self.assertEqual(second.read(), DART_ARCHIVE)
            self.assertEqual(second.headers["X-Artifact-Cache"], "HIT")
        with self.assertRaises(HTTPError) as raised:
            self.request("/v1/cache/dart-pub/api/packages/badpkg")
        self.assertEqual(raised.exception.code, 400)
        self.assertEqual(
            UpstreamHandler.counters[
                "/dart-archives/api/archives/melos-1.0.0.tar.gz"
            ],
            1,
        )

    def test_julia_redirect_objects_range_head_and_cache(self) -> None:
        with self.request("/v1/cache/julia-pkg/registries") as response:
            registry_reference = response.read().decode().strip()
            self.assertEqual(response.headers["X-Artifact-Cache"], "MISS")
            self.assertNotIn("Location", response.headers)
        self.assertEqual(
            registry_reference, f"/registry/{JULIA_UUID}/{JULIA_HASH}"
        )
        registry_route = "/v1/cache/julia-pkg" + registry_reference
        with self.request(registry_route) as first:
            self.assertEqual(first.read(), JULIA_OBJECT)
            self.assertEqual(first.headers["X-Artifact-Cache"], "MISS")
            self.assertNotIn("Location", first.headers)
        with self.request(registry_route) as second:
            self.assertEqual(second.read(), JULIA_OBJECT)
            self.assertEqual(second.headers["X-Artifact-Cache"], "HIT")

        package_route = f"/v1/cache/julia-pkg/package/{JULIA_UUID}/{JULIA_HASH}"
        with self.request(package_route, {"Range": "bytes=0-1"}) as partial:
            self.assertEqual(partial.status, 206)
            self.assertEqual(partial.read(), JULIA_OBJECT[:2])
            self.assertEqual(
                partial.headers["Content-Range"], f"bytes 0-1/{len(JULIA_OBJECT)}"
            )
        artifact_route = f"/v1/cache/julia-pkg/artifact/{JULIA_HASH}"
        with self.request(artifact_route, method="HEAD") as head:
            self.assertEqual(head.read(), b"")
            self.assertEqual(int(head.headers["Content-Length"]), len(JULIA_OBJECT))
            self.assertEqual(head.headers["Accept-Ranges"], "bytes")
        self.assertEqual(
            UpstreamHandler.counters[
                f"/julia/registry/{JULIA_UUID}/{JULIA_HASH}"
            ],
            1,
        )

    def test_failed_primary_falls_back_then_caches_object(self) -> None:
        path = "/v1/cache/fallback/object"
        with self.request(path) as first:
            self.assertEqual(first.read(), FALLBACK_OBJECT)
            self.assertEqual(first.headers["X-Artifact-Cache"], "MISS")
        with self.request(path) as second:
            self.assertEqual(second.read(), FALLBACK_OBJECT)
            self.assertEqual(second.headers["X-Artifact-Cache"], "HIT")
        self.assertEqual(UpstreamHandler.counters["/primary/object"], 1)
        self.assertEqual(UpstreamHandler.counters["/secondary/object"], 1)
        stats = self.gateway.gateway.stats()
        self.assertEqual(
            stats["upstream_attempts"]["direct"],
            {"success": 1, "failure": 1},
        )

    def test_disallowed_redirect_falls_back_to_safe_upstream(self) -> None:
        with self.request("/v1/cache/redirect-fallback/object") as response:
            self.assertEqual(response.read(), FALLBACK_OBJECT)
            self.assertEqual(response.headers["X-Artifact-Cache"], "MISS")
        self.assertEqual(UpstreamHandler.counters["/redirect/object"], 1)
        self.assertEqual(UpstreamHandler.counters["/secondary/object"], 1)

    def test_slow_primary_is_discarded_before_fallback_is_cached(self) -> None:
        with self.request("/v1/cache/slow-fallback/object") as response:
            self.assertEqual(response.read(), FALLBACK_OBJECT)
            self.assertEqual(response.headers["X-Artifact-Cache"], "MISS")
        self.assertEqual(UpstreamHandler.counters["/slow/object"], 1)
        self.assertEqual(UpstreamHandler.counters["/secondary/object"], 1)

    def test_all_upstream_errors_are_recorded_without_urls(self) -> None:
        with self.assertRaises(HTTPError) as raised:
            self.request("/v1/cache/failed/object")
        self.assertEqual(raised.exception.code, 502)
        error_document = json.load(raised.exception)
        self.assertEqual(len(error_document["attempts"]), 2)
        self.assertEqual(
            [attempt["label"] for attempt in error_document["attempts"]],
            ["domestic", "official"],
        )
        self.assertEqual(
            [attempt["http_status"] for attempt in error_document["attempts"]],
            [503, 503],
        )

        with self.request("/v1/status") as status:
            document = json.load(status)
        failure = document["recent_failures"][0]
        self.assertEqual(failure["source"], "failed")
        self.assertEqual(len(failure["attempts"]), 2)
        encoded = json.dumps(failure)
        self.assertNotIn("127.0.0.1", encoded)
        self.assertNotIn("bad-a", encoded)
        self.assertNotIn("bad-b", encoded)
        attempts = document["requests"]["upstream_attempts"]
        self.assertEqual(attempts["direct"]["failure"], 1)
        self.assertEqual(attempts["configured_direct"]["failure"], 1)

    def test_range_head_health_and_status(self) -> None:
        wheel_path = "/v1/cache/pytorch/whl/cpu/torch-1.0-cp310-cp310-manylinux_x86_64.whl"
        with self.request(wheel_path) as response:
            response.read()
        with self.request(wheel_path, {"Range": "bytes=5-11"}) as partial:
            self.assertEqual(partial.status, 206)
            self.assertEqual(partial.read(), WHEEL[5:12])
            self.assertEqual(
                partial.headers["Content-Range"], f"bytes 5-11/{len(WHEEL)}"
            )
        with self.request(wheel_path, method="HEAD") as head:
            self.assertEqual(head.read(), b"")
            self.assertEqual(int(head.headers["Content-Length"]), len(WHEEL))
        with self.request("/healthz") as health:
            self.assertEqual(json.load(health), {"status": "ok"})
        with self.request("/v1/status") as status:
            document = json.load(status)
            self.assertEqual(status.headers["Cache-Control"], "no-store")
            self.assertEqual(
                document["sources"],
                [
                    "dart-pub",
                    "dart-pub-archives",
                    "failed",
                    "fallback",
                    "frozen-download-local",
                    "julia-pkg",
                    "npm-registry",
                    "pytorch",
                    "redirect-fallback",
                    "slow-fallback",
                ],
            )
            self.assertEqual(document["schema_version"], 6)
            self.assertEqual(document["storage"], "FileStorage")
            pytorch = next(
                row
                for row in document["source_details"]
                if row["name"] == "pytorch"
            )
            self.assertEqual(pytorch["route"], "/v1/cache/pytorch/")
            self.assertGreaterEqual(document["requests"]["hit"], 2)

    def test_read_only_webui_is_self_contained_and_supports_head(self) -> None:
        with self.request("/") as redirected:
            body = redirected.read()
            self.assertTrue(redirected.geturl().endswith("/ui/"))
            self.assertIn(b"<gateway-dashboard>", body)
            self.assertNotIn(b"https://", body)
            self.assertIn(
                "default-src 'self'",
                redirected.headers["Content-Security-Policy"],
            )

        for path, content_type in (
            ("/ui/styles.css", "text/css"),
            ("/ui/app.js", "text/javascript"),
        ):
            with self.request(path) as asset:
                content = asset.read()
                self.assertTrue(
                    asset.headers["Content-Type"].startswith(content_type)
                )
                self.assertGreater(len(content), 100)
                self.assertEqual(asset.headers["X-Content-Type-Options"], "nosniff")

        app = webui_asset("/ui/app.js")
        self.assertIsNotNone(app)
        with self.request("/ui/app.js", method="HEAD") as head:
            self.assertEqual(head.read(), b"")
            self.assertEqual(int(head.headers["Content-Length"]), len(app.body))

    def test_stale_index_uses_conditional_refresh(self) -> None:
        self.gateway.gateway.index_ttl_seconds = 0
        index_path = "/v1/cache/pytorch/whl/cpu/torch/"
        with self.request(index_path) as first:
            first.read()
            self.assertEqual(first.headers["X-Artifact-Cache"], "MISS")
        with self.request(index_path) as refreshed:
            refreshed.read()
            self.assertEqual(refreshed.headers["X-Artifact-Cache"], "REVALIDATED")
        self.assertEqual(UpstreamHandler.counters["/whl/cpu/torch/"], 2)

    def test_explicit_download_refresh_bypasses_freshness_without_http_admin(self) -> None:
        plan = {
            "kind": "artifact-cache-download-gateway-plan",
            "dataset": "/dataset",
            "rewrites": [
                {
                    "source": "pytorch",
                    "relative_path": "whl/cpu/torch/",
                    "gateway_path": "/v1/cache/pytorch/whl/cpu/torch/",
                    "refresh_policy": "manual",
                },
                {
                    "source": "pytorch",
                    "relative_path": "whl/cpu/torch/",
                    "gateway_path": "/v1/cache/pytorch/whl/cpu/torch/",
                    "refresh_policy": "immutable",
                },
            ],
        }
        first = refresh_downloads(
            plan,
            gateway=self.gateway.gateway,
            concurrency=1,
        )
        self.assertEqual(first["results"][0]["status"], "cached")
        self.assertEqual(first["results"][1]["status"], "skipped")
        second = refresh_downloads(
            {**plan, "rewrites": plan["rewrites"][:1]},
            gateway=self.gateway.gateway,
            concurrency=1,
        )
        self.assertEqual(second["results"][0]["status"], "unchanged")
        self.assertEqual(UpstreamHandler.counters["/whl/cpu/torch/"], 2)

    def test_download_warm_verifies_second_request_is_strict_hit(self) -> None:
        plan = {
            "kind": "artifact-cache-download-gateway-plan",
            "dataset": "/dataset",
            "rewrites": [
                {
                    "source": "pytorch",
                    "relative_path": "whl/cpu/torch-1.0-cp310-cp310-manylinux_x86_64.whl",
                    "gateway_path": "/v1/cache/pytorch/whl/cpu/torch-1.0-cp310-cp310-manylinux_x86_64.whl",
                    "refresh_policy": "immutable",
                }
            ],
        }
        result = warm_downloads(
            plan,
            gateway_url=f"{self.origin}/v1/cache",
            timeout_seconds=2,
            concurrency=1,
        )
        self.assertEqual(result["summary"]["cached"], 1)
        self.assertEqual(result["results"][0]["first_cache_state"], "MISS")
        self.assertEqual(result["results"][0]["verification_cache_state"], "HIT")

    def test_invalid_range_returns_416(self) -> None:
        wheel_path = "/v1/cache/pytorch/whl/cpu/torch-1.0-cp310-cp310-manylinux_x86_64.whl"
        with self.request(wheel_path) as response:
            response.read()
        with self.assertRaises(HTTPError) as raised:
            self.request(wheel_path, {"Range": "bytes=100-200"})
        self.assertEqual(raised.exception.code, 416)


if __name__ == "__main__":
    unittest.main()

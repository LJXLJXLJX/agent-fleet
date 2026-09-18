from __future__ import annotations

import unittest
from urllib.parse import urlsplit

from artifact_cache_gateway.core.source_naming import (
    apt_gateway_path,
    canonical_download_origin,
    download_gateway_path,
    frozen_download_source_name,
    parse_apt_gateway_route,
    parse_download_gateway_route,
    readable_source_name,
    without_legacy_source_hash,
)

from scripts.migrate_source_names import migrate


class SourceNamingTest(unittest.TestCase):
    def test_apt_route_is_reversible_and_accepts_appended_paths(self) -> None:
        route = apt_gateway_path("https://repo.example:8443/linux/ubuntu/")
        self.assertEqual(
            route,
            "/v1/cache/apt/v1/https/"
            f"{b'repo.example:8443'.hex()}/base/"
            f"{b'/linux/ubuntu'.hex()}",
        )
        origin, base_path, suffix = parse_apt_gateway_route(
            route.removeprefix("/v1/cache/apt/") + "/dists/noble/InRelease"
        )
        self.assertEqual(origin, "https://repo.example:8443")
        self.assertEqual(base_path, "/linux/ubuntu")
        self.assertEqual(suffix, "/dists/noble/InRelease")

    def test_apt_route_supports_origin_root_and_rejects_query(self) -> None:
        route = apt_gateway_path("http://repo.example")
        origin, base_path, suffix = parse_apt_gateway_route(
            route.removeprefix("/v1/cache/apt/") + "/pool/tool.deb"
        )
        self.assertEqual(origin, "http://repo.example")
        self.assertEqual(base_path, "/")
        self.assertEqual(suffix, "/pool/tool.deb")
        with self.assertRaises(ValueError):
            apt_gateway_path("https://repo.example/debian?channel=stable")

    def test_download_route_is_reversible_and_collision_free(self) -> None:
        urls = (
            "https://example.com",
            "https://example.com/@root",
            "http://example.com:080/a%20b",
            "https://" + "a" * 70 + ".example/one",
            "https://" + "a" * 69 + "b.example/two",
        )
        routes = [download_gateway_path(url) for url in urls]

        self.assertEqual(len(routes), len(set(routes)))
        for url, route in zip(urls, routes, strict=True):
            parsed_route = urlsplit(route)
            relative = parsed_route.path.removeprefix("/v1/cache/download/")
            origin, path = parse_download_gateway_route(
                relative, parsed_route.query
            )
            parsed_path = urlsplit(url).path
            self.assertEqual(origin, canonical_download_origin(url))
            self.assertEqual(path, None if parsed_path in {"", "/"} else parsed_path)

    def test_download_route_preserves_query_and_client_side_fragment(self) -> None:
        route = download_gateway_path(
            "https://example.com/object?version=2#section"
        )
        self.assertTrue(route.endswith("?version=2#section"))
        parsed = urlsplit(route)
        origin, path = parse_download_gateway_route(
            parsed.path.removeprefix("/v1/cache/download/"), parsed.query
        )
        self.assertEqual(origin, "https://example.com")
        self.assertEqual(path, "/object")

    def test_frozen_source_name_keeps_hash_when_hosts_share_long_prefix(self) -> None:
        first = frozen_download_source_name("https://" + "a" * 70 + ".example")
        second = frozen_download_source_name("https://" + "a" * 69 + "b.example")
        self.assertNotEqual(first, second)
        self.assertLessEqual(len(first), 63)

    def test_download_route_rejects_noncanonical_aliases(self) -> None:
        authority = "example.com".encode().hex()
        for route in (
            f"v1/https/{'example.com/ignored'.encode().hex()}/root",
            f"v1/https/{authority.upper()}/root",
            f"v1/https/{authority}/object/not-hex",
        ):
            with self.subTest(route=route), self.assertRaises(ValueError):
                parse_download_gateway_route(route)

    def test_readable_name_uses_host_path_scheme_and_port_without_hash(self) -> None:
        self.assertEqual(
            readable_source_name(
                "apt-repo",
                "https://repo.example/apt/ubuntu/",
                include_path=True,
            ),
            "apt-repo-repo-example-apt-ubuntu",
        )
        self.assertEqual(
            readable_source_name(
                "apt-repo",
                "http://repo.example:8080/apt/",
                include_path=True,
            ),
            "apt-repo-http-repo-example-port-8080-apt",
        )

    def test_only_generated_legacy_names_lose_hash_suffix(self) -> None:
        self.assertEqual(
            without_legacy_source_hash("apt-objects-example-com-1234abcd"),
            "apt-objects-example-com",
        )
        self.assertEqual(
            without_legacy_source_hash("vendor-release-1234abcd"),
            "vendor-release-1234abcd",
        )

    def test_migration_updates_sources_routes_and_review_metadata(self) -> None:
        document = {
            "sources": [
                {
                    "name": "download-objects-example-com-1234abcd",
                    "kind": "static-objects",
                    "ecosystem": "download",
                    "base_url": "https://example.com/",
                    "allowed_exact_paths": ["tool.tar.gz"],
                    "allow_query": False,
                }
            ],
            "rewrites": [
                {
                    "source": "download-objects-example-com-1234abcd",
                    "gateway_path": (
                        "/v1/cache/download-objects-example-com-1234abcd/"
                        "tool.tar.gz"
                    ),
                }
            ],
        }
        migrated = migrate(document, "2026-09-07")
        source = migrated["sources"][0]
        self.assertEqual(source["name"], "download-objects-example-com")
        self.assertEqual(source["config_updated_at"], "2026-09-07")
        self.assertEqual(source["config_update_policy"], "manual")
        self.assertEqual(
            migrated["rewrites"][0]["gateway_path"],
            "/v1/cache/download-objects-example-com/tool.tar.gz",
        )


if __name__ == "__main__":
    unittest.main()

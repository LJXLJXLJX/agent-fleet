"""dart ecosystem hooks: dart-pub path validation, the dart-pub-archives cross-source constraint, pub metadata rewriting and inventory fields."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from ..exceptions import ConfigError
from .base import EcosystemHandler

if TYPE_CHECKING:
    from ...gateway.engine.result import CacheResult
    from ...storage.base import CacheEntry
    from ..config.source import SourceConfig


_DART_PACKAGE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


_DART_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,127}$")


def _valid_dart_pub_path(path: str) -> bool:
    segments = path.split("/")
    if len(segments) == 3 and segments[:2] == ["api", "packages"]:
        return bool(_DART_PACKAGE.fullmatch(segments[2]))
    if (
        len(segments) == 5
        and segments[:2] == ["api", "packages"]
        and segments[3] == "versions"
    ):
        return bool(
            _DART_PACKAGE.fullmatch(segments[2])
            and _DART_VERSION.fullmatch(segments[4])
        )
    if (
        len(segments) == 4
        and segments[:2] == ["api", "packages"]
        and segments[3] == "advisories"
    ):
        return bool(_DART_PACKAGE.fullmatch(segments[2]))
    if len(segments) == 3 and segments[:2] == ["api", "archives"]:
        filename = segments[2]
        if not filename.endswith(".tar.gz"):
            return False
        package, separator, version = filename[:-7].partition("-")
        return bool(
            separator
            and _DART_PACKAGE.fullmatch(package)
            and _DART_VERSION.fullmatch(version)
        )
    if (
        len(segments) == 4
        and segments[0] == "packages"
        and segments[2] == "versions"
        and segments[3].endswith(".tar.gz")
    ):
        return bool(
            _DART_PACKAGE.fullmatch(segments[1])
            and _DART_VERSION.fullmatch(segments[3][:-7])
        )
    return False


def _validate_dart_path(source: SourceConfig, decoded: str) -> str | None:
    if not _valid_dart_pub_path(decoded):
        return "upstream path does not match the Dart Pub protocol allowlist"
    return None


def _validate_dart_config(
    source: SourceConfig, sources: Mapping[str, SourceConfig]
) -> str | None:
    if source.kind == "dart-pub" and any(
        prefix.startswith("api/packages/")
        for prefix in source.allowed_path_prefixes
    ):
        archive_source = sources.get("dart-pub-archives")
        if (
            archive_source is None
            or archive_source.kind != "dart-pub"
            or archive_source.ecosystem != "dart"
            or not any(
                prefix.startswith("api/archives/")
                for prefix in archive_source.allowed_path_prefixes
            )
        ):
            return "Dart Pub metadata sources require a reviewed dart-pub-archives source"
    return None


_MAX_REWRITTEN_DART_METADATA_BYTES = 32 * 1024 * 1024


def rewrite_dart_pub_metadata(
    content: bytes,
    result: CacheResult,
    archive_source: SourceConfig,
    gateway_origin: str,
) -> bytes:
    """Route every Dart archive URL through the reviewed archive source."""
    try:
        document = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return content
    route = f"{gateway_origin}/v1/cache/{archive_source.name}/"
    upstreams = (
        archive_source.primary_upstream,
        *archive_source.fallback_upstreams,
    )

    def archive_route(value: object) -> str:
        if not isinstance(value, str):
            raise ConfigError("Dart Pub metadata archive_url must be a string")
        try:
            parsed = urlsplit(value)
        except ValueError as exc:
            raise ConfigError("invalid Dart Pub metadata archive_url") from exc
        if parsed.username or parsed.password or parsed.fragment:
            raise ConfigError("invalid Dart Pub metadata archive_url")
        origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
        for upstream in upstreams:
            base = urlsplit(upstream.base_url)
            base_origin = f"{base.scheme.lower()}://{base.netloc.lower()}"
            if origin != base_origin or not parsed.path.startswith(base.path):
                continue
            relative_path = parsed.path[len(base.path) :].lstrip("/")
            archive_source.build_url(relative_path, parsed.query)
            rewritten = route + relative_path
            return rewritten + (f"?{parsed.query}" if parsed.query else "")
        raise ConfigError(
            "Dart Pub metadata archive_url does not belong to a reviewed archive source"
        )

    def rewrite(value):  # noqa: ANN001
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        if isinstance(value, dict):
            return {
                key: archive_route(item) if key == "archive_url" else rewrite(item)
                for key, item in value.items()
            }
        return value

    return json.dumps(
        rewrite(document), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def _dart_fields(relative_path: str, filename: str) -> dict[str, str | None]:
    segments = relative_path.split("/")
    if len(segments) == 3 and segments[:2] == ["api", "packages"]:
        return {
            "package": segments[2],
            "version": "@index",
            "object_type": "metadata",
        }
    if len(segments) == 5 and segments[:2] == ["api", "packages"]:
        return {
            "package": segments[2],
            "version": segments[4],
            "object_type": "metadata",
        }
    if (
        len(segments) == 4
        and segments[:2] == ["api", "packages"]
        and segments[3] == "advisories"
    ):
        return {
            "package": segments[2],
            "version": "@advisories",
            "object_type": "metadata",
        }
    if len(segments) == 3 and segments[:2] == ["api", "archives"]:
        stem = filename.removesuffix(".tar.gz")
        package, _, version = stem.partition("-")
        return {
            "package": package,
            "version": version,
            "object_type": "artifact",
        }
    if len(segments) == 4 and segments[0] == "packages":
        return {
            "package": segments[1],
            "version": filename.removesuffix(".tar.gz"),
            "object_type": "artifact",
        }
    return {
        "package": filename,
        "version": "@metadata",
        "object_type": "metadata",
    }


def _rewrite_matches(result: CacheResult, entry: CacheEntry) -> bool:
    return result.source.kind == "dart-pub" and (
        entry.url.startswith(f"{result.source.base_url}api/packages/")
    )


def _rewrite(
    content: bytes,
    result: CacheResult,
    *,
    gateway_origin: str,
    archive_source: SourceConfig | None = None,
) -> bytes:
    return rewrite_dart_pub_metadata(
        content, result, archive_source, gateway_origin
    )

HANDLER = EcosystemHandler(
    name="dart",
    kinds=("dart-pub",),
    ecosystems=("dart",),
    validate_path=_validate_dart_path,
    validate_config=_validate_dart_config,
    rewrite_matches=_rewrite_matches,
    rewrite=_rewrite,
    rewrite_size_limit=_MAX_REWRITTEN_DART_METADATA_BYTES,
    inventory_fields=lambda source, decoded_path, filename, content_type: (
        _dart_fields(decoded_path, filename)
    ),
)

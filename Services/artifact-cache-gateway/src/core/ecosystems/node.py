"""node ecosystem hooks: npm-registry path validation, metadata rewriting and inventory fields."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from ..exceptions import ConfigError
from .base import EcosystemHandler

if TYPE_CHECKING:
    from ...gateway.engine.result import CacheResult
    from ...storage.base import CacheEntry
    from ..config.source import SourceConfig


_NPM_PATH_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+~-]*$")


_NPM_SCOPE = re.compile(r"^@[A-Za-z0-9][A-Za-z0-9._~-]*$")


def _valid_npm_registry_path(path: str) -> bool:
    segments = path.split("/")
    package_segments = 2 if segments and segments[0].startswith("@") else 1
    if len(segments) < package_segments:
        return False
    if package_segments == 2:
        if not _NPM_SCOPE.fullmatch(segments[0]) or not _NPM_PATH_COMPONENT.fullmatch(
            segments[1]
        ):
            return False
    elif not _NPM_PATH_COMPONENT.fullmatch(segments[0]):
        return False
    remainder = segments[package_segments:]
    if not remainder:
        return True
    if len(remainder) == 1:
        return bool(_NPM_PATH_COMPONENT.fullmatch(remainder[0]))
    return (
        len(remainder) == 2
        and remainder[0] == "-"
        and remainder[1].endswith(".tgz")
        and bool(_NPM_PATH_COMPONENT.fullmatch(remainder[1][:-4]))
    )


def _validate_npm_path(source: SourceConfig, decoded: str) -> str | None:
    if not _valid_npm_registry_path(decoded):
        return "upstream path does not match the npm Registry allowlist"
    return None


_MAX_REWRITTEN_NPM_METADATA_BYTES = 16 * 1024 * 1024


def rewrite_npm_metadata(
    content: bytes, result: CacheResult, gateway_origin: str
) -> bytes:
    """Route npm Registry tarball URLs back through this fixed source."""
    try:
        document = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return content
    upstreams = (result.source.primary_upstream, *result.source.fallback_upstreams)
    route = f"{gateway_origin}/v1/cache/{result.source.name}/"

    def rewrite(value):  # noqa: ANN001
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        if isinstance(value, dict):
            return {key: rewrite(item) for key, item in value.items()}
        if not isinstance(value, str):
            return value
        try:
            parsed = urlsplit(value)
        except ValueError:
            return value
        origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
        for upstream in upstreams:
            base = urlsplit(upstream.base_url)
            base_origin = f"{base.scheme.lower()}://{base.netloc.lower()}"
            if origin != base_origin or not parsed.path.startswith(base.path):
                continue
            relative_path = parsed.path[len(base.path) :].lstrip("/")
            try:
                result.source.build_url(relative_path, parsed.query)
            except ConfigError:
                return value
            return route + relative_path
        return value

    return json.dumps(
        rewrite(document), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def _node_fields(
    relative_path: str, filename: str, content_type: str
) -> dict[str, str | None]:
    segments = [segment for segment in relative_path.strip("/").split("/") if segment]
    try:
        separator = segments.index("-")
    except ValueError:
        separator = -1
    if separator > 0 and separator + 2 == len(segments) and filename.endswith(".tgz"):
        package_parts = segments[:separator]
        package = "/".join(package_parts)
        basename = package_parts[-1]
        stem = filename[:-4]
        prefix = f"{basename}-"
        if stem.startswith(prefix) and len(stem) > len(prefix):
            return {
                "package": package,
                "version": stem[len(prefix) :],
                "object_type": "artifact",
            }
    if content_type.lower().startswith("application/json") and segments:
        package_parts = segments[:2] if segments[0].startswith("@") else segments[:1]
        return {
            "package": "/".join(package_parts),
            "version": "@index",
            "object_type": "metadata",
        }
    return {
        "package": filename,
        "version": "@unversioned",
        "object_type": "object",
    }


def _rewrite_matches(result: CacheResult, entry: CacheEntry) -> bool:
    return result.source.kind == "npm-registry" and (
        entry.content_type.lower().startswith("application/json")
    )


def _rewrite(
    content: bytes,
    result: CacheResult,
    *,
    gateway_origin: str,
    archive_source: SourceConfig | None = None,
) -> bytes:
    return rewrite_npm_metadata(content, result, gateway_origin)

HANDLER = EcosystemHandler(
    name="node",
    kinds=("npm-registry",),
    ecosystems=("node",),
    validate_path=_validate_npm_path,
    rewrite_matches=_rewrite_matches,
    rewrite=_rewrite,
    rewrite_size_limit=_MAX_REWRITTEN_NPM_METADATA_BYTES,
    inventory_fields=lambda source, decoded_path, filename, content_type: (
        _node_fields(decoded_path, filename, content_type)
    ),
)

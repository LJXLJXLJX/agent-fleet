"""julia ecosystem hooks: julia-pkg path validation, registries rewriting and inventory fields."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from ..exceptions import ConfigError
from .base import EcosystemHandler

if TYPE_CHECKING:
    from ...gateway.engine.result import CacheResult
    from ...storage.base import CacheEntry
    from ..config.source import SourceConfig


_JULIA_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


_JULIA_HASH = r"[0-9a-f]{40}"


_JULIA_OBJECT_PATH = re.compile(
    rf"^(?:registry|package)/{_JULIA_UUID}/{_JULIA_HASH}$|^artifact/{_JULIA_HASH}$"
)


def _valid_julia_pkg_path(path: str) -> bool:
    return path == "registries" or bool(_JULIA_OBJECT_PATH.fullmatch(path))


def _validate_julia_path(source: SourceConfig, decoded: str) -> str | None:
    if not _valid_julia_pkg_path(decoded):
        return "upstream path does not match the Julia Pkg Server protocol allowlist"
    return None


_MAX_REWRITTEN_JULIA_REGISTRIES_BYTES = 1024 * 1024


def rewrite_julia_registries(content: bytes, result: CacheResult) -> bytes:
    """Validate and normalize Julia registry references to relative routes."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError("Julia registries response is not UTF-8") from exc
    upstreams = (
        result.source.primary_upstream,
        *result.source.fallback_upstreams,
    )
    bases = tuple(upstream.base_url for upstream in upstreams)
    redirect_origins = frozenset(
        origin
        for upstream in upstreams
        for origin in upstream.allowed_redirect_origins
    )
    normalized: list[str] = []
    for line in text.splitlines():
        reference = line.strip()
        if not reference:
            continue
        if reference.startswith("/"):
            relative_path = reference.lstrip("/")
        else:
            try:
                parsed = urlsplit(reference)
            except ValueError as exc:
                raise ConfigError("Julia registries response contains an invalid URL") from exc
            relative_path = ""
            for base_url in bases:
                base = urlsplit(base_url)
                if (
                    parsed.scheme.lower() == base.scheme.lower()
                    and parsed.netloc.lower() == base.netloc.lower()
                    and parsed.path.startswith(base.path)
                ):
                    relative_path = parsed.path[len(base.path) :].lstrip("/")
                    break
            if not relative_path:
                origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
                if origin in redirect_origins:
                    relative_path = parsed.path.lstrip("/")
            if not relative_path:
                raise ConfigError("Julia registries response contains an unreviewed absolute URL")
        result.source.build_url(relative_path)
        if not relative_path.startswith("registry/"):
            raise ConfigError("Julia registries response may only reference registry objects")
        normalized.append(f"/{relative_path}")
    if not normalized:
        raise ConfigError("Julia registries response is empty")
    return ("\n".join(normalized) + "\n").encode("utf-8")


def _julia_fields(relative_path: str, filename: str) -> dict[str, str | None]:
    if relative_path == "registries":
        return {
            "package": "@registries",
            "version": "@index",
            "object_type": "metadata",
        }
    segments = relative_path.split("/")
    kind = segments[0]
    identifier = segments[1] if len(segments) == 3 else "@artifact"
    digest = segments[-1]
    return {
        "package": identifier,
        "version": digest,
        "object_type": "artifact",
    }


def _rewrite_matches(result: CacheResult, entry: CacheEntry) -> bool:
    return result.source.kind == "julia-pkg" and (
        entry.url == f"{result.source.base_url}registries"
    )


def _rewrite(
    content: bytes,
    result: CacheResult,
    *,
    gateway_origin: str,
    archive_source: SourceConfig | None = None,
) -> bytes:
    return rewrite_julia_registries(content, result)

HANDLER = EcosystemHandler(
    name="julia",
    kinds=("julia-pkg",),
    ecosystems=("julia",),
    validate_path=_validate_julia_path,
    rewrite_matches=_rewrite_matches,
    rewrite=_rewrite,
    rewrite_size_limit=_MAX_REWRITTEN_JULIA_REGISTRIES_BYTES,
    inventory_fields=lambda source, decoded_path, filename, content_type: (
        _julia_fields(decoded_path, filename)
    ),
)

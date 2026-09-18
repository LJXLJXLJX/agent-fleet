"""cargo ecosystem hooks: cargo-crates/cargo-sparse path validation, sparse config rewriting, rewrite_rustup_init (called by the server based on the source named rustup-init; semantics preserved) and inventory fields."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from .base import EcosystemHandler

if TYPE_CHECKING:
    from ...gateway.engine.result import CacheResult
    from ...storage.base import CacheEntry
    from ..config.source import SourceConfig


_CARGO_CRATE_NAME = re.compile(r"^[a-z0-9_-]{1,64}$")


_CARGO_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_-]{0,127}$")


def _valid_cargo_sparse_path(path: str) -> bool:
    if path == "config.json":
        return True
    segments = path.split("/")
    name = segments[-1]
    if not _CARGO_CRATE_NAME.fullmatch(name):
        return False
    if len(name) == 1:
        expected = ["1", name]
    elif len(name) == 2:
        expected = ["2", name]
    elif len(name) == 3:
        expected = ["3", name[0], name]
    else:
        expected = [name[:2], name[2:4], name]
    return segments == expected


def _valid_cargo_crate_path(path: str) -> bool:
    segments = path.split("/")
    return (
        len(segments) == 3
        and bool(_CARGO_CRATE_NAME.fullmatch(segments[0]))
        and bool(_CARGO_VERSION.fullmatch(segments[1]))
        and segments[2] == "download"
    )


def _validate_cargo_sparse_path(source: SourceConfig, decoded: str) -> str | None:
    if not _valid_cargo_sparse_path(decoded):
        return "upstream path does not match the Cargo sparse index allowlist"
    return None


def _validate_cargo_crate_path(source: SourceConfig, decoded: str) -> str | None:
    if not _valid_cargo_crate_path(decoded):
        return "upstream path does not match the Cargo crate download allowlist"
    return None


def _validate_path(source: SourceConfig, decoded: str) -> str | None:
    if source.kind == "cargo-sparse":
        return _validate_cargo_sparse_path(source, decoded)
    return _validate_cargo_crate_path(source, decoded)


_MAX_REWRITTEN_CARGO_CONFIG_BYTES = 1024 * 1024


def rewrite_cargo_sparse_config(
    content: bytes, gateway_origin: str
) -> bytes:
    """Keep crate downloads on the fixed cargo-crates Gateway source."""
    try:
        document = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return content
    if not isinstance(document, dict) or not isinstance(document.get("dl"), str):
        return content
    document["dl"] = f"{gateway_origin}/v1/cache/cargo-crates"
    return json.dumps(
        document, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def rewrite_rustup_init(content: bytes) -> bytes:
    """Allow the reviewed bootstrap script to fetch from an HTTP Gateway."""
    return content.replace(
        b"--proto '=https'", b"--proto '=http,https'"
    ).replace(
        b'--proto "=https"', b'--proto "=http,https"'
    )


def _cargo_fields(
    source: SourceConfig, relative_path: str, filename: str
) -> dict[str, str | None]:
    segments = relative_path.split("/")
    if source.kind == "cargo-crates" and len(segments) == 3:
        package, version, _download = segments
        return {
            "package": package,
            "version": version,
            "filename": f"{package}-{version}.crate",
            "object_type": "artifact",
        }
    if source.kind == "cargo-sparse":
        package = "@config" if relative_path == "config.json" else segments[-1]
        return {
            "package": package,
            "version": "@index",
            "object_type": "metadata",
        }
    return {
        "package": filename,
        "version": "@unversioned",
        "object_type": "object",
    }


def _rewrite_matches(result: CacheResult, entry: CacheEntry) -> bool:
    return result.source.kind == "cargo-sparse" and (
        entry.url == f"{result.source.base_url}config.json"
    )


def _rewrite(
    content: bytes,
    result: CacheResult,
    *,
    gateway_origin: str,
    archive_source: SourceConfig | None = None,
) -> bytes:
    return rewrite_cargo_sparse_config(content, gateway_origin)

HANDLER = EcosystemHandler(
    name="cargo",
    kinds=("cargo-crates", "cargo-sparse"),
    ecosystems=("cargo",),
    validate_path=_validate_path,
    rewrite_matches=_rewrite_matches,
    rewrite=_rewrite,
    rewrite_size_limit=_MAX_REWRITTEN_CARGO_CONFIG_BYTES,
    inventory_fields=lambda source, decoded_path, filename, content_type: (
        _cargo_fields(source, decoded_path, filename)
    ),
)

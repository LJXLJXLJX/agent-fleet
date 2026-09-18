"""go ecosystem hooks: go-proxy/go-sumdb path validation and inventory fields (dispatched internally by kind)."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .base import EcosystemHandler

if TYPE_CHECKING:
    from ..config.source import SourceConfig


_GO_PROXY_PATH = re.compile(
    r"^[A-Za-z0-9!$&'()*+,;=:@._~/-]+/(?:@latest|@v/(?:list|[^/]+\.(?:info|mod|zip)))$"
)


_GO_SUMDB_LOOKUP_PATH = re.compile(
    r"^lookup/[A-Za-z0-9!$&'()*+,;=:@._~/-]+@[A-Za-z0-9!$&'()*+,;=:@._~+-]+$"
)


def _valid_go_proxy_path(path: str) -> bool:
    return bool(_GO_PROXY_PATH.fullmatch(path))


def _valid_go_sumdb_path(path: str) -> bool:
    if path == "latest" or _GO_SUMDB_LOOKUP_PATH.fullmatch(path):
        return True
    parts = path.split("/")
    if len(parts) < 4 or parts[0] != "tile":
        return False
    try:
        height = int(parts[1])
    except ValueError:
        return False
    if not 1 <= height <= 30 or str(height) != parts[1]:
        return False
    if parts[2] != "data":
        try:
            level = int(parts[2])
        except ValueError:
            return False
        if not 0 <= level <= 63 or str(level) != parts[2]:
            return False

    coordinates = parts[3:]
    if len(coordinates) >= 2 and coordinates[-2].endswith(".p"):
        coordinates[-2] = coordinates[-2][:-2]
        try:
            width = int(coordinates[-1])
        except ValueError:
            return False
        if not 1 <= width < 1 << height or str(width) != coordinates[-1]:
            return False
        coordinates.pop()
    if not coordinates or not re.fullmatch(r"[0-9]{3}", coordinates[-1]):
        return False
    if any(not re.fullmatch(r"x[0-9]{3}", item) for item in coordinates[:-1]):
        return False
    # The Go tlog path encoding is canonical and never emits a leading x000.
    return not (len(coordinates) > 1 and coordinates[0] == "x000")


def _validate_go_proxy_path(source: SourceConfig, decoded: str) -> str | None:
    if not _valid_go_proxy_path(decoded):
        return "upstream path does not match the GOPROXY protocol allowlist"
    return None


def _validate_go_sumdb_path(source: SourceConfig, decoded: str) -> str | None:
    if not _valid_go_sumdb_path(decoded):
        return "upstream path does not match the Go checksum database allowlist"
    return None


def _validate_path(source: SourceConfig, decoded: str) -> str | None:
    if source.kind == "go-proxy":
        return _validate_go_proxy_path(source, decoded)
    return _validate_go_sumdb_path(source, decoded)


def _go_fields(relative_path: str, filename: str) -> dict[str, str | None]:
    module, separator, endpoint = relative_path.partition("/@v/")
    if separator:
        if endpoint == "list":
            return {
                "package": module,
                "version": "@index",
                "object_type": "metadata",
            }
        version, suffix = endpoint.rsplit(".", 1)
        return {
            "package": module,
            "version": version,
            "object_type": "artifact" if suffix == "zip" else "metadata",
        }
    if relative_path.endswith("/@latest"):
        return {
            "package": relative_path.removesuffix("/@latest"),
            "version": "@latest",
            "object_type": "metadata",
        }
    return {
        "package": filename,
        "version": "@unversioned",
        "object_type": "object",
    }


def _go_sumdb_fields(relative_path: str, filename: str) -> dict[str, str | None]:
    if relative_path.startswith("lookup/"):
        record = relative_path.removeprefix("lookup/")
        module, _, version = record.rpartition("@")
        return {
            "package": module or "@sumdb",
            "version": version or "@metadata",
            "object_type": "metadata",
        }
    return {
        "package": "@sumdb",
        "version": "@latest" if relative_path == "latest" else "@tile",
        "object_type": "metadata",
    }


def _inventory_fields(
    source: SourceConfig, decoded_path: str, filename: str, content_type: str
) -> dict[str, str | None]:
    return (
        _go_sumdb_fields(decoded_path, filename)
        if source.kind == "go-sumdb"
        else _go_fields(decoded_path, filename)
    )

HANDLER = EcosystemHandler(
    name="go",
    kinds=("go-proxy", "go-sumdb"),
    ecosystems=("go",),
    validate_path=_validate_path,
    inventory_fields=_inventory_fields,
)

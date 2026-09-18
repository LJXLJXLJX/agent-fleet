"""apt ecosystem hooks: stats display name (including the static-objects special case) and inventory fields. The APT dynamic routing service lives in gateway/services/apt.py; this module only wires up the hooks."""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

from .base import EcosystemHandler

if TYPE_CHECKING:
    from ..config.source import SourceConfig


def _apt_fields(relative_path: str, filename: str) -> dict[str, str | None]:
    lowered = filename.lower()
    if lowered.endswith((".deb", ".udeb")):
        stem = filename.rsplit(".", 1)[0]
        parts = stem.rsplit("_", 2)
        if len(parts) == 3 and all(parts):
            return {
                "package": parts[0],
                "version": parts[1],
                "architecture": parts[2],
                "object_type": "artifact",
            }
    segments = [segment for segment in relative_path.strip("/").split("/") if segment]
    try:
        dists_index = segments.index("dists")
    except ValueError:
        dists_index = -1
    if dists_index >= 0:
        suite = segments[dists_index + 1] if len(segments) > dists_index + 1 else "@root"
        return {"package": "@metadata", "version": suite, "object_type": "metadata"}
    if lowered.endswith((".gpg", ".asc", ".key")) or filename == "gpg":
        return {"package": "@keys", "version": "@unversioned", "object_type": "key"}
    return {"package": "@metadata", "version": "@unversioned", "object_type": "metadata"}


def _display_name(source: SourceConfig, request_url: str | None) -> str | None:
    candidate = source.base_url
    if source.kind == "static-objects":
        if request_url is not None:
            candidate = request_url
        elif len(source.allowed_exact_paths) == 1:
            candidate = source.build_url(next(iter(source.allowed_exact_paths)))
    try:
        parsed = urlsplit(candidate)
        if parsed.username is not None or parsed.password is not None:
            return source.name
        return urlunsplit(
            (
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                parsed.path.rstrip("/"),
                "",
                "",
            )
        )
    except ValueError:
        return source.name

HANDLER = EcosystemHandler(
    name="apt",
    ecosystems=("apt",),
    display_name=_display_name,
    inventory_fields=lambda source, decoded_path, filename, content_type: (
        _apt_fields(decoded_path, filename)
    ),
)

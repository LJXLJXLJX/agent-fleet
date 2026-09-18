from __future__ import annotations

import hashlib
from pathlib import PurePosixPath
from urllib.parse import unquote

from ..core.config import SourceConfig
from ..core.ecosystems import handler_for_ecosystem
from ..storage.base import CacheEntry
from ..storage.inventory import CacheObject


def cache_object(
    source: SourceConfig,
    relative_path: str,
    entry: CacheEntry,
) -> CacheObject:
    decoded_path = unquote(relative_path).lstrip("/")
    filename = PurePosixPath(decoded_path.rstrip("/")).name or "@root"
    fields: dict[str, str | None]
    handler = handler_for_ecosystem(source.ecosystem)
    if handler is not None and handler.inventory_fields is not None:
        fields = handler.inventory_fields(
            source, decoded_path, filename, entry.content_type
        )
    else:
        fields = {
            "package": filename,
            "version": "@unversioned",
            "object_type": "object",
        }
    result = CacheObject(
        object_id=hashlib.sha256(entry.url.encode("utf-8")).hexdigest(),
        source=source.name,
        ecosystem=source.ecosystem,
        package=str(fields["package"]),
        version=str(fields["version"]),
        filename=str(fields.get("filename", filename)),
        relative_path=decoded_path,
        object_type=str(fields["object_type"]),
        digest=entry.digest,
        size=entry.size,
        content_type=entry.content_type,
        fetched_at=entry.fetched_at,
        python_tag=fields.get("python_tag"),
        abi_tag=fields.get("abi_tag"),
        platform_tag=fields.get("platform_tag"),
        architecture=fields.get("architecture"),
    )
    result.validate()
    return result

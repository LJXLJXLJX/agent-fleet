from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from .base import StorageError

_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_SAFE_GROUP = re.compile(r"^[^\x00\r\n]{1,512}$")
_WHEEL_TAG = re.compile(r"^[A-Za-z0-9_.]+$")


@dataclass(frozen=True)
class CacheObject:
    object_id: str
    source: str
    ecosystem: str
    package: str
    version: str
    filename: str
    relative_path: str
    object_type: str
    digest: str
    size: int
    content_type: str
    fetched_at: float
    python_tag: str | None = None
    abi_tag: str | None = None
    platform_tag: str | None = None
    architecture: str | None = None

    def validate(self) -> None:
        if not _DIGEST.fullmatch(self.object_id) or not _DIGEST.fullmatch(self.digest):
            raise StorageError("invalid inventory digest")
        if self.ecosystem not in {
            "apt",
            "pip",
            "node",
            "go",
            "cargo",
            "dart",
            "julia",
            "download",
            "generic",
        }:
            raise StorageError("invalid inventory ecosystem")
        for value in (self.source, self.package, self.version, self.filename):
            if not _SAFE_GROUP.fullmatch(value):
                raise StorageError("invalid inventory group field")
        if not self.relative_path or "\x00" in self.relative_path:
            raise StorageError("invalid inventory relative_path")
        if self.object_type not in {"artifact", "index", "metadata", "key", "object"}:
            raise StorageError("invalid inventory object_type")
        if self.size < 0:
            raise StorageError("invalid inventory size")
        for value in (self.python_tag, self.abi_tag, self.platform_tag):
            if value is not None and not _WHEEL_TAG.fullmatch(value):
                raise StorageError("invalid inventory wheel tag")

    def document(self) -> dict[str, object]:
        self.validate()
        return asdict(self)

    def public_document(self) -> dict[str, object]:
        """Credential-free object view; canonical upstream URLs are never exposed."""

        return self.document()

    @classmethod
    def from_document(cls, value: object) -> "CacheObject":
        if not isinstance(value, dict):
            raise StorageError("inventory document must be an object")
        try:
            result = cls(**value)
        except TypeError as exc:
            raise StorageError("malformed inventory document") from exc
        result.validate()
        return result


@dataclass(frozen=True)
class InventoryListing:
    children: tuple[str, ...] = ()
    objects: tuple[CacheObject, ...] = ()
    next_cursor: str | None = None

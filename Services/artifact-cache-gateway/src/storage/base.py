from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO, Protocol

if TYPE_CHECKING:
    from .inventory import CacheObject, InventoryListing
    from .request_stats import RequestStatsSession


_DIGEST = re.compile(r"^[0-9a-f]{64}$")


class StorageError(RuntimeError):
    """Raised when authoritative storage cannot complete an operation."""


@dataclass(frozen=True)
class CacheEntry:
    url: str
    digest: str
    size: int
    content_type: str
    fetched_at: float
    etag: str | None = None
    last_modified: str | None = None

    def validate(self) -> None:
        if not _DIGEST.fullmatch(self.digest):
            raise StorageError("invalid cache entry digest")
        if self.size < 0:
            raise StorageError("invalid cache entry size")
        if not self.url or not self.content_type:
            raise StorageError("incomplete cache entry fields")


class Storage(Protocol):
    def load(self, url: str) -> CacheEntry | None: ...

    def create_temp(self) -> tuple[BinaryIO, Path]: ...

    def publish(self, entry: CacheEntry, temp_path: Path) -> CacheEntry: ...

    def touch(self, entry: CacheEntry, fetched_at: float) -> CacheEntry: ...

    def open_blob(
        self, entry: CacheEntry, start: int = 0, end: int | None = None
    ) -> AbstractContextManager[BinaryIO]: ...

    def ensure_inventory(
        self, record: CacheObject, *, replace_existing: bool = False
    ) -> None: ...

    def list_inventory(
        self, components: tuple[str, ...], cursor: str | None, limit: int
    ) -> InventoryListing: ...

    def iter_entries(self) -> Iterator[CacheEntry]: ...

    def load_request_stats(self) -> tuple[RequestStatsSession, ...]: ...

    def save_request_stats(self, session: RequestStatsSession) -> None: ...
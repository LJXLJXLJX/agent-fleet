from __future__ import annotations

import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
from typing import BinaryIO, Iterator
from urllib.parse import quote, unquote

from .base import CacheEntry, StorageError
from .inventory import CacheObject, InventoryListing
from .request_stats import RequestStatsSession


class FileStorage:
    """Explicit development/test storage; never a production fallback."""

    def __init__(self, root: Path):
        self.root = root
        self.blobs = root / "blobs" / "sha256"
        self.metadata = root / "metadata" / "url"
        self.inventory = root / "inventory" / "v1"
        self.request_stats = root / "metrics" / "request-sessions" / "v1"
        self.tmp = root / "tmp"
        for directory in (
            self.blobs,
            self.metadata,
            self.inventory,
            self.request_stats,
            self.tmp,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def url_key(url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    def blob_path(self, digest: str) -> Path:
        return self.blobs / digest[:2] / digest

    def metadata_path(self, url: str) -> Path:
        key = self.url_key(url)
        return self.metadata / key[:2] / f"{key}.json"

    def load(self, url: str) -> CacheEntry | None:
        path = self.metadata_path(url)
        try:
            with path.open("r", encoding="utf-8") as stream:
                entry = CacheEntry(**json.load(stream))
                entry.validate()
        except (
            FileNotFoundError, json.JSONDecodeError, OSError, TypeError, StorageError
        ):
            return None
        if entry.url != url or not self.blob_path(entry.digest).is_file():
            return None
        return entry

    def create_temp(self) -> tuple[BinaryIO, Path]:
        descriptor, name = tempfile.mkstemp(prefix="fetch-", dir=self.tmp)
        return os.fdopen(descriptor, "wb"), Path(name)

    @contextmanager
    def open_blob(
        self, entry: CacheEntry, start: int = 0, end: int | None = None
    ):
        del end
        try:
            stream = self.blob_path(entry.digest).open("rb")
        except OSError as exc:
            raise StorageError("local blob is not readable") from exc
        try:
            stream.seek(start)
            yield stream
        finally:
            stream.close()

    def publish(self, entry: CacheEntry, temp_path: Path) -> CacheEntry:
        entry.validate()
        blob_path = self.blob_path(entry.digest)
        blob_path.parent.mkdir(parents=True, exist_ok=True)
        if blob_path.exists():
            temp_path.unlink(missing_ok=True)
        else:
            os.replace(temp_path, blob_path)
        self.write_metadata(entry)
        return entry

    def touch(self, entry: CacheEntry, fetched_at: float) -> CacheEntry:
        updated = replace(entry, fetched_at=fetched_at)
        self.write_metadata(updated)
        return updated

    def write_metadata(self, entry: CacheEntry) -> None:
        path = self.metadata_path(entry.url)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix="metadata-", dir=path.parent)
        temp_path = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(asdict(entry), stream, ensure_ascii=False, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)

    @staticmethod
    def _inventory_component(value: str) -> str:
        return quote(value, safe="")

    def inventory_path(self, record: "CacheObject") -> Path:
        return self.inventory.joinpath(
            *(self._inventory_component(value) for value in (
                record.ecosystem,
                record.source,
                record.package,
                record.version,
            )),
            f"{record.object_id}.json",
        )

    def ensure_inventory(
        self, record: "CacheObject", *, replace_existing: bool = False
    ) -> None:
        if not isinstance(record, CacheObject):
            raise StorageError("invalid inventory record type")
        document = record.document()
        path = self.inventory_path(record)
        if path.is_file() and not replace_existing:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(prefix="inventory-", dir=path.parent)
        temp_path = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(document, stream, ensure_ascii=False, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)

    def list_inventory(
        self, components: tuple[str, ...], cursor: str | None, limit: int
    ) -> "InventoryListing":
        if len(components) > 4:
            raise StorageError("invalid inventory hierarchy")
        directory = self.inventory.joinpath(
            *(self._inventory_component(value) for value in components)
        )
        try:
            offset = int(cursor or "0")
        except ValueError as exc:
            raise StorageError("invalid inventory cursor") from exc
        if offset < 0:
            raise StorageError("invalid inventory cursor")
        try:
            entries = sorted(directory.iterdir(), key=lambda item: item.name)
        except FileNotFoundError:
            entries = []
        except OSError as exc:
            raise StorageError("failed to read local inventory") from exc
        entries = [
            item for item in entries
            if (len(components) < 4 and item.is_dir())
            or (len(components) == 4 and item.is_file() and item.suffix == ".json")
        ]
        page = entries[offset : offset + limit]
        next_cursor = str(offset + limit) if offset + limit < len(entries) else None
        if len(components) < 4:
            return InventoryListing(
                children=tuple(unquote(item.name) for item in page),
                next_cursor=next_cursor,
            )
        objects = []
        for item in page:
            try:
                with item.open("r", encoding="utf-8") as stream:
                    objects.append(CacheObject.from_document(json.load(stream)))
            except (OSError, json.JSONDecodeError, StorageError) as exc:
                raise StorageError("local inventory document is corrupted") from exc
        return InventoryListing(objects=tuple(objects), next_cursor=next_cursor)

    def iter_entries(self) -> Iterator[CacheEntry]:
        for path in sorted(self.metadata.glob("*/*.json")):
            try:
                with path.open("r", encoding="utf-8") as stream:
                    entry = CacheEntry(**json.load(stream))
                entry.validate()
            except (OSError, json.JSONDecodeError, TypeError, StorageError):
                continue
            if self.blob_path(entry.digest).is_file():
                yield entry

    def load_request_stats(self) -> tuple["RequestStatsSession", ...]:
        sessions = []
        for path in sorted(self.request_stats.glob("*.json")):
            try:
                with path.open("r", encoding="utf-8") as stream:
                    sessions.append(
                        RequestStatsSession.from_document(json.load(stream))
                    )
            except (OSError, json.JSONDecodeError, StorageError) as exc:
                raise StorageError("local request stats document is corrupted") from exc
        return tuple(sessions)

    def save_request_stats(self, session: "RequestStatsSession") -> None:
        document = session.document()
        path = self.request_stats / f"{session.session_id}.json"
        descriptor, name = tempfile.mkstemp(prefix="request-stats-", dir=path.parent)
        temp_path = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(document, stream, ensure_ascii=False, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)

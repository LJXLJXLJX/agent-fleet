"""inventory mixin: freshness checks, index entries, inventory documents and backfill."""

from __future__ import annotations

from urllib.parse import urlsplit
import logging
import time

from ...core.config import (
    ConfigError,
    SourceConfig,
)
from ...storage.base import (
    CacheEntry,
    StorageError,
)
from ..inventory import cache_object
from ...core.logging_utils import emit_event
from ...core.source_naming import without_legacy_source_hash
from ._common import _HIT_RATE_LOGGER


class _InventoryMixin:
    def _is_fresh(
        self, entry: CacheEntry, source: SourceConfig, relative_path: str
    ) -> bool:
        ttl = source.freshness_ttl(
            relative_path, entry.content_type, self.index_ttl_seconds
        )
        return ttl is None or time.time() - entry.fetched_at < ttl

    def _index_entry(
        self,
        source: SourceConfig,
        relative_path: str,
        entry: CacheEntry,
        *,
        replace_existing: bool = False,
    ) -> None:
        record = cache_object(source, relative_path, entry)
        with self._inventory_guard:
            if record.object_id in self._inventory_seen and not replace_existing:
                return
        try:
            self.storage.ensure_inventory(
                record, replace_existing=replace_existing
            )
        except StorageError as exc:
            emit_event(
                _HIT_RATE_LOGGER,
                "cache_inventory_write_failed",
                level=logging.WARNING,
                source=source.name,
                object_id=record.object_id[:16],
                reason=str(exc),
            )
            return
        with self._inventory_guard:
            self._inventory_seen.add(record.object_id)

    def inventory_document(
        self,
        *,
        ecosystem: str | None = None,
        source_name: str | None = None,
        package: str | None = None,
        version: str | None = None,
        cursor: str | None = None,
        limit: int = 50,
    ) -> dict[str, object]:
        if limit < 1 or limit > 100:
            raise ConfigError("inventory limit must be in 1..100")
        if cursor is not None and (not cursor or len(cursor) > 2048):
            raise ConfigError("invalid inventory cursor")
        configured_ecosystems = {
            source.ecosystem for source in self.config.sources.values()
        }
        configured_ecosystems.update({"apt", "download"})
        current_source: SourceConfig | None = None
        if ecosystem is None:
            if any(value is not None for value in (source_name, package, version)):
                raise ConfigError("inventory source/package/version require an ecosystem")
            components: tuple[str, ...] = ()
            level = "ecosystems"
        else:
            if ecosystem not in configured_ecosystems:
                raise ConfigError("unknown inventory ecosystem")
            components = (ecosystem,)
            level = "sources"
            if source_name is not None:
                current_source = self.config.sources.get(source_name)
                if current_source is None:
                    canonical_name = without_legacy_source_hash(source_name)
                    if canonical_name != source_name:
                        current_source = self.config.sources.get(canonical_name)
                dynamic_source = (
                    (source_name == "apt" and ecosystem == "apt")
                    or (source_name == "download" and ecosystem == "download")
                )
                if current_source is None and not dynamic_source:
                    raise ConfigError(f"unknown source: {source_name}")
                if (
                    current_source is not None
                    and current_source.ecosystem != ecosystem
                ):
                    raise ConfigError("inventory source does not match the ecosystem")
                components += (source_name,)
                level = "packages"
                if package is not None:
                    self._validate_inventory_group(package, "package")
                    components += (package,)
                    level = "versions"
                    if version is not None:
                        self._validate_inventory_group(version, "version")
                        components += (version,)
                        level = "artifacts"
                elif version is not None:
                    raise ConfigError("inventory version requires a package")
            elif package is not None or version is not None:
                raise ConfigError("inventory package/version require a source")
        listing = self.storage.list_inventory(components, cursor, limit)
        if level == "artifacts":
            assert ecosystem is not None
            assert source_name is not None
            assert package is not None
            assert version is not None
            items = []
            for record in listing.objects:
                if (
                    record.ecosystem,
                    record.source,
                    record.package,
                    record.version,
                ) != (ecosystem, source_name, package, version):
                    raise StorageError("inventory document does not match the index level")
                if current_source is not None:
                    try:
                        current_source.build_url(record.relative_path)
                    except ConfigError:
                        continue
                document = record.public_document()
                document["source"] = (
                    current_source.name
                    if current_source is not None
                    else source_name
                )
                items.append(document)
        elif level == "ecosystems":
            items = [
                {"key": child, "label": child}
                for child in listing.children
                if child in configured_ecosystems
            ]
        elif level == "sources":
            assert ecosystem is not None
            by_canonical_name: dict[str, dict[str, str]] = {}
            for child in listing.children:
                configured_source = self.config.sources.get(child)
                canonical_name = child
                if configured_source is None:
                    canonical_name = without_legacy_source_hash(child)
                    if canonical_name != child:
                        configured_source = self.config.sources.get(canonical_name)
                if (
                    configured_source is None
                    or configured_source.ecosystem != ecosystem
                ):
                    if (
                        child == "apt" and ecosystem == "apt"
                    ) or (
                        child == "download" and ecosystem == "download"
                    ):
                        by_canonical_name[child] = {
                            "key": child,
                            "label": child,
                        }
                    continue
                existing = by_canonical_name.get(canonical_name)
                if existing is None or child == canonical_name:
                    by_canonical_name[canonical_name] = {
                        "key": child,
                        "label": canonical_name,
                    }
            items = sorted(
                by_canonical_name.values(), key=lambda item: item["label"]
            )
        else:
            items = [{"key": child, "label": child} for child in listing.children]
        return {
            "schema_version": 1,
            "level": level,
            "items": items,
            "next_cursor": listing.next_cursor,
        }

    @staticmethod
    def _validate_inventory_group(value: str, label: str) -> None:
        if not value or len(value) > 512 or any(
            character in value for character in ("\x00", "\r", "\n")
        ):
            raise ConfigError(f"invalid inventory {label}")

    def backfill_inventory(self) -> dict[str, object]:
        counts: dict[str, object] = {
            "metadata": 0,
            "matched": 0,
            "unmatched": 0,
            "excluded": 0,
            "errors": 0,
            "sources": {},
            "error_reasons": {},
        }
        sources = counts["sources"]
        error_reasons = counts["error_reasons"]
        assert isinstance(sources, dict)
        assert isinstance(error_reasons, dict)
        for entry in self.storage.iter_entries():
            counts["metadata"] = int(counts["metadata"]) + 1
            matched = False
            for source in self.config.sources.values():
                if not entry.url.startswith(source.base_url):
                    continue
                parsed = urlsplit(entry.url)
                base = urlsplit(source.base_url)
                relative_path = parsed.path[len(base.path) :].lstrip("/")
                if (
                    not relative_path
                    and source.kind == "static-objects"
                    and "@root" in source.allowed_exact_paths
                ):
                    relative_path = "@root"
                try:
                    if source.build_url(relative_path, parsed.query) != entry.url:
                        continue
                    record = cache_object(source, relative_path, entry)
                    self.storage.ensure_inventory(record)
                except ConfigError:
                    counts["excluded"] = int(counts["excluded"]) + 1
                    source_counts = sources.setdefault(
                        source.name,
                        {"matched": 0, "excluded": 0, "errors": 0},
                    )
                    source_counts["excluded"] += 1
                    matched = True
                    break
                except StorageError as exc:
                    counts["errors"] = int(counts["errors"]) + 1
                    source_counts = sources.setdefault(
                        source.name,
                        {"matched": 0, "excluded": 0, "errors": 0},
                    )
                    source_counts["errors"] += 1
                    reason = str(exc)
                    error_reasons[reason] = error_reasons.get(reason, 0) + 1
                    matched = True
                    break
                counts["matched"] = int(counts["matched"]) + 1
                source_counts = sources.setdefault(
                    source.name,
                    {"matched": 0, "excluded": 0, "errors": 0},
                )
                source_counts["matched"] += 1
                matched = True
                break
            if not matched:
                counts["unmatched"] = int(counts["unmatched"]) + 1
        return counts

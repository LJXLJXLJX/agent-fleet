"""Gateway engine core: construction, resolution pipeline and release policy (red line)."""

from __future__ import annotations

import hashlib
import threading
import time
from collections import deque
from contextlib import contextmanager
from typing import BinaryIO, Iterator

from ...core.config import (
    ConfigError,
    GatewayConfig,
    SourceConfig,
)
from ...core.ecosystems import always_publish, display_name
from ...core.source_naming import (
    frozen_download_relative_path,
    parse_apt_gateway_route,
    parse_download_gateway_route,
)
from ...storage.base import (
    Storage,
    StorageError,
)
from ..fetcher import (
    Fetcher,
    FetchError,
    FetchResult,
    UpstreamAttempt,
)
from ._failures import _FailureMixin
from ._inventory import _InventoryMixin
from ._stats import RequestStats
from .result import CacheResult


class Gateway(_InventoryMixin, _FailureMixin):
    @staticmethod
    def source_display_name(
        source: SourceConfig, request_url: str | None = None
    ) -> str:
        """Return a credential-free plain-text label for source statistics."""

        display = display_name(source, request_url)
        if display is not None:
            return display
        return source.name

    def __init__(
        self,
        config: GatewayConfig,
        storage: Storage,
        fetcher: Fetcher,
        index_ttl_seconds: float,
        stale_if_error: bool = True,
        persist_request_stats: bool = False,
        stats_checkpoint_seconds: float = 5.0,
        cache_mode: str = "proxy-only",
    ):
        self.config = config
        self.storage = storage
        self.fetcher = fetcher
        self.index_ttl_seconds = index_ttl_seconds
        self.stale_if_error = stale_if_error
        if stats_checkpoint_seconds <= 0:
            raise ValueError("stats checkpoint seconds must be positive")
        if cache_mode not in {"proxy-only", "all"}:
            raise ValueError("cache mode must be proxy-only or all")
        self.cache_mode = cache_mode
        self._locks = tuple(threading.Lock() for _ in range(256))
        self._inventory_guard = threading.Lock()
        self._inventory_seen: set[str] = set()
        self._failure_guard = threading.Lock()
        self._recent_failures: deque[dict[str, object]] = deque(maxlen=100)
        self._request_stats = RequestStats(
            config=config,
            storage=storage,
            source_display_name=self.source_display_name,
            persist_request_stats=persist_request_stats,
            stats_checkpoint_seconds=stats_checkpoint_seconds,
            cache_mode=cache_mode,
        )

    def _lock_for(self, url: str) -> threading.Lock:
        digest = hashlib.sha256(url.encode("utf-8")).digest()
        return self._locks[int.from_bytes(digest[:2], "big") % len(self._locks)]

    def _record(
        self,
        state: str,
        source: SourceConfig,
        url: str,
        *,
        stats_source_name: str | None = None,
        cache_fill_route: str | None = None,
        cache_fill_bytes: int = 0,
    ) -> None:
        self._request_stats.record(
            state,
            source,
            url,
            stats_source_name=stats_source_name,
            cache_fill_route=cache_fill_route,
            cache_fill_bytes=cache_fill_bytes,
        )

    def record_git_mirror_state(self, state: str) -> None:
        self._request_stats.record_git_mirror_state(state)

    @staticmethod
    def _upstream_route(proxy_mode: str | None, used_proxy: bool) -> str:
        return RequestStats.upstream_route(proxy_mode, used_proxy)

    @classmethod
    def _cache_fill_route(cls, fetched: FetchResult) -> str:
        return cls._upstream_route(fetched.proxy_mode, fetched.used_proxy)

    def _record_upstream_attempts(
        self,
        source: SourceConfig,
        failed: tuple[UpstreamAttempt, ...],
        successful: FetchResult | None = None,
        *,
        stats_source_name: str | None = None,
    ) -> None:
        self._request_stats.record_upstream_attempts(
            source,
            failed,
            successful,
            stats_source_name=stats_source_name,
        )

    def stats(self) -> dict[str, object]:
        return self._request_stats.stats()

    def close(self) -> None:
        self._request_stats.close()

    def _should_publish(self, fetched: FetchResult) -> bool:
        return self.cache_mode == "all" or fetched.used_proxy

    @contextmanager
    def open_result_blob(
        self,
        result: CacheResult,
        start: int = 0,
        end: int | None = None,
    ) -> Iterator[BinaryIO]:
        if result.temp_path is None:
            with self.storage.open_blob(result.entry, start=start, end=end) as stream:
                yield stream
            return
        try:
            stream = result.temp_path.open("rb")
        except OSError as exc:
            raise StorageError("temporary upstream blob is not readable") from exc
        try:
            stream.seek(start)
            yield stream
        finally:
            stream.close()

    @staticmethod
    def release_result(result: CacheResult) -> None:
        if result.temp_path is not None:
            result.temp_path.unlink(missing_ok=True)

    def resolve(
        self,
        source_name: str,
        relative_path: str,
        query: str,
        *,
        prefer_fallback: bool = False,
        force_refresh: bool = False,
        source_override: SourceConfig | None = None,
        stats_source_name: str | None = None,
    ) -> CacheResult:
        source = source_override or self.config.source(source_name)
        url = source.build_url(relative_path, query)
        try:
            entry = self.storage.load(url)
        except StorageError:
            self._record("ERROR", source, url, stats_source_name=stats_source_name)
            raise
        if (
            entry
            and not force_refresh
            and self._is_fresh(entry, source, relative_path)
        ):
            self._index_entry(source, relative_path, entry)
            self._record("HIT", source, url, stats_source_name=stats_source_name)
            return CacheResult(entry=entry, state="HIT", source=source)

        with self._lock_for(url):
            try:
                entry = self.storage.load(url)
            except StorageError:
                self._record("ERROR", source, url, stats_source_name=stats_source_name)
                raise

            if (
                entry
                and not force_refresh
                and self._is_fresh(entry, source, relative_path)
            ):
                self._index_entry(source, relative_path, entry)
                self._record("HIT", source, url, stats_source_name=stats_source_name)
                return CacheResult(entry=entry, state="HIT", source=source)
            try:
                fetched = self.fetcher.fetch(
                    source,
                    url,
                    previous=entry,
                    relative_path=relative_path,
                    query=query,
                    prefer_fallback=prefer_fallback,
                )
                self._record_upstream_attempts(
                    source,
                    fetched.attempts,
                    fetched,
                    stats_source_name=stats_source_name,
                )
                if fetched.not_modified:
                    assert entry is not None
                    updated = self.storage.touch(entry, time.time())
                    self._index_entry(
                        source, relative_path, updated, replace_existing=True
                    )
                    self._record(
                        "REVALIDATED",
                        source,
                        url,
                        stats_source_name=stats_source_name,
                    )
                    return CacheResult(
                        entry=updated, state="REVALIDATED", source=source
                    )
                assert fetched.entry is not None and fetched.temp_path is not None
                if always_publish(source) or self._should_publish(fetched):
                    published = self.storage.publish(
                        fetched.entry, fetched.temp_path
                    )
                    self._index_entry(
                        source, relative_path, published, replace_existing=True
                    )
                    state = "REFRESH" if entry else "MISS"
                    self._record(
                        state,
                        source,
                        url,
                        stats_source_name=stats_source_name,
                        cache_fill_route=self._cache_fill_route(fetched),
                        cache_fill_bytes=fetched.size,
                    )
                    return CacheResult(
                        entry=published, state=state, source=source
                    )
                self._record(
                    "BYPASS", source, url, stats_source_name=stats_source_name
                )
                return CacheResult(
                    entry=fetched.entry,
                    state="BYPASS",
                    source=source,
                    temp_path=fetched.temp_path,
                )
            except FetchError as exc:
                self._record_upstream_attempts(
                    source,
                    exc.attempts,
                    stats_source_name=stats_source_name,
                )
                self._record_failure(source.name, url, exc)
                if entry and self.stale_if_error:
                    self._index_entry(source, relative_path, entry)
                    self._record(
                        "STALE", source, url, stats_source_name=stats_source_name
                    )
                    return CacheResult(entry=entry, state="STALE", source=source)
                self._record(
                    "ERROR", source, url, stats_source_name=stats_source_name
                )
                raise
            except StorageError:
                self._record(
                    "ERROR", source, url, stats_source_name=stats_source_name
                )
                raise

    def resolve_download_route(
        self,
        relative_route: str,
        query: str,
        *,
        prefer_fallback: bool = False,
    ) -> CacheResult:
        """Resolve an arbitrary reversible prebuild download route."""

        try:
            origin, path = parse_download_gateway_route(relative_route, query)
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc

        source = SourceConfig.from_dict(
            {
                "name": "download",
                "kind": "transparent-download",
                "ecosystem": "download",
                "base_url": f"{origin}/",
                "allow_query": True,
                "proxy_mode": "configured",
            }
        )
        source_relative = frozen_download_relative_path(path)
        return self.resolve(
            source.name,
            source_relative,
            query,
            prefer_fallback=prefer_fallback,
            source_override=source,
        )

    def resolve_apt_route(
        self,
        relative_route: str,
        query: str,
        *,
        prefer_fallback: bool = False,
    ) -> CacheResult:
        """Resolve a map-free APT source route with an appended repository path."""

        try:
            origin, base_path, suffix = parse_apt_gateway_route(relative_route)
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc

        path = (
            base_path
            if suffix is None
            else f"{base_path.rstrip('/')}{suffix}"
        )
        stats_source_name = origin + ("" if base_path == "/" else base_path)

        source = SourceConfig.from_dict(
            {
                "name": "apt",
                "kind": "transparent-download",
                "ecosystem": "apt",
                "base_url": f"{origin}/",
                "allow_query": True,
                "proxy_mode": "configured",
            }
        )
        return self.resolve(
            source.name,
            frozen_download_relative_path(path),
            query,
            prefer_fallback=prefer_fallback,
            source_override=source,
            stats_source_name=stats_source_name,
        )

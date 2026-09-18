"""Request-stats component: runtime recording, persistence, checkpointing, stats
documents and shutdown.

The Gateway holds this component via composition (`engine/core.py`) and delegates a
small set of record, snapshot and close methods; the component owns its own counting
state, stats lock, checkpoint thread and shutdown flow, and does not hold the full
Gateway or share Gateway state through dynamic forwarding. The persistence session and
the schema 1/2/3/4 contracts still live in `storage/request_stats.py` (the storage
contract layer); this component only handles runtime recording and lifecycle.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from collections.abc import Callable
from typing import TYPE_CHECKING

from ...core.config import GatewayConfig, SourceConfig
from ...core.logging_utils import emit_event
from ...core.source_naming import without_legacy_source_hash
from ...storage.base import Storage, StorageError
from ...storage.request_stats import (
    CACHE_FILL_ROUTES,
    REQUEST_STATES,
    RequestStatsSession,
    empty_cache_fills,
    empty_counts,
    empty_module_stats,
    empty_source_stats,
    empty_upstream_attempts,
)
from ..request_stats import (
    LEGACY_REQUEST_MODULE,
    empty_modules,
    request_module_for_ecosystem,
)
from ._common import _HIT_RATE_LOGGER

if TYPE_CHECKING:
    from ..fetcher import FetchResult, UpstreamAttempt


class RequestStats:
    """Gateway's standalone request-stats component (composition replacing the
    original mixin).

    It receives its dependencies explicitly: configuration, storage, a
    source-display-name function and the stats runtime parameters. The component owns
    all runtime counting, aggregation and checkpoint-related thread lifecycle; the
    Gateway does not keep a duplicate copy of the same stats state.
    """

    def __init__(
        self,
        config: GatewayConfig,
        storage: Storage,
        source_display_name: Callable[[SourceConfig, str | None], str],
        *,
        persist_request_stats: bool,
        stats_checkpoint_seconds: float,
        cache_mode: str,
    ):
        if stats_checkpoint_seconds <= 0:
            raise ValueError("stats checkpoint seconds must be positive")
        self._config = config
        self._storage = storage
        self._source_display_name = source_display_name
        self._persist_request_stats = persist_request_stats
        self._checkpoint_seconds = stats_checkpoint_seconds
        self._cache_mode = cache_mode

        self._guard = threading.Lock()
        self._stats = empty_counts()
        self._historical_stats = empty_counts()
        self._cache_fills = empty_cache_fills()
        self._historical_cache_fills = empty_cache_fills()
        self._upstream_attempts = empty_upstream_attempts()
        self._historical_upstream_attempts = empty_upstream_attempts()
        self._modules = empty_modules()
        self._historical_modules = empty_modules(include_legacy=True)
        self._historical_sessions = 0
        self._session_id = uuid.uuid4().hex
        self._started_at = time.time()
        self._last_attempt_monotonic = 0.0
        self._persisted_at: float | None = None
        self._persistence_error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if persist_request_stats:
            self._load_persistent_stats()
            self._thread = threading.Thread(
                target=self._checkpoint_worker,
                name="request-stats-checkpoint",
                daemon=True,
            )
            self._thread.start()

    @staticmethod
    def _with_rates(counts: dict[str, int]) -> dict[str, int | float]:
        lookup_total = sum(counts[name] for name in REQUEST_STATES if name != "error")
        reusable = counts["hit"] + counts["revalidated"] + counts["stale"]
        result: dict[str, int | float] = dict(counts)
        result["lookup_total"] = lookup_total
        result["request_total"] = lookup_total + counts["error"]
        result["hit_rate"] = round(reusable / lookup_total, 6) if lookup_total else 0.0
        result["fresh_hit_rate"] = (
            round(counts["hit"] / lookup_total, 6) if lookup_total else 0.0
        )
        return result

    @staticmethod
    def upstream_route(proxy_mode: str | None, used_proxy: bool) -> str:
        if proxy_mode == "direct":
            return "direct"
        return "configured_proxy" if used_proxy else "configured_direct"

    def _checkpoint_worker(self) -> None:
        while not self._stop.wait(self._checkpoint_seconds):
            with self._guard:
                self._checkpoint_stats_locked()

    def _load_persistent_stats(self) -> None:
        try:
            sessions = self._storage.load_request_stats()
        except StorageError as exc:
            self._persistence_error = str(exc)
            emit_event(
                _HIT_RATE_LOGGER,
                "request_stats_load_failed",
                level=logging.WARNING,
                reason=str(exc),
            )
            return
        for session in sessions:
            for state in REQUEST_STATES:
                self._historical_stats[state] += session.counts[state]
            for route in CACHE_FILL_ROUTES:
                for name in ("objects", "bytes"):
                    self._historical_cache_fills[route][name] += (
                        session.cache_fills[route][name]
                    )
                for name in ("success", "failure"):
                    self._historical_upstream_attempts[route][name] += (
                        session.upstream_attempts[route][name]
                    )
            session_modules = session.modules
            if not session_modules:
                legacy = empty_module_stats()
                legacy["counts"] = session.counts
                legacy["cache_fills"] = session.cache_fills
                legacy["upstream_attempts"] = session.upstream_attempts
                session_modules = {LEGACY_REQUEST_MODULE: legacy}
            for module, counters in session_modules.items():
                target = self._historical_modules.setdefault(
                    module, empty_module_stats()
                )
                for state in REQUEST_STATES:
                    target["counts"][state] += counters["counts"][state]
                for route in CACHE_FILL_ROUTES:
                    for name in ("objects", "bytes"):
                        target["cache_fills"][route][name] += (
                            counters["cache_fills"][route][name]
                        )
                    for name in ("success", "failure"):
                        target["upstream_attempts"][route][name] += (
                            counters["upstream_attempts"][route][name]
                        )
                for source, source_counters in counters["sources"].items():
                    source = without_legacy_source_hash(source)
                    configured_source = self._config.sources.get(source)
                    if configured_source is not None:
                        source = self._source_display_name(configured_source, None)
                    elif module == "apt" and not source.startswith(
                        ("http://", "https://")
                    ):
                        # Removed legacy APT source slugs are lossy and cannot be
                        # rendered as original URLs. Keep their counts in the
                        # module total, where they become historical_unattributed,
                        # instead of exposing a misleading internal identifier.
                        continue
                    target_source = target["sources"].setdefault(
                        source, empty_source_stats()
                    )
                    for state in REQUEST_STATES:
                        target_source["counts"][state] += source_counters[
                            "counts"
                        ][state]
                    for route in CACHE_FILL_ROUTES:
                        for name in ("objects", "bytes"):
                            target_source["cache_fills"][route][name] += (
                                source_counters["cache_fills"][route][name]
                            )
                        for name in ("success", "failure"):
                            target_source["upstream_attempts"][route][name] += (
                                source_counters["upstream_attempts"][route][name]
                            )
        self._historical_sessions = len(sessions)
        if sessions:
            self._persisted_at = max(session.updated_at for session in sessions)

    def _checkpoint_stats_locked(self, *, force: bool = False) -> None:
        has_module_stats = any(
            any(counters["counts"].values())
            for counters in self._modules.values()
        )
        if not self._persist_request_stats or not (
            any(self._stats.values()) or has_module_stats
        ):
            return
        now_monotonic = time.monotonic()
        if (
            not force
            and now_monotonic - self._last_attempt_monotonic
            < self._checkpoint_seconds
        ):
            return
        self._last_attempt_monotonic = now_monotonic
        now = time.time()
        session = RequestStatsSession(
            session_id=self._session_id,
            started_at=self._started_at,
            updated_at=now,
            counts=dict(self._stats),
            cache_fills={
                route: dict(counters)
                for route, counters in self._cache_fills.items()
            },
            upstream_attempts={
                route: dict(counters)
                for route, counters in self._upstream_attempts.items()
            },
            modules={
                module: {
                    "counts": dict(counters["counts"]),
                    "cache_fills": {
                        route: dict(route_counters)
                        for route, route_counters in counters["cache_fills"].items()
                    },
                    "upstream_attempts": {
                        route: dict(route_counters)
                        for route, route_counters in counters[
                            "upstream_attempts"
                        ].items()
                    },
                    "sources": {
                        source: {
                            "counts": dict(source_counters["counts"]),
                            "cache_fills": {
                                route: dict(route_counters)
                                for route, route_counters in source_counters[
                                    "cache_fills"
                                ].items()
                            },
                            "upstream_attempts": {
                                route: dict(route_counters)
                                for route, route_counters in source_counters[
                                    "upstream_attempts"
                                ].items()
                            },
                        }
                        for source, source_counters in counters["sources"].items()
                    },
                }
                for module, counters in self._modules.items()
            },
            label="gateway-process",
        )
        try:
            self._storage.save_request_stats(session)
        except StorageError as exc:
            self._persistence_error = str(exc)
            emit_event(
                _HIT_RATE_LOGGER,
                "request_stats_checkpoint_failed",
                level=logging.WARNING,
                reason=str(exc),
            )
            return
        self._persisted_at = now
        self._persistence_error = None

    def _combined_stats_locked(self) -> dict[str, int]:
        return {
            state: self._historical_stats[state] + self._stats[state]
            for state in REQUEST_STATES
        }

    def _combined_cache_fills_locked(self) -> dict[str, dict[str, int]]:
        return {
            route: {
                name: (
                    self._historical_cache_fills[route][name]
                    + self._cache_fills[route][name]
                )
                for name in ("objects", "bytes")
            }
            for route in CACHE_FILL_ROUTES
        }

    def _combined_upstream_attempts_locked(self) -> dict[str, dict[str, int]]:
        return {
            route: {
                name: (
                    self._historical_upstream_attempts[route][name]
                    + self._upstream_attempts[route][name]
                )
                for name in ("success", "failure")
            }
            for route in CACHE_FILL_ROUTES
        }

    def _stats_bucket_document(
        self, current: dict[str, object], historical: dict[str, object]
    ) -> dict[str, object]:
        counts = {
            state: current["counts"][state] + historical["counts"][state]
            for state in REQUEST_STATES
        }
        document: dict[str, object] = self._with_rates(counts)
        document["cache_fills"] = {
            route: {
                name: (
                    current["cache_fills"][route][name]
                    + historical["cache_fills"][route][name]
                )
                for name in ("objects", "bytes")
            }
            for route in CACHE_FILL_ROUTES
        }
        document["upstream_attempts"] = {
            route: {
                name: (
                    current["upstream_attempts"][route][name]
                    + historical["upstream_attempts"][route][name]
                )
                for name in ("success", "failure")
            }
            for route in CACHE_FILL_ROUTES
        }
        return document

    def _module_stats_document_locked(
        self, *, current_process: bool
    ) -> dict[str, dict[str, object]]:
        current = self._modules
        historical = {} if current_process else self._historical_modules
        modules = set(current) | set(historical)
        result: dict[str, dict[str, object]] = {}
        for module in sorted(modules):
            current_counters = current.get(module, empty_module_stats())
            historical_counters = historical.get(module, empty_module_stats())
            document = self._stats_bucket_document(
                current_counters, historical_counters
            )
            configured_sources = {
                self._source_display_name(source, None)
                for source in self._config.sources.values()
                if request_module_for_ecosystem(source.ecosystem) == module
            }
            if module == "git_clone":
                configured_sources.add("github")
            source_names = (
                configured_sources
                | set(current_counters["sources"])
                | set(historical_counters["sources"])
            )
            source_documents = {
                source: self._stats_bucket_document(
                    current_counters["sources"].get(source, empty_source_stats()),
                    historical_counters["sources"].get(
                        source, empty_source_stats()
                    ),
                )
                for source in sorted(source_names)
            }
            unattributed = empty_source_stats()
            for state in REQUEST_STATES:
                unattributed["counts"][state] = max(
                    0,
                    document[state]
                    - sum(row[state] for row in source_documents.values()),
                )
            for route in CACHE_FILL_ROUTES:
                for name in ("objects", "bytes"):
                    unattributed["cache_fills"][route][name] = max(
                        0,
                        document["cache_fills"][route][name]
                        - sum(
                            row["cache_fills"][route][name]
                            for row in source_documents.values()
                        ),
                    )
                for name in ("success", "failure"):
                    unattributed["upstream_attempts"][route][name] = max(
                        0,
                        document["upstream_attempts"][route][name]
                        - sum(
                            row["upstream_attempts"][route][name]
                            for row in source_documents.values()
                        ),
                    )
            if (
                any(unattributed["counts"].values())
                or any(
                    value
                    for counters in unattributed["cache_fills"].values()
                    for value in counters.values()
                )
                or any(
                    value
                    for counters in unattributed["upstream_attempts"].values()
                    for value in counters.values()
                )
            ):
                document["historical_unattributed"] = self._stats_bucket_document(
                    unattributed, empty_source_stats()
                )
            document["sources"] = source_documents
            result[module] = document
        return result

    def record(
        self,
        state: str,
        source: SourceConfig,
        url: str,
        *,
        stats_source_name: str | None = None,
        cache_fill_route: str | None = None,
        cache_fill_bytes: int = 0,
    ) -> None:
        key = state.lower()
        module = request_module_for_ecosystem(source.ecosystem)
        stats_source = stats_source_name or self._source_display_name(source, url)
        with self._guard:
            self._stats[key] += 1
            self._modules[module]["counts"][key] += 1
            source_stats = self._modules[module]["sources"].setdefault(
                stats_source, empty_source_stats()
            )
            source_stats["counts"][key] += 1
            if cache_fill_route is not None:
                self._cache_fills[cache_fill_route]["objects"] += 1
                self._cache_fills[cache_fill_route]["bytes"] += cache_fill_bytes
                self._modules[module]["cache_fills"][cache_fill_route][
                    "objects"
                ] += 1
                self._modules[module]["cache_fills"][cache_fill_route][
                    "bytes"
                ] += cache_fill_bytes
                source_stats["cache_fills"][cache_fill_route]["objects"] += 1
                source_stats["cache_fills"][cache_fill_route][
                    "bytes"
                ] += cache_fill_bytes
            self._checkpoint_stats_locked()
            snapshot = self._with_rates(self._combined_stats_locked())
        emit_event(
            _HIT_RATE_LOGGER,
            "cache_hit_rate",
            source=source.name,
            state=state,
            url_key=hashlib.sha256(url.encode("utf-8")).hexdigest()[:16],
            storage_backend=type(self._storage).__name__,
            **snapshot,
        )

    def record_git_mirror_state(self, state: str) -> None:
        request_state = {
            "hit": "hit",
            "fill": "miss",
            "refresh": "refresh",
            "error": "error",
        }.get(state)
        if request_state is None:
            raise ValueError(f"unknown Git mirror state: {state}")
        with self._guard:
            self._modules["git_clone"]["counts"][request_state] += 1
            source_stats = self._modules["git_clone"]["sources"].setdefault(
                "github", empty_source_stats()
            )
            source_stats["counts"][request_state] += 1
            self._checkpoint_stats_locked()

    def record_upstream_attempts(
        self,
        source: SourceConfig,
        failed: tuple[UpstreamAttempt, ...],
        successful: FetchResult | None = None,
        *,
        stats_source_name: str | None = None,
    ) -> None:
        module = request_module_for_ecosystem(source.ecosystem)
        stats_source = stats_source_name or self._source_display_name(source, None)
        with self._guard:
            source_stats = self._modules[module]["sources"].setdefault(
                stats_source, empty_source_stats()
            )
            for attempt in failed:
                route = self.upstream_route(
                    attempt.proxy_mode,
                    attempt.used_proxy,
                )
                self._upstream_attempts[route]["failure"] += 1
                self._modules[module]["upstream_attempts"][route]["failure"] += 1
                source_stats["upstream_attempts"][route]["failure"] += 1
            if successful is not None:
                route = self.upstream_route(
                    successful.proxy_mode,
                    successful.used_proxy,
                )
                self._upstream_attempts[route]["success"] += 1
                self._modules[module]["upstream_attempts"][route]["success"] += 1
                source_stats["upstream_attempts"][route]["success"] += 1

    def stats(self) -> dict[str, object]:
        with self._guard:
            self._checkpoint_stats_locked()
            result: dict[str, object] = self._with_rates(
                self._combined_stats_locked()
            )
            result["scope"] = (
                "persistent-cumulative"
                if self._persist_request_stats
                else "current-process"
            )
            result["current_process"] = self._with_rates(dict(self._stats))
            result["cache_fills"] = self._combined_cache_fills_locked()
            result["current_process_cache_fills"] = {
                route: dict(counters)
                for route, counters in self._cache_fills.items()
            }
            result["upstream_attempts"] = self._combined_upstream_attempts_locked()
            result["current_process_upstream_attempts"] = {
                route: dict(counters)
                for route, counters in self._upstream_attempts.items()
            }
            result["modules"] = self._module_stats_document_locked(
                current_process=False
            )
            result["current_process_modules"] = self._module_stats_document_locked(
                current_process=True
            )
            result["cache_mode"] = self._cache_mode
            result["historical_sessions"] = self._historical_sessions
            result["checkpoint_seconds"] = (
                self._checkpoint_seconds if self._persist_request_stats else None
            )
            result["persisted_at"] = self._persisted_at
            result["persistence_error"] = self._persistence_error
            return result

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._checkpoint_seconds + 1)
        with self._guard:
            self._checkpoint_stats_locked(force=True)
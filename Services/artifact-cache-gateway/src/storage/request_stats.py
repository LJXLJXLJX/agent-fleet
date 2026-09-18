"""Request stats session data classes and persistence schema validation (storage contract layer).

Schema 1/2/3/4 read/write compatibility and the counting structure are taken verbatim from the
original gateway/request_stats.py; the ecosystem-to-statistics-module grouping (REQUEST_MODULES
etc.) stays in gateway/request_stats.py.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import cast
from urllib.parse import urlsplit

from .base import StorageError

REQUEST_STATES = (
    "hit",
    "bypass",
    "miss",
    "refresh",
    "revalidated",
    "stale",
    "error",
)


CACHE_FILL_ROUTES = ("direct", "configured_direct", "configured_proxy")


UPSTREAM_ROUTES = CACHE_FILL_ROUTES


_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


_MODULE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


_SOURCE_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,127}$")


def _valid_source_name(value: str) -> bool:
    if _SOURCE_NAME.fullmatch(value):
        return True
    if len(value) > 2048 or any(ord(character) < 32 for character in value):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.netloc)
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )


def empty_counts() -> dict[str, int]:
    return {state: 0 for state in REQUEST_STATES}


def empty_cache_fills() -> dict[str, dict[str, int]]:
    return {
        route: {"objects": 0, "bytes": 0}
        for route in CACHE_FILL_ROUTES
    }


def empty_upstream_attempts() -> dict[str, dict[str, int]]:
    return {
        route: {"success": 0, "failure": 0}
        for route in UPSTREAM_ROUTES
    }


def empty_source_stats() -> dict[str, object]:
    return {
        "counts": empty_counts(),
        "cache_fills": empty_cache_fills(),
        "upstream_attempts": empty_upstream_attempts(),
    }


def empty_module_stats() -> dict[str, object]:
    return {**empty_source_stats(), "sources": {}}


def validate_cache_fills(value: Mapping[str, object]) -> dict[str, dict[str, int]]:
    if set(value) != set(CACHE_FILL_ROUTES):
        raise StorageError("invalid request stats cache_fills fields")
    result = empty_cache_fills()
    for route in CACHE_FILL_ROUTES:
        counters = value[route]
        if not isinstance(counters, dict) or set(counters) != {"objects", "bytes"}:
            raise StorageError("invalid request stats cache_fills counter")
        for name in ("objects", "bytes"):
            count = counters[name]
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise StorageError("invalid request stats cache_fills count")
            result[route][name] = count
    return result


def validate_upstream_attempts(
    value: Mapping[str, object],
) -> dict[str, dict[str, int]]:
    if set(value) != set(UPSTREAM_ROUTES):
        raise StorageError("invalid request stats upstream_attempts fields")
    result = empty_upstream_attempts()
    for route in UPSTREAM_ROUTES:
        counters = value[route]
        if not isinstance(counters, dict) or set(counters) != {
            "success",
            "failure",
        }:
            raise StorageError("invalid request stats upstream_attempts counter")
        for name in ("success", "failure"):
            count = counters[name]
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise StorageError("invalid request stats upstream_attempts count")
            result[route][name] = count
    return result


def validate_counts(value: Mapping[str, object]) -> dict[str, int]:
    if set(value) != set(REQUEST_STATES):
        raise StorageError("invalid request stats counts fields")
    counts: dict[str, int] = {}
    for state in REQUEST_STATES:
        count = value[state]
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise StorageError("invalid request stats count")
        counts[state] = count
    return counts


def _validate_source_stats(
    value: Mapping[str, object], *, label: str
) -> dict[str, object]:
    if set(value) != {"counts", "cache_fills", "upstream_attempts"}:
        raise StorageError(f"invalid request stats {label} counters")
    counts = value["counts"]
    cache_fills = value["cache_fills"]
    upstream_attempts = value["upstream_attempts"]
    if not isinstance(counts, dict):
        raise StorageError(f"invalid request stats {label} counts")
    if not isinstance(cache_fills, dict):
        raise StorageError(f"invalid request stats {label} cache_fills")
    if not isinstance(upstream_attempts, dict):
        raise StorageError(f"invalid request stats {label} upstream_attempts")
    return {
        "counts": validate_counts(counts),
        "cache_fills": validate_cache_fills(cache_fills),
        "upstream_attempts": validate_upstream_attempts(upstream_attempts),
    }


def validate_modules(
    value: Mapping[str, object], *, include_sources: bool = True
) -> dict[str, dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for module, raw_counters in value.items():
        if not isinstance(module, str) or not _MODULE_NAME.fullmatch(module):
            raise StorageError("invalid request stats module name")
        expected_fields = {
            "counts",
            "cache_fills",
            "upstream_attempts",
        }
        if include_sources:
            expected_fields.add("sources")
        if not isinstance(raw_counters, dict) or set(raw_counters) != expected_fields:
            raise StorageError("invalid request stats module counters")
        parsed = _validate_source_stats(
            {name: raw_counters[name] for name in expected_fields if name != "sources"},
            label="module",
        )
        sources = raw_counters.get("sources", {})
        if not isinstance(sources, dict):
            raise StorageError("invalid request stats module sources")
        parsed_sources: dict[str, dict[str, object]] = {}
        for source, source_counters in sources.items():
            if not isinstance(source, str) or not _valid_source_name(source):
                raise StorageError("invalid request stats source name")
            if not isinstance(source_counters, dict):
                raise StorageError("invalid request stats source counters")
            parsed_sources[source] = _validate_source_stats(
                source_counters, label="source"
            )
        parsed["sources"] = parsed_sources
        result[module] = parsed
    return result


@dataclass(frozen=True)
class RequestStatsSession:
    session_id: str
    started_at: float
    updated_at: float
    counts: dict[str, int]
    cache_fills: dict[str, dict[str, int]]
    upstream_attempts: dict[str, dict[str, int]] = field(
        default_factory=empty_upstream_attempts
    )
    modules: dict[str, dict[str, object]] = field(default_factory=dict)
    label: str | None = None

    def validate(self) -> None:
        if not _SESSION_ID.fullmatch(self.session_id):
            raise StorageError("invalid request stats session_id")
        if not isinstance(self.started_at, (int, float)) or self.started_at <= 0:
            raise StorageError("invalid request stats started_at")
        if not isinstance(self.updated_at, (int, float)) or self.updated_at < self.started_at:
            raise StorageError("invalid request stats updated_at")
        validate_counts(self.counts)
        validate_cache_fills(self.cache_fills)
        validate_upstream_attempts(self.upstream_attempts)
        validate_modules(self.modules)
        if self.label is not None and (not self.label or len(self.label) > 200):
            raise StorageError("invalid request stats label")

    def document(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": 4,
            "session_id": self.session_id,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "counts": dict(self.counts),
            "cache_fills": {
                route: dict(counters)
                for route, counters in self.cache_fills.items()
            },
            "upstream_attempts": {
                route: dict(counters)
                for route, counters in self.upstream_attempts.items()
            },
            "modules": {
                module: {
                    "counts": dict(counters["counts"]),
                    "cache_fills": {
                        route: dict(route_counters)
                        for route, route_counters in counters["cache_fills"].items()
                    },
                    "upstream_attempts": {
                        route: dict(route_counters)
                        for route, route_counters in counters["upstream_attempts"].items()
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
                for module, counters in self.modules.items()
            },
            "label": self.label,
        }

    @classmethod
    def from_document(cls, value: object) -> "RequestStatsSession":
        if not isinstance(value, dict) or value.get("schema_version") not in {1, 2, 3, 4}:
            raise StorageError("invalid request stats document schema")
        schema_version = value["schema_version"]
        expected_fields = {
            "schema_version",
            "session_id",
            "started_at",
            "updated_at",
            "counts",
            "cache_fills",
            "label",
        }
        if schema_version == 2:
            expected_fields.add("upstream_attempts")
        elif schema_version in {3, 4}:
            expected_fields.update(("upstream_attempts", "modules"))
        if set(value) != expected_fields:
            raise StorageError("invalid request stats document fields")
        counts = value["counts"]
        cache_fills = value["cache_fills"]
        upstream_attempts = value.get("upstream_attempts")
        modules = value.get("modules", {})
        if not isinstance(counts, dict):
            raise StorageError("invalid request stats counts")
        if not isinstance(cache_fills, dict):
            raise StorageError("invalid request stats cache_fills")
        if upstream_attempts is not None and not isinstance(upstream_attempts, dict):
            raise StorageError("invalid request stats upstream_attempts")
        if not isinstance(modules, dict):
            raise StorageError("invalid request stats modules")
        session_id = value["session_id"]
        started_at = value["started_at"]
        updated_at = value["updated_at"]
        label = value["label"]
        if not isinstance(session_id, str):
            raise StorageError("invalid request stats session_id")
        if isinstance(started_at, bool) or not isinstance(started_at, (int, float)):
            raise StorageError("invalid request stats started_at")
        if isinstance(updated_at, bool) or not isinstance(updated_at, (int, float)):
            raise StorageError("invalid request stats updated_at")
        if label is not None and not isinstance(label, str):
            raise StorageError("invalid request stats label")
        session = cls(
            session_id=session_id,
            started_at=float(started_at),
            updated_at=float(updated_at),
            counts=validate_counts(counts),
            cache_fills=validate_cache_fills(cache_fills),
            upstream_attempts=(
                empty_upstream_attempts()
                if upstream_attempts is None
                else validate_upstream_attempts(upstream_attempts)
            ),
            modules=validate_modules(modules, include_sources=schema_version >= 4),
            label=cast(str | None, label),
        )
        session.validate()
        return session

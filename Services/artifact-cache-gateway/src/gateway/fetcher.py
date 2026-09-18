from __future__ import annotations

import hashlib
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import (
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from ..core.config import SourceConfig, UpstreamConfig
from ..core.logging_utils import emit_event
from ..storage.base import CacheEntry, Storage


_LOGGER = logging.getLogger("artifact_cache_gateway.upstream")


class FetchError(RuntimeError):
    def __init__(
        self,
        message: str,
        status: int = 502,
        *,
        stage: str = "request",
        upstream_http_status: int | None = None,
        attempts: tuple["UpstreamAttempt", ...] = (),
    ):
        super().__init__(message)
        self.status = status
        self.stage = stage
        self.upstream_http_status = upstream_http_status
        self.attempts = attempts


@dataclass(frozen=True)
class UpstreamAttempt:
    position: int
    label: str
    proxy_mode: str
    stage: str
    duration_seconds: float
    error: str
    used_proxy: bool = False
    http_status: int | None = None
    gateway_status: int = 502

    def document(self) -> dict[str, object]:
        return {
            "position": self.position,
            "label": self.label,
            "proxy_mode": self.proxy_mode,
            "stage": self.stage,
            "duration_seconds": round(self.duration_seconds, 3),
            "error": self.error,
            "used_proxy": self.used_proxy,
            "http_status": self.http_status,
            "gateway_status": self.gateway_status,
        }


@dataclass(frozen=True)
class FetchResult:
    entry: CacheEntry | None
    temp_path: Path | None
    not_modified: bool = False
    upstream_label: str | None = None
    proxy_mode: str | None = None
    used_proxy: bool = False
    size: int = 0
    attempts: tuple[UpstreamAttempt, ...] = ()


class SafeRedirectHandler(HTTPRedirectHandler):
    def __init__(
        self,
        source: SourceConfig,
        upstream: UpstreamConfig,
        candidate_url: str,
    ):
        super().__init__()
        self.source = source
        self.upstream = upstream
        self.candidate_url = candidate_url

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        if not self.source.allows_upstream_url(
            self.upstream,
            newurl,
            candidate_url=self.candidate_url,
        ):
            raise FetchError(
                "upstream redirect is outside source allowlist", stage="redirect"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Fetcher:
    def __init__(
        self,
        storage: Storage,
        proxy_url: str | None,
        timeout: float,
        max_object_bytes: int,
        chunk_bytes: int = 1024 * 1024,
    ):
        self.storage = storage
        self.proxy_url = proxy_url
        self.timeout = timeout
        self.max_object_bytes = max_object_bytes
        self.chunk_bytes = chunk_bytes

    def fetch(
        self,
        source: SourceConfig,
        url: str,
        previous: CacheEntry | None = None,
        *,
        relative_path: str | None = None,
        query: str = "",
        prefer_fallback: bool = False,
    ) -> FetchResult:
        headers = {
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "User-Agent": "artifact-cache-gateway/0.2",
        }
        if previous and previous.etag:
            headers["If-None-Match"] = previous.etag
        if previous and previous.last_modified:
            headers["If-Modified-Since"] = previous.last_modified

        if relative_path is None:
            if not url.startswith(source.base_url):
                raise FetchError("canonical URL does not belong to the source")
            relative_path = url[len(source.base_url) :].split("?", 1)[0]
        candidates = source.fetch_candidates(
            relative_path, query, prefer_fallback=prefer_fallback
        )
        attempts: list[UpstreamAttempt] = []
        for position, (upstream, candidate_url) in enumerate(candidates, start=1):
            started = time.monotonic()
            try:
                result = self._fetch_candidate(
                    source,
                    upstream,
                    candidate_url,
                    url,
                    headers,
                    previous=previous,
                    started=started,
                )
                emit_event(
                    _LOGGER,
                    "upstream_attempt",
                    source=source.name,
                    position=position,
                    label=upstream.label,
                    proxy_mode=upstream.proxy_mode,
                    outcome="success",
                    duration_seconds=round(time.monotonic() - started, 3),
                    size=result.size,
                )
                return FetchResult(
                    entry=result.entry,
                    temp_path=result.temp_path,
                    not_modified=result.not_modified,
                    upstream_label=upstream.label,
                    proxy_mode=upstream.proxy_mode,
                    used_proxy=(
                        upstream.proxy_mode == "configured" and bool(self.proxy_url)
                    ),
                    size=result.size,
                    attempts=tuple(attempts),
                )
            except FetchError as exc:
                attempt = UpstreamAttempt(
                    position=position,
                    label=upstream.label,
                    proxy_mode=upstream.proxy_mode,
                    stage=exc.stage,
                    duration_seconds=time.monotonic() - started,
                    error=str(exc),
                    used_proxy=(
                        upstream.proxy_mode == "configured" and bool(self.proxy_url)
                    ),
                    http_status=exc.upstream_http_status,
                    gateway_status=exc.status,
                )
                attempts.append(attempt)
                emit_event(
                    _LOGGER,
                    "upstream_attempt",
                    level=logging.WARNING,
                    source=source.name,
                    outcome="failed",
                    **attempt.document(),
                )
        if not attempts:
            raise FetchError("source has no available upstream")
        last = attempts[-1]
        status = last.gateway_status if last.gateway_status in {404, 410, 413} else 502
        raise FetchError(
            f"all {len(attempts)} upstream attempts failed; last={last.error}",
            status=status,
            stage=last.stage,
            attempts=tuple(attempts),
        )

    def _fetch_candidate(
        self,
        source: SourceConfig,
        upstream: UpstreamConfig,
        candidate_url: str,
        canonical_url: str,
        headers: dict[str, str],
        *,
        previous: CacheEntry | None,
        started: float,
    ) -> FetchResult:
        proxies = {}
        if upstream.proxy_mode == "configured" and self.proxy_url:
            proxies = {"http": self.proxy_url, "https": self.proxy_url}
        opener = build_opener(
            ProxyHandler(proxies),
            SafeRedirectHandler(source, upstream, candidate_url),
        )
        request = Request(candidate_url, headers=headers, method="GET")
        socket_timeout = self.timeout
        if upstream.attempt_timeout_seconds is not None:
            socket_timeout = min(socket_timeout, upstream.attempt_timeout_seconds)
        try:
            response = opener.open(request, timeout=socket_timeout)
        except FetchError:
            raise
        except HTTPError as exc:
            if exc.code == 304 and previous:
                return FetchResult(entry=None, temp_path=None, not_modified=True)
            status = exc.code if exc.code in {404, 410} else 502
            raise FetchError(
                f"upstream HTTP {exc.code}",
                status=status,
                stage="response",
                upstream_http_status=exc.code,
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise FetchError(
                f"upstream request failed: {type(exc).__name__}", stage="connect"
            ) from exc

        with response:
            final_url = response.geturl()
            if not source.allows_upstream_url(
                upstream,
                final_url,
                candidate_url=candidate_url,
            ):
                raise FetchError(
                    "upstream final URL is outside source allowlist",
                    stage="redirect",
                )
            length_header = response.headers.get("Content-Length")
            if length_header:
                try:
                    content_length = int(length_header)
                except ValueError as exc:
                    raise FetchError(
                        "upstream Content-Length is invalid", stage="response"
                    ) from exc
                if content_length < 0:
                    raise FetchError(
                        "upstream Content-Length is invalid", stage="response"
                    )
                if content_length > self.max_object_bytes:
                    raise FetchError(
                        "upstream object exceeds size limit",
                        status=413,
                        stage="response",
                    )

            stream, temp_path = self.storage.create_temp()
            digest = hashlib.sha256()
            size = 0
            try:
                with stream:
                    while True:
                        try:
                            chunk = response.read(self.chunk_bytes)
                        except (URLError, TimeoutError, OSError) as exc:
                            raise FetchError(
                                f"upstream body read failed: {type(exc).__name__}",
                                stage="read",
                            ) from exc
                        if not chunk:
                            break
                        size += len(chunk)
                        elapsed = max(time.monotonic() - started, 1e-9)
                        if (
                            upstream.attempt_timeout_seconds is not None
                            and elapsed > upstream.attempt_timeout_seconds
                        ):
                            raise FetchError(
                                "upstream attempt exceeded total timeout",
                                stage="read",
                            )
                        if (
                            upstream.slow_after_seconds is not None
                            and upstream.min_bytes_per_second is not None
                            and elapsed >= upstream.slow_after_seconds
                            and size / elapsed < upstream.min_bytes_per_second
                        ):
                            raise FetchError(
                                "upstream throughput is below configured minimum",
                                stage="read",
                            )
                        if size > self.max_object_bytes:
                            raise FetchError(
                                "upstream object exceeds size limit",
                                status=413,
                                stage="read",
                            )
                        digest.update(chunk)
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
                if length_header and size != int(length_header):
                    raise FetchError(
                        "upstream response length is incomplete", stage="read"
                    )
            except BaseException:
                temp_path.unlink(missing_ok=True)
                raise

            content_type = response.headers.get_content_type()
            charset = response.headers.get_content_charset()
            if charset:
                content_type = f"{content_type}; charset={charset}"
            entry = CacheEntry(
                url=canonical_url,
                digest=digest.hexdigest(),
                size=size,
                content_type=content_type or "application/octet-stream",
                fetched_at=time.time(),
                etag=response.headers.get("ETag"),
                last_modified=response.headers.get("Last-Modified"),
            )
            return FetchResult(entry=entry, temp_path=temp_path, size=size)

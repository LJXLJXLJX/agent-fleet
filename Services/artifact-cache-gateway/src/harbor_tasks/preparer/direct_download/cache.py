from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import ProxyHandler, Request, build_opener

from ....gateway.fetcher import FetchError
from ....storage.base import StorageError

if TYPE_CHECKING:
    from ....gateway.engine import Gateway


def _rewrites(plan: dict[str, object]) -> list[dict[str, object]]:
    if plan.get("kind") != "artifact-cache-download-gateway-plan":
        raise ValueError("input is not a download Gateway plan")
    rows = plan.get("rewrites")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("download plan has no rewrites array")
    return rows  # type: ignore[return-value]


def _gateway_origin(gateway_url: str) -> str:
    parsed = urlsplit(gateway_url.rstrip("/"))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("gateway URL must be absolute HTTP(S)")
    if parsed.query or parsed.fragment:
        raise ValueError("gateway URL must not contain query or fragment")
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _warm_one(
    row: dict[str, object], *, origin: str, timeout_seconds: float
) -> dict[str, object]:
    gateway_path = row.get("gateway_path")
    source = row.get("source")
    relative_path = row.get("relative_path")
    refresh_policy = row.get("refresh_policy")
    if (
        not isinstance(gateway_path, str)
        or not gateway_path.startswith("/v1/cache/")
        or not isinstance(source, str)
        or not isinstance(relative_path, str)
        or refresh_policy not in {"immutable", "manual"}
    ):
        return {
            "source": str(source),
            "relative_path": str(relative_path),
            "status": "failed",
            "reason": "invalid reviewed rewrite row",
        }
    url = origin + gateway_path
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(Request(url, method="GET"), timeout=timeout_seconds) as response:
            first_state = response.headers.get("X-Artifact-Cache", "UNKNOWN")
            size = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
        with opener.open(Request(url, method="HEAD"), timeout=timeout_seconds) as response:
            verification_state = response.headers.get("X-Artifact-Cache", "UNKNOWN")
        cached = verification_state == "HIT"
        return {
            "source": source,
            "relative_path": relative_path,
            "refresh_policy": refresh_policy,
            "status": (
                "already-cached" if first_state == "HIT" and cached
                else "cached" if cached
                else "failed"
            ),
            "reason": (
                "verified S3 cache hit"
                if cached
                else "verification request did not return HIT"
            ),
            "first_cache_state": first_state,
            "verification_cache_state": verification_state,
            "size": size,
        }
    except HTTPError as exc:
        return {
            "source": source,
            "relative_path": relative_path,
            "refresh_policy": refresh_policy,
            "status": "failed",
            "reason": f"gateway HTTP {exc.code}",
        }
    except (OSError, URLError, TimeoutError) as exc:
        return {
            "source": source,
            "relative_path": relative_path,
            "refresh_policy": refresh_policy,
            "status": "failed",
            "reason": type(exc).__name__,
        }


def warm_downloads(
    plan: dict[str, object],
    *,
    gateway_url: str,
    timeout_seconds: float,
    concurrency: int,
) -> dict[str, object]:
    if timeout_seconds <= 0:
        raise ValueError("download warm timeout must be positive")
    if concurrency < 1 or concurrency > 32:
        raise ValueError("download warm concurrency must be between 1 and 32")
    rows = _rewrites(plan)
    origin = _gateway_origin(gateway_url)
    results: list[dict[str, object]] = []
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        pending = {
            executor.submit(
                _warm_one,
                row,
                origin=origin,
                timeout_seconds=timeout_seconds,
            ): index
            for index, row in enumerate(rows)
        }
        ordered: dict[int, dict[str, object]] = {}
        for future in as_completed(pending):
            ordered[pending[future]] = future.result()
        results = [ordered[index] for index in range(len(rows))]
    failures = sum(row["status"] == "failed" for row in results)
    return {
        "schema_version": 1,
        "kind": "artifact-cache-download-warm-result",
        "dataset": plan.get("dataset"),
        "summary": {
            "objects": len(results),
            "cached": sum(row["status"] == "cached" for row in results),
            "already_cached": sum(
                row["status"] == "already-cached" for row in results
            ),
            "failures": failures,
            "concurrency": concurrency,
        },
        "results": results,
    }


def _refresh_one(
    row: dict[str, object], *, gateway: "Gateway", include_immutable: bool
) -> dict[str, object]:
    source_name = row.get("source")
    relative_path = row.get("relative_path")
    refresh_policy = row.get("refresh_policy")
    if (
        not isinstance(source_name, str)
        or not isinstance(relative_path, str)
        or refresh_policy not in {"immutable", "manual"}
    ):
        return {
            "source": str(source_name),
            "relative_path": str(relative_path),
            "status": "failed",
            "reason": "invalid reviewed rewrite row",
        }
    if refresh_policy == "immutable" and not include_immutable:
        return {
            "source": source_name,
            "relative_path": relative_path,
            "refresh_policy": refresh_policy,
            "status": "skipped",
            "reason": "immutable object requires --include-immutable",
        }
    try:
        source = gateway.config.source(source_name)
        canonical_url = source.build_url(relative_path)
        previous = gateway.storage.load(canonical_url)
        result = gateway.resolve(
            source_name,
            relative_path,
            "",
            force_refresh=True,
        )
        try:
            same_digest = (
                previous is not None and previous.digest == result.entry.digest
            )
            status = {
                "MISS": "cached",
                "REFRESH": "unchanged" if same_digest else "refreshed",
                "REVALIDATED": "unchanged",
            }.get(result.state, "failed")
            return {
                "source": source_name,
                "relative_path": relative_path,
                "refresh_policy": refresh_policy,
                "status": status,
                "reason": (
                    "atomically published refreshed metadata"
                    if status in {"cached", "refreshed"}
                    else "upstream confirmed existing object"
                    if status == "unchanged"
                    else f"unexpected cache state: {result.state}"
                ),
                "previous_digest": previous.digest if previous else None,
                "digest": result.entry.digest,
                "size": result.entry.size,
            }
        finally:
            gateway.release_result(result)
    except (FetchError, StorageError, OSError, ValueError) as exc:
        return {
            "source": source_name,
            "relative_path": relative_path,
            "refresh_policy": refresh_policy,
            "status": "failed",
            "reason": str(exc),
        }


def refresh_downloads(
    plan: dict[str, object],
    *,
    gateway: "Gateway",
    concurrency: int,
    include_immutable: bool = False,
) -> dict[str, object]:
    if concurrency < 1 or concurrency > 16:
        raise ValueError("download refresh concurrency must be between 1 and 16")
    rows = _rewrites(plan)
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        pending = {
            executor.submit(
                _refresh_one,
                row,
                gateway=gateway,
                include_immutable=include_immutable,
            ): index
            for index, row in enumerate(rows)
        }
        ordered = {
            pending[future]: future.result() for future in as_completed(pending)
        }
    results = [ordered[index] for index in range(len(rows))]
    failures = sum(row["status"] == "failed" for row in results)
    return {
        "schema_version": 1,
        "kind": "artifact-cache-download-refresh-result",
        "dataset": plan.get("dataset"),
        "summary": {
            "objects": len(results),
            "cached": sum(row["status"] == "cached" for row in results),
            "refreshed": sum(row["status"] == "refreshed" for row in results),
            "unchanged": sum(row["status"] == "unchanged" for row in results),
            "skipped": sum(row["status"] == "skipped" for row in results),
            "failures": failures,
            "concurrency": concurrency,
            "include_immutable": include_immutable,
        },
        "results": results,
    }

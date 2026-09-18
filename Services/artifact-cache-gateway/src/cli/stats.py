from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ..storage.base import Storage, StorageError
from ..storage.gpfs import FileStorage
from ..storage.request_stats import (
    RequestStatsSession,
    empty_cache_fills,
    empty_upstream_attempts,
)
from ..storage.s3 import S3Settings, S3Storage


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Idempotently import a historical Gateway request-stat session"
    )
    result.add_argument(
        "--storage",
        choices=("s3", "file"),
        default=os.environ.get("ARTIFACT_CACHE_STORAGE", "s3"),
    )
    result.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(os.environ.get("ARTIFACT_CACHE_DIR", "/data/artifact-cache")),
    )
    result.add_argument("--session-id", required=True)
    result.add_argument("--label", required=True)
    result.add_argument("--started-at", required=True, type=float)
    result.add_argument("--updated-at", required=True, type=float)
    states = (
        "hit",
        "bypass",
        "miss",
        "refresh",
        "revalidated",
        "stale",
        "error",
    )
    for state in states:
        result.add_argument(f"--{state}", type=int, default=0)
    for route in ("direct", "configured-direct", "configured-proxy"):
        result.add_argument(f"--{route}-objects", type=int, default=0)
        result.add_argument(f"--{route}-bytes", type=int, default=0)
        result.add_argument(f"--{route}-success", type=int, default=0)
        result.add_argument(f"--{route}-failure", type=int, default=0)
    return result


def _storage(args: argparse.Namespace) -> Storage:
    if args.storage == "s3":
        return S3Storage(S3Settings.from_env(), work_dir=args.cache_dir)
    return FileStorage(args.cache_dir)


def main() -> None:
    args = parser().parse_args()
    counts = {
        state: getattr(args, state)
        for state in (
            "hit",
            "bypass",
            "miss",
            "refresh",
            "revalidated",
            "stale",
            "error",
        )
    }
    cache_fills = empty_cache_fills()
    for route in cache_fills:
        cache_fills[route] = {
            "objects": getattr(args, f"{route}_objects"),
            "bytes": getattr(args, f"{route}_bytes"),
        }
    upstream_attempts = empty_upstream_attempts()
    for route in upstream_attempts:
        upstream_attempts[route] = {
            "success": getattr(args, f"{route}_success"),
            "failure": getattr(args, f"{route}_failure"),
        }
    session = RequestStatsSession(
        session_id=args.session_id,
        started_at=args.started_at,
        updated_at=args.updated_at,
        counts=counts,
        cache_fills=cache_fills,
        upstream_attempts=upstream_attempts,
        label=args.label,
    )
    session.validate()
    storage = _storage(args)
    existing = {
        item.session_id: item for item in storage.load_request_stats()
    }.get(session.session_id)
    if existing is not None and existing != session:
        raise StorageError(
            "a request stats session with the same id already exists with different content"
        )
    if existing is None:
        storage.save_request_stats(session)
    print(
        json.dumps(
            {
                "session_id": session.session_id,
                "status": "already-present" if existing is not None else "imported",
                "lookup_total": sum(
                    count for state, count in counts.items() if state != "error"
                ),
                "request_total": sum(counts.values()),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()

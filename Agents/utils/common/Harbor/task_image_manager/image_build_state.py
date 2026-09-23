"""Local service-image records, build locks, and log paths."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from pathlib import Path


def image_build_state_paths(
    cache_root: Path, repository: str, tag: str, task_identity: str
) -> tuple[Path, Path, Path]:
    """Return the lock, record, and log paths for one service image tag."""
    target_key = hashlib.sha256(repository.encode("utf-8")).hexdigest()[:16]
    lock_path = cache_root / "locks" / "images" / f"{target_key}-{tag}.lock"
    record_path = cache_root / "records" / "images" / target_key / f"{tag}.json"
    log_path = cache_root / "logs" / task_identity / f"{tag}.log"
    return lock_path, record_path, log_path


@contextmanager
def exclusive_image_build_lock(lock_path: Path):
    """Serialize preparation of one target image tag across local workers.

    Hold the lock through Registry reuse checks, building, and publication
    to prevent duplicate builds. Running an image does not require this lock.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        yield


def read_json_record(path: Path) -> dict[str, object]:
    """Read a persisted image/Bundle record; missing or invalid JSON is a miss."""
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)

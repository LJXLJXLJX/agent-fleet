"""Gateway resolution result (CacheResult)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ...core.config import SourceConfig
from ...storage.base import CacheEntry


@dataclass(frozen=True)
class CacheResult:
    entry: CacheEntry
    state: str
    source: SourceConfig
    temp_path: Path | None = None

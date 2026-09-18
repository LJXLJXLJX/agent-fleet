from __future__ import annotations

from pathlib import Path
from typing import Protocol, Sequence

from ..models import ProbeResult, ProbeSettings, WarmResult


class PackageProvider(Protocol):
    """Contract implemented independently by each package ecosystem."""

    manager: str

    def probe(
        self,
        package_rows: Sequence[dict[str, object]],
        settings: ProbeSettings,
        *,
        work_dir: Path,
    ) -> list[ProbeResult]: ...

    def warm(
        self,
        rows: Sequence[dict[str, object]],
        *,
        gateway_url: str,
        timeout_seconds: float,
        work_dir: Path,
    ) -> list[WarmResult]: ...

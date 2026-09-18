"""Storage contract layering guard (Phase 3, 2026-09-17).

storage/ is the storage contract layer: the S3/GPFS inventory records and request-stats
session persistence reads/writes must not depend on the gateway runtime (the reverse of the
gateway → storage one-way dependency must be zero).
"""

from __future__ import annotations

import ast
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
STORAGE_DIR = REPO_ROOT / "src" / "storage"


class StorageContractStaticTest(unittest.TestCase):
    """Static guard: no import in any storage/ .py may reference gateway (any level/form)."""

    def test_storage_has_no_gateway_imports(self) -> None:
        failures = []
        for path in sorted(STORAGE_DIR.glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith("artifact_cache_gateway.gateway"):
                            failures.append(f"{path.name}: import {alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    dotted = "." * node.level + (node.module or "")
                    normalized = dotted.lstrip(".")
                    if (
                        module := node.module or ""
                    ) and module.startswith("artifact_cache_gateway.gateway"):
                        failures.append(f"{path.name}: from {dotted} import ...")
                    # Relative imports inside the storage package are only allowed within the
                    # same package (. / ..), never ../gateway
                    if normalized == "gateway" or normalized.startswith("gateway."):
                        failures.append(f"{path.name}: relative gateway import {dotted}")
        self.assertEqual(failures, [])


class StorageContractIsolationTest(unittest.TestCase):
    """Isolated-process verification: the serialization path does not load gateway."""

    def test_serialization_roundtrip_without_gateway(self) -> None:
        script = r'''
import sys
from artifact_cache_gateway.storage.inventory import CacheObject, InventoryListing
from artifact_cache_gateway.storage.request_stats import (
    RequestStatsSession,
    empty_cache_fills,
    empty_counts,
    empty_module_stats,
    empty_upstream_attempts,
)

obj = CacheObject(
    object_id="a" * 64,
    source="pypi",
    ecosystem="pip",
    package="numpy",
    version="1.26.0",
    filename="numpy-1.26.0.tar.gz",
    relative_path="numpy/",
    object_type="artifact",
    digest="b" * 64,
    size=1,
    content_type="application/octet-stream",
    fetched_at=1.0,
)
restored = CacheObject.from_document(obj.document())
assert restored == obj
listing = InventoryListing(children=("a",), next_cursor=None)

session = RequestStatsSession(
    session_id="s-1",
    started_at=1.0,
    updated_at=2.0,
    counts=empty_counts(),
    cache_fills=empty_cache_fills(),
    upstream_attempts=empty_upstream_attempts(),
    modules={"pypi": {**empty_module_stats(), "sources": {}}},
    label=None,
)
restored_session = RequestStatsSession.from_document(session.document())
assert restored_session == session

loaded = [
    name for name in sys.modules
    if name.startswith("artifact_cache_gateway.gateway")
]
assert not loaded, f"gateway modules loaded: {loaded}"
'''
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()

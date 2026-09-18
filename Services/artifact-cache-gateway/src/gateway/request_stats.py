"""Mapping from ecosystems to the runtime stats modules.

The counting-structure helpers, schema validation and the `RequestStatsSession` data
class live in `storage/request_stats.py` (the storage contract layer, shared by S3/GPFS
persistence reads and writes); this module only handles stats-module-name grouping and
composing `empty_modules` (the module list is gateway-side knowledge).
"""

from __future__ import annotations

from ..storage.request_stats import empty_module_stats

REQUEST_MODULES = (
    "apt",
    "pypi",
    "npm",
    "go",
    "rust",
    "dart",
    "julia",
    "curl",
    "git_clone",
)
LEGACY_REQUEST_MODULE = "unclassified"


def empty_modules(*, include_legacy: bool = False) -> dict[str, dict[str, object]]:
    names = REQUEST_MODULES + ((LEGACY_REQUEST_MODULE,) if include_legacy else ())
    return {name: empty_module_stats() for name in names}


def request_module_for_ecosystem(ecosystem: str) -> str:
    return {
        "apt": "apt",
        "pip": "pypi",
        "node": "npm",
        "go": "go",
        "cargo": "rust",
        "dart": "dart",
        "julia": "julia",
        "download": "curl",
        "generic": "curl",
    }[ecosystem]

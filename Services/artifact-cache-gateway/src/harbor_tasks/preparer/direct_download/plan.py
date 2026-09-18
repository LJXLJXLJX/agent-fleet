from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from ....core.config import SourceConfig
from ....core.source_naming import (
    canonical_download_origin,
    download_gateway_path,
    frozen_download_relative_path,
    frozen_download_source_name,
)


class DownloadGatewayPlanError(ValueError):
    """Raised when analyzed downloads cannot become a safe Gateway plan."""


_VERSION = re.compile(r"(?<![A-Za-z0-9])v?\d+\.\d+(?:\.\d+)?(?:[-._][A-Za-z0-9]+)*")
_CONTENT_DIGEST = re.compile(r"(?:^|[-_/])[0-9a-f]{12,64}(?:[-_.\/]|$)", re.IGNORECASE)
_MUTABLE_SEGMENTS = {"main", "master", "develop", "development", "latest", "stable"}
_MUTABLE_FILENAMES = {
    "install.sh",
    "get-pip.py",
    "minio",
    "rustup-init.sh",
}
_KNOWN_REDIRECT_ORIGINS = {
    "https://crates.io/": ["https://static.crates.io"],
    "https://dl.min.io/": [
        "https://github.com",
        "https://release-assets.githubusercontent.com",
    ],
    "https://dot.net/": ["https://builds.dotnet.microsoft.com"],
    "https://downloads.sourceforge.net/": [
        "https://onboardcloud.dl.sourceforge.net",
    ],
    "https://foundry.paradigm.xyz/": ["https://raw.githubusercontent.com"],
    "https://github.com/": [
        "https://codeload.github.com",
        "https://raw.githubusercontent.com",
        "https://release-assets.githubusercontent.com",
        "https://objects.githubusercontent.com",
    ],
    "https://go.dev/": ["https://dl.google.com"],
    "https://golang.org/": ["https://go.dev", "https://dl.google.com"],
    "https://huggingface.co/": [
        "https://cas-bridge.xethub.hf.co",
        "https://cdn-lfs.huggingface.co",
        "https://us.aws.cdn.hf.co",
    ],
    "https://pypi.io/": [
        "https://pypi.org",
        "https://files.pythonhosted.org",
    ],
    "https://pypi.python.org/": [
        "https://pypi.org",
        "https://files.pythonhosted.org",
    ],
    "https://sourceforge.net/": [
        "https://downloads.sourceforge.net",
        "https://onboardcloud.dl.sourceforge.net",
        "https://twds.dl.sourceforge.net",
    ],
}
_KNOWN_EXISTING_ROUTES = {
    "https://sh.rustup.rs/": ("rustup-init", "rustup-init.sh"),
}
def classify_refresh_policy(url: str) -> str:
    """Conservatively classify explicit downloads as immutable or manual."""

    parsed = urlsplit(url)
    segments = [segment.lower() for segment in parsed.path.split("/") if segment]
    filename = segments[-1] if segments else ""
    if (
        not filename
        or filename in _MUTABLE_FILENAMES
        or any(segment in _MUTABLE_SEGMENTS for segment in segments)
        or any(marker in filename for marker in ("latest", "snapshot", "nightly"))
        or re.fullmatch(r"setup_\d+\.x", filename)
    ):
        return "manual"
    if (
        "/archive/refs/tags/" in parsed.path.lower()
        or "/releases/download/" in parsed.path.lower()
        or _VERSION.search(parsed.path)
        or _CONTENT_DIGEST.search(parsed.path)
    ):
        return "immutable"
    return "manual"


def _source_name(origin: str) -> str:
    return frozen_download_source_name(origin)


def _candidate_rows(report: dict[str, object]) -> list[dict[str, object]]:
    if report.get("kind") != "artifact-cache-external-build-input-report":
        raise DownloadGatewayPlanError("input is not an external build input report")
    rows = report.get("cache_candidates")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise DownloadGatewayPlanError("external report has no cache_candidates array")
    return rows  # type: ignore[return-value]


def compile_download_gateway_plan(report: dict[str, object]) -> dict[str, object]:
    updated_at = date.today().isoformat()
    origins: set[str] = set()
    candidates: dict[str, dict[str, object]] = {}
    rejected: list[dict[str, str]] = []

    for row in _candidate_rows(report):
        if row.get("kind") != "http-download" or row.get("action") != "cache":
            continue
        raw_url = row.get("url")
        if not isinstance(raw_url, str):
            rejected.append({"url": str(raw_url), "reason": "missing URL"})
            continue
        parsed = urlsplit(raw_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.query
            or parsed.fragment
            or row.get("query_present") is True
            or row.get("credentials_present") is True
        ):
            rejected.append(
                {
                    "url": raw_url,
                    "reason": "URL is not credential-free, query-free HTTP(S)",
                }
            )
            continue
        if raw_url in _KNOWN_EXISTING_ROUTES:
            candidates[raw_url] = {
                "url": raw_url,
                "refresh_policy": classify_refresh_policy(raw_url),
                "existing_route": _KNOWN_EXISTING_ROUTES[raw_url],
            }
            continue
        try:
            origin = canonical_download_origin(raw_url)
            gateway_path = download_gateway_path(raw_url)
        except ValueError as exc:
            rejected.append({"url": raw_url, "reason": str(exc)})
            continue
        path = None if parsed.path in {"", "/"} else parsed.path
        origins.add(origin)
        candidates[raw_url] = {
            "url": raw_url,
            "origin": origin,
            "relative_path": frozen_download_relative_path(path),
            "gateway_path": gateway_path,
            "refresh_policy": classify_refresh_policy(raw_url),
        }

    sources: list[dict[str, object]] = []
    source_names: dict[str, str] = {}
    for origin in sorted(origins):
        name = _source_name(origin)
        source_names[origin] = name
        base_url = f"{origin}/"
        source = {
            "name": name,
            "kind": "frozen-download",
            "ecosystem": "download",
            "base_url": base_url,
            "allowed_redirect_origins": _KNOWN_REDIRECT_ORIGINS.get(base_url, []),
            "allow_query": False,
            "proxy_mode": "configured",
            "config_updated_at": updated_at,
            "config_update_policy": "manual",
        }
        SourceConfig.from_dict(source)
        sources.append(source)

    rewrites = []
    for raw_url, candidate in sorted(candidates.items()):
        existing_route = candidate.get("existing_route")
        if existing_route is not None:
            source, path = existing_route
        else:
            source = source_names[str(candidate["origin"])]
            path = str(candidate["relative_path"])
        rewrites.append(
            {
                "kind": "http-download",
                "upstream_url": raw_url,
                "source": source,
                "relative_path": path,
                "gateway_path": candidate.get(
                    "gateway_path", f"/v1/cache/{source}/{path}"
                ),
                "refresh_policy": candidate["refresh_policy"],
            }
        )

    return {
        "schema_version": 2,
        "kind": "artifact-cache-download-gateway-plan",
        "dataset": report.get("dataset"),
        "sources": sources,
        "rewrites": rewrites,
        "rejected": rejected,
        "summary": {
            "sources": len(sources),
            "rewrites": len(rewrites),
            "immutable": sum(row["refresh_policy"] == "immutable" for row in rewrites),
            "manual": sum(row["refresh_policy"] == "manual" for row in rewrites),
            "rejected": len(rejected),
        },
    }


def merge_download_plan_directory(
    base_document: dict[str, object], directory: Path
) -> tuple[dict[str, object], tuple[Path, ...]]:
    """Merge reviewed v1 exact-object and v2 frozen-origin download plans."""

    from ....gateway.services.apt import merge_gateway_config

    if not directory.is_dir():
        raise DownloadGatewayPlanError(
            f"download plan directory does not exist: {directory}"
        )
    merged = base_document
    loaded: list[Path] = []
    for path in sorted(directory.glob("*.json")):
        if not path.is_file():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DownloadGatewayPlanError(
                f"invalid download plan: {path.name}"
            ) from exc
        if (
            not isinstance(value, dict)
            or value.get("schema_version") not in {1, 2}
            or value.get("kind") != "artifact-cache-download-gateway-plan"
        ):
            raise DownloadGatewayPlanError(
                f"invalid download plan: {path.name}"
            )
        merged = merge_gateway_config(merged, value)
        loaded.append(path)
    return merged, tuple(loaded)

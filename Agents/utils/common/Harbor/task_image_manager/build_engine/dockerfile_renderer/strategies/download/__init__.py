"""curl/wget runtime secret rendering and source-root validation."""

from __future__ import annotations

import hashlib
from pathlib import Path

from .....source_urls import validate_source_url

DOWNLOAD_RUNTIME_ASSET_NAMES = (
    "download-wrapper.sh",
    "url-rewriter.awk",
)
DOWNLOAD_RUNTIME_ASSET_DIR = Path(__file__).resolve().parent


def validate_download_source_url(value: str, build_network: str) -> str:
    """Validate the optional provider-neutral curl/wget and APT source root."""
    if not value.strip():
        return ""
    return validate_source_url(
        value, "curl/wget download source", build_network
    ).rstrip("/")


def materialize_download_runtime_assets(
    destination: Path, source_url: str
) -> dict[str, Path]:
    """Materialize content-addressed curl/wget runtime secrets."""
    destination.mkdir(parents=True, exist_ok=True)
    assets = {
        name: DOWNLOAD_RUNTIME_ASSET_DIR / name for name in DOWNLOAD_RUNTIME_ASSET_NAMES
    }
    secret_files: dict[str, Path] = {}
    for name, prefix in (
        ("download-wrapper.sh", "opensandbox-download-wrapper"),
        ("url-rewriter.awk", "opensandbox-download-url-rewriter"),
    ):
        path = assets[name]
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise RuntimeError(
                f"Harbor task image download runtime asset is unavailable: {path}"
            ) from exc
        secret_files[f"{prefix}-{hashlib.sha256(content).hexdigest()}"] = path
    source_path = destination / "source"
    source_path.write_text(source_url.rstrip("/") + "\n", encoding="utf-8")
    source_digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    secret_files[f"opensandbox-download-source-{source_digest}"] = source_path
    return secret_files

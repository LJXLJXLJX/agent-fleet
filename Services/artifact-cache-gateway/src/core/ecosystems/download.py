"""download ecosystem hooks: the same-origin uniqueness constraint for frozen/transparent-download and always_publish (publish directly, skipping the publish policy)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from ._origin import _origin
from .base import EcosystemHandler

if TYPE_CHECKING:
    from ..config.source import SourceConfig


def _no_download_path(source: SourceConfig, decoded: str) -> str | None:
    return None


def _validate_download_config(
    source: SourceConfig, sources: Mapping[str, SourceConfig]
) -> str | None:
    if source.kind != "frozen-download":
        return None
    origin = _origin(source.base_url)
    for other in sources.values():
        if other is source:
            break
        if (
            other.kind == "frozen-download"
            and _origin(other.base_url) == origin
        ):
            return (
                "the same origin cannot host multiple frozen-download sources: "
                f"{origin} ({other.name}, {source.name})"
            )
    return None


def _always_publish(source: SourceConfig) -> bool:
    return True

HANDLER = EcosystemHandler(
    name="download",
    kinds=("frozen-download", "transparent-download"),
    ecosystems=("download",),
    validate_path=_no_download_path,
    validate_config=_validate_download_config,
    always_publish=_always_publish,
)

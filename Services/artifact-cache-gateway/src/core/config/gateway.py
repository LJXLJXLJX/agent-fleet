from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..ecosystems import handler_for_kind
from .errors import ConfigError
from .source import SourceConfig


@dataclass(frozen=True)
class GatewayConfig:
    sources: dict[str, SourceConfig]

    def __post_init__(self) -> None:
        for source in self.sources.values():
            if source.name in {"apt", "download"}:
                raise ConfigError(
                    f"source name {source.name} is reserved for versioned dynamic routing"
                )
            handler = handler_for_kind(source.kind)
            if handler is not None and handler.validate_config is not None:
                message = handler.validate_config(source, self.sources)
                if message is not None:
                    raise ConfigError(message)
            for _rewrite_origin, target in source.html_rewrite_routes:
                if target not in self.sources:
                    raise ConfigError(
                        f"source {source.name!r} references a missing HTML rewrite target: {target!r}"
                    )
            for _prefix, target in source.html_rewrite_relative_routes:
                if target not in self.sources:
                    raise ConfigError(
                        f"source {source.name!r} references a missing HTML relative rewrite target: {target!r}"
                    )
            if handler is not None and handler.validate_config is not None:
                message = handler.validate_config(source, self.sources)
                if message is not None:
                    raise ConfigError(message)

    def source(self, name: str) -> SourceConfig:
        try:
            return self.sources[name]
        except KeyError as exc:
            raise ConfigError(f"unknown source: {name}") from exc
def default_config() -> GatewayConfig:
    source = SourceConfig.from_dict(
        {
            "name": "pytorch",
            "base_url": "https://download.pytorch.org/",
            "allowed_redirect_origins": [],
            "html_rewrite_origins": ["https://download-r2.pytorch.org"],
            "rewrite_html": True,
            "ecosystem": "pip",
        }
    )
    return GatewayConfig(sources={source.name: source})
def config_from_document(document: object) -> GatewayConfig:
    raw_sources = document.get("sources") if isinstance(document, dict) else None
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ConfigError("config must provide a non-empty sources array")
    sources = [SourceConfig.from_dict(value) for value in raw_sources]
    by_name = {source.name: source for source in sources}
    if len(by_name) != len(sources):
        raise ConfigError("source names must be unique")
    return GatewayConfig(sources=by_name)
def load_config(path: Path | None) -> GatewayConfig:
    if path is None:
        return default_config()
    with path.open("r", encoding="utf-8") as stream:
        document = json.load(stream)
    return config_from_document(document)

"""Ecosystem hook registry: aggregates each module's HANDLER and builds a single kind/ecosystem double index.

Layering: gateway → core.ecosystems (this package only depends at runtime on the
config.errors leaf module; SourceConfig/CacheResult/CacheEntry are TYPE_CHECKING only). To add an
ecosystem, add a module to this directory and register one line in _HANDLERS (see the README's
"Adding an Ecosystem" guide).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import apt, cargo, dart, download, go, julia, node, pip
from .base import EcosystemHandler

if TYPE_CHECKING:
    from ..config.source import SourceConfig

_HANDLERS = (
    pip.HANDLER,
    node.HANDLER,
    go.HANDLER,
    cargo.HANDLER,
    dart.HANDLER,
    julia.HANDLER,
    download.HANDLER,
    apt.HANDLER,
)

KIND_HANDLERS: dict[str, EcosystemHandler] = {}
ECOSYSTEM_HANDLERS: dict[str, EcosystemHandler] = {}
for _handler in _HANDLERS:
    for _kind in _handler.kinds:
        KIND_HANDLERS[_kind] = _handler
    for _ecosystem in _handler.ecosystems:
        ECOSYSTEM_HANDLERS[_ecosystem] = _handler
del _handler, _kind, _ecosystem


def handler_for_kind(kind: str) -> EcosystemHandler | None:
    return KIND_HANDLERS.get(kind)


def handler_for_ecosystem(ecosystem: str) -> EcosystemHandler | None:
    return ECOSYSTEM_HANDLERS.get(ecosystem)


def always_publish(source: SourceConfig) -> bool:
    handler = KIND_HANDLERS.get(source.kind)
    return handler.always_publish(source) if handler is not None else False


def display_name(source: SourceConfig, request_url: str | None = None) -> str | None:
    handler = ECOSYSTEM_HANDLERS.get(source.ecosystem)
    if handler is not None and handler.display_name is not None:
        return handler.display_name(source, request_url)
    handler = KIND_HANDLERS.get(source.kind)
    if handler is not None and handler.display_name is not None:
        return handler.display_name(source, request_url)
    return None


__all__ = [
    "ECOSYSTEM_HANDLERS",
    "KIND_HANDLERS",
    "EcosystemHandler",
    "always_publish",
    "display_name",
    "handler_for_ecosystem",
    "handler_for_kind",
]

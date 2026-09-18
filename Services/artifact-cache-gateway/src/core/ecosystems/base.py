"""Unified ecosystem hook protocol (core layer; all hooks are pure functions that duck-type source/result at runtime).

Field default semantics: optional callables default to None (trunk checks None before branching to
the default path), rewrite_matches/always_publish default to always False, and rewrite_size_limit
defaults to 0. CacheResult/CacheEntry are referenced only under TYPE_CHECKING; no gateway runtime
types are imported.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...gateway.engine.result import CacheResult
    from ...storage.base import CacheEntry
    from ..config.source import SourceConfig


def _rewrite_matches_false(result: CacheResult, entry: CacheEntry) -> bool:
    return False


def _always_publish_false(source: SourceConfig) -> bool:
    return False


@dataclass(frozen=True)
class EcosystemHandler:
    """The set of behavior hooks for an ecosystem/kind.

    Load-time hooks: validate_path(source, decoded) returns the error message text or None;
    validate_config(source, sources) returns the message for a cross-source constraint or None.
    Service-time hooks: prepare_html(source, rewritten) runs any kind-specific preprocessing before
    HTML rewriting; rewrite_matches(result, entry) decides kind-specific rewriting; rewrite(content,
    result, *, gateway_origin, archive_source=None) performs the rewriting; rewrite_size_limit is the
    byte cap for rewriting; inventory_fields(source, decoded_path, filename, content_type) returns an
    inventory field dict (None means fall back to the trunk default); display_name(source,
    request_url) returns the stats display name (None means fall back to source.name);
    always_publish(source) publishes directly, skipping the publish policy.
    """

    name: str = ""
    kinds: tuple[str, ...] = ()
    ecosystems: tuple[str, ...] = ()
    validate_path: Callable[[SourceConfig, str], str | None] | None = None
    validate_config: Callable[
        [SourceConfig, Mapping[str, SourceConfig]], str | None
    ] | None = None
    prepare_html: Callable[[SourceConfig, bytes], bytes] | None = None
    rewrite_matches: Callable[[CacheResult, CacheEntry], bool] = _rewrite_matches_false
    rewrite: Callable[..., bytes] | None = None
    rewrite_size_limit: int = 0
    inventory_fields: Callable[..., dict[str, str | None]] | None = None
    display_name: Callable[
        [SourceConfig, str | None], str | None
    ] | None = None
    always_publish: Callable[[SourceConfig], bool] = _always_publish_false

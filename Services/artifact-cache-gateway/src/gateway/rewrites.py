"""Common core for HTML index rewriting (kind-specialized hooks live in core/ecosystems/)."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from ..core.ecosystems import handler_for_kind
from .engine import CacheResult

_MAX_REWRITTEN_HTML_BYTES = 16 * 1024 * 1024
_MAX_REWRITTEN_RUSTUP_INIT_BYTES = 1024 * 1024


def rewrite_index(content: bytes, result: CacheResult) -> bytes:
    if not result.source.rewrite_html:
        return content
    route = f"/v1/cache/{result.source.name}/".encode("ascii")
    rewritten = content
    handler = handler_for_kind(result.source.kind)
    if handler is not None and handler.prepare_html is not None:
        rewritten = handler.prepare_html(result.source, rewritten)
    base_path = urlsplit(result.source.base_url).path
    for prefix in result.source.html_rewrite_url_prefixes:
        parsed_prefix = urlsplit(prefix)
        protocol_relative = f"//{parsed_prefix.netloc}{parsed_prefix.path}"
        rewritten = rewritten.replace(prefix.encode("utf-8"), route)
        rewritten = rewritten.replace(protocol_relative.encode("utf-8"), route)
        escaped_path = re.escape(parsed_prefix.path.encode("utf-8"))
        rewritten = re.sub(
            rb"(?i)((?:href|src)\s*=\s*['\"])" + escaped_path,
            lambda match: match.group(1) + route,
            rewritten,
        )
    parsed = urlsplit(result.source.base_url)
    if parsed.path == "/":
        # Rewrite upstream-root-relative links before absolute-origin links.
        # Otherwise an absolute URL rewritten to our own root-relative route
        # would be rewritten a second time (for example /v1/cache/pytorch/...).
        rewritten = re.sub(
            rb"(?i)((?:href|src)\s*=\s*['\"])/",
            lambda match: match.group(1) + route,
            rewritten,
        )
    for origin in result.source.html_rewrite_origins:
        origin_prefix = f"{origin}{base_path}".encode("utf-8")
        protocol_relative = f"//{urlsplit(origin).netloc}{base_path}".encode("utf-8")
        rewritten = rewritten.replace(origin_prefix, route)
        rewritten = rewritten.replace(protocol_relative, route)
    for origin, target_source in result.source.html_rewrite_routes:
        target_route = f"/v1/cache/{target_source}/".encode("ascii")
        origin_prefix = f"{origin}/".encode("utf-8")
        protocol_relative = f"//{urlsplit(origin).netloc}/".encode("utf-8")
        rewritten = rewritten.replace(origin_prefix, target_route)
        rewritten = rewritten.replace(protocol_relative, target_route)
    for relative_prefix, target_source in result.source.html_rewrite_relative_routes:
        target = (
            f"/v1/cache/{target_source}/{relative_prefix}".encode("ascii")
        )
        escaped = re.escape(relative_prefix.encode("utf-8"))
        rewritten = re.sub(
            rb"(?i)((?:href|src)\s*=\s*['\"])(?:\.\./)+" + escaped,
            lambda match: match.group(1) + target,
            rewritten,
        )
    return rewritten



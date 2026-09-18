from __future__ import annotations

from datetime import date

from .engine import Gateway
from .services.git import GitMirrorStore


def status_document(
    gateway: Gateway, git_mirror: GitMirrorStore | None = None
) -> dict[str, object]:
    """Build the stable, credential-free status view model."""

    sources = []
    for name, source in sorted(gateway.config.sources.items()):
        upstreams = (source.primary_upstream, *source.fallback_upstreams)
        if source.config_updated_at is None:
            config_update_status = "unknown"
        elif source.config_update_policy == "manual":
            config_update_status = "manual"
        elif (
            source.config_expires_at is not None
            and source.config_expires_at < date.today().isoformat()
        ):
            config_update_status = "expired"
        else:
            config_update_status = "current"
        sources.append(
            {
                "name": name,
                **(
                    {"display_name": gateway.source_display_name(source)}
                    if source.ecosystem == "apt"
                    else {}
                ),
                "route": f"/v1/cache/{name}/",
                "kind": source.kind,
                "ecosystem": source.ecosystem,
                "proxy_mode": source.proxy_mode,
                "fallback_count": len(source.fallback_upstreams),
                "metadata_ttl_seconds": source.metadata_ttl_seconds,
                "allow_query": source.allow_query,
                "config_updated_at": source.config_updated_at,
                "config_update_policy": source.config_update_policy,
                "config_expires_at": source.config_expires_at,
                "config_update_status": config_update_status,
                "upstream_chain": [
                    {
                        "position": position,
                        "label": upstream.label,
                        "proxy_mode": upstream.proxy_mode,
                        "attempt_timeout_seconds": upstream.attempt_timeout_seconds,
                        "slow_after_seconds": upstream.slow_after_seconds,
                        "min_bytes_per_second": upstream.min_bytes_per_second,
                    }
                    for position, upstream in enumerate(upstreams, start=1)
                ],
            }
        )
    return {
        "schema_version": 6,
        "status": "ok",
        "storage": type(gateway.storage).__name__,
        "sources": [source["name"] for source in sources],
        "source_details": sources,
        "requests": gateway.stats(),
        "recent_failures": gateway.recent_failures(),
        "git_mirror": (
            git_mirror.status()
            if git_mirror is not None
            else {"enabled": False}
        ),
    }

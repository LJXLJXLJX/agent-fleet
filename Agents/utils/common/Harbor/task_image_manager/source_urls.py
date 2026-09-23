"""Shared build-source URL validation."""

from __future__ import annotations

from urllib.parse import urlparse


def validate_source_url(
    value: str,
    label: str,
    build_network: str,
    *,
    sparse: bool = False,
) -> str:
    normalized = value.strip()
    parsed_value = normalized.removeprefix("sparse+") if sparse else normalized
    parsed = urlparse(parsed_value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            f"{label} must be an absolute query-free HTTP(S) URL without credentials"
        )
    host = parsed.hostname.lower()
    # This is an address-scope check, not a service availability probe. With
    # isolated BuildKit networking, loopback names the build container itself.
    if host in {"127.0.0.1", "localhost", "::1"} and build_network != "host":
        raise ValueError(
            f"loopback {label} is unreachable from BuildKit without "
            "--build-network=host; use a build-reachable source instead"
        )
    return normalized

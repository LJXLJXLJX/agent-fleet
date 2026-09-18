from __future__ import annotations

import hashlib
import re
from urllib.parse import urlsplit, urlunsplit

_SLUG = re.compile(r"[^a-z0-9]+")
_LEGACY_HASH = re.compile(r"-[0-9a-f]{8}$")
_GENERATED_PREFIXES = ("apt-objects-", "apt-repo-", "download-objects-")
_HEX_COMPONENT = re.compile(r"(?:[0-9a-f]{2})+")
_DOWNLOAD_ROUTE_VERSION = "v1"
_APT_ROUTE_VERSION = "v1"


def readable_source_name(
    prefix: str, identity: str, *, include_path: bool
) -> str:
    """Build a readable source identifier without embedding an opaque hash."""

    parsed = urlsplit(identity)
    authority = parsed.hostname or "source"
    try:
        port = parsed.port
    except ValueError:
        port = None
    default_port = 443 if parsed.scheme == "https" else 80
    if port is not None and port != default_port:
        authority = f"{authority}-port-{port}"
    if parsed.scheme and parsed.scheme != "https":
        authority = f"{parsed.scheme}-{authority}"
    raw_stem = f"{authority}-{parsed.path}" if include_path else authority
    stem = _SLUG.sub("-", raw_stem.lower()).strip("-") or "source"
    room = 63 - len(prefix) - 1
    return f"{prefix}-{stem[:room].rstrip('-')}"


def without_legacy_source_hash(name: str) -> str:
    if not name.startswith(_GENERATED_PREFIXES):
        return name
    return _LEGACY_HASH.sub("", name)


def canonical_download_origin(value: str) -> str:
    """Return the canonical, credential-free HTTP(S) origin for a download."""

    parsed = urlsplit(value)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("download origin must be credential-free HTTP(S)")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("download origin has an invalid port") from exc
    hostname = parsed.hostname
    if hostname is None or not hostname.isascii():
        raise ValueError("download origin hostname must be ASCII")
    hostname = hostname.lower()
    authority = f"[{hostname}]" if ":" in hostname else hostname
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    if port is not None and port != default_port:
        authority = f"{authority}:{port}"
    return urlunsplit((parsed.scheme.lower(), authority, "", "", ""))


def frozen_download_source_name(origin: str) -> str:
    """Build a readable origin source name with a collision-resistant identity."""

    canonical = canonical_download_origin(origin)
    digest = hashlib.sha256(canonical.encode("ascii")).hexdigest()[:16]
    parsed = urlsplit(canonical)
    readable = readable_source_name(
        "frozen-download", canonical, include_path=False
    )
    # Reserve the digest suffix before truncating the human-readable part.
    room = 63 - len(digest) - 1
    stem = readable[:room].rstrip("-") or f"frozen-{parsed.scheme}"
    return f"{stem}-{digest}"


def _hex_component(value: str) -> str:
    return value.encode("utf-8").hex()


def _decode_hex_component(value: str, label: str) -> str:
    if not _HEX_COMPONENT.fullmatch(value):
        raise ValueError(f"invalid {label} encoding")
    try:
        return bytes.fromhex(value).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"invalid {label} encoding") from exc


def download_gateway_path(url: str) -> str:
    """Encode a URL into the reversible, collision-free download v1 route."""

    parsed = urlsplit(url)
    origin = canonical_download_origin(url)
    origin_parts = urlsplit(origin)
    prefix = (
        f"/v1/cache/download/{_DOWNLOAD_ROUTE_VERSION}/"
        f"{origin_parts.scheme}/{_hex_component(origin_parts.netloc)}"
    )
    if parsed.path in {"", "/"}:
        route = f"{prefix}/root"
    else:
        if not parsed.path.startswith("/") or parsed.path.startswith("//"):
            raise ValueError("download path must have exactly one leading slash")
        route = f"{prefix}/object/{_hex_component(parsed.path)}"
    if parsed.query:
        route += f"?{parsed.query}"
    if parsed.fragment:
        route += f"#{parsed.fragment}"
    return route


def apt_gateway_path(source_url: str) -> str:
    """Encode an APT source base URL into an appendable dynamic route."""

    parsed = urlsplit(source_url)
    if parsed.query or parsed.fragment:
        raise ValueError("APT source URL must not contain query or fragment")
    origin = canonical_download_origin(source_url)
    origin_parts = urlsplit(origin)
    base_path = parsed.path.rstrip("/") or "/"
    if not base_path.startswith("/") or base_path.startswith("//"):
        raise ValueError("APT source path must have exactly one leading slash")
    return (
        f"/v1/cache/apt/{_APT_ROUTE_VERSION}/{origin_parts.scheme}/"
        f"{_hex_component(origin_parts.netloc)}/base/{_hex_component(base_path)}"
    )


def frozen_download_relative_path(path: str | None) -> str:
    """Encode an origin-relative path without reserving a real object name."""

    if path is None:
        return "root"
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError("download path must have exactly one leading slash")
    return f"object/{_hex_component(path)}"


def decode_frozen_download_relative_path(relative_path: str) -> str | None:
    """Decode a frozen-download source key; ``None`` denotes the origin root."""

    if relative_path == "root":
        return None
    selector, separator, encoded_path = relative_path.partition("/")
    if selector != "object" or not separator or not encoded_path or "/" in encoded_path:
        raise ValueError("invalid frozen download object key")
    path = _decode_hex_component(encoded_path, "frozen download path")
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError("invalid frozen download object path")
    return path


def parse_download_gateway_route(
    relative_path: str, query: str = ""
) -> tuple[str, str | None]:
    """Decode the part following ``/v1/cache/download/`` into origin/path."""

    parts = relative_path.split("/", 4)
    if len(parts) < 4 or parts[0] != _DOWNLOAD_ROUTE_VERSION:
        raise ValueError("invalid download route version")
    version, scheme, encoded_authority, selector = parts[:4]
    del version
    if scheme not in {"http", "https"} or not encoded_authority:
        raise ValueError("invalid download route origin")
    authority = _decode_hex_component(
        encoded_authority, "download route authority"
    )
    origin = canonical_download_origin(f"{scheme}://{authority}")
    if urlsplit(origin).netloc != authority:
        raise ValueError("download route authority is not canonical")
    if selector == "root" and len(parts) == 4:
        return origin, None
    if selector != "object" or len(parts) != 5 or not parts[4]:
        raise ValueError("invalid download route object selector")
    encoded_path = parts[4]
    path = _decode_hex_component(encoded_path, "download route path")
    if not path.startswith("/") or path.startswith("//"):
        raise ValueError("invalid download route object path")
    return origin, path


def parse_apt_gateway_route(
    relative_path: str,
) -> tuple[str, str, str | None]:
    """Decode an appendable APT route into origin, base path, and suffix."""

    parts = relative_path.split("/", 5)
    if len(parts) < 5 or parts[0] != _APT_ROUTE_VERSION:
        raise ValueError("invalid APT route version")
    version, scheme, encoded_authority, selector, encoded_base = parts[:5]
    del version
    if scheme not in {"http", "https"} or not encoded_authority:
        raise ValueError("invalid APT route origin")
    authority = _decode_hex_component(encoded_authority, "APT route authority")
    origin = canonical_download_origin(f"{scheme}://{authority}")
    if urlsplit(origin).netloc != authority:
        raise ValueError("APT route authority is not canonical")
    if selector != "base" or not encoded_base:
        raise ValueError("invalid APT route base selector")
    base_path = _decode_hex_component(encoded_base, "APT route base path")
    if not base_path.startswith("/") or base_path.startswith("//"):
        raise ValueError("invalid APT route base path")
    if base_path != "/" and base_path.endswith("/"):
        raise ValueError("APT route base path must be canonical")
    if len(parts) == 5:
        return origin, base_path, None
    suffix = parts[5]
    if not suffix or suffix.startswith("/"):
        raise ValueError("invalid APT route suffix")
    return origin, base_path, f"/{suffix}"

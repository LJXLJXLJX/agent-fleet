from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit, urlunsplit

from ._shared import _origin
from .errors import ConfigError

_UPSTREAM_LABEL = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_PROXY_MODES = {"configured", "direct"}

@dataclass(frozen=True)
class UpstreamConfig:
    base_url: str
    allowed_redirect_origins: frozenset[str]
    proxy_mode: str
    label: str = "primary"
    attempt_timeout_seconds: float | None = None
    slow_after_seconds: float | None = None
    min_bytes_per_second: float | None = None
    allowed_path_prefixes: tuple[str, ...] = ()
    allowed_filename_prefixes: tuple[str, ...] = ()
    allowed_exact_paths: frozenset[str] = frozenset()
    allow_query: bool = True

    def build_url(self, relative_path: str, query: str = "") -> str:
        return f"{self.base_url}{relative_path.lstrip('/')}" + (
            f"?{query}" if query else ""
        )

    def allows_url(self, url: str) -> bool:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or _origin(url) not in self.allowed_redirect_origins
        ):
            return False
        base = urlsplit(self.base_url)
        if parsed.query and not self.allow_query:
            return False
        if _origin(url) == _origin(self.base_url):
            if not parsed.path.startswith(base.path):
                return False
            relative = unquote(parsed.path[len(base.path) :]).lstrip("/")
            if (
                self.allowed_path_prefixes
                or self.allowed_filename_prefixes
                or self.allowed_exact_paths
            ) and not (
                relative in self.allowed_exact_paths
                or (relative == "" and "@root" in self.allowed_exact_paths)
                or any(
                    relative.startswith(prefix)
                    for prefix in self.allowed_path_prefixes
                )
                or (
                    "/" not in relative
                    and any(
                        relative.lower().startswith(prefix.lower())
                        for prefix in self.allowed_filename_prefixes
                    )
                )
            ):
                return False
        return True
def _optional_positive(
    value: dict[str, object], name: str, *, prefix: str
) -> float | None:
    raw = value.get(name)
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise ConfigError(f"{prefix} {name} must be a positive number")
    try:
        parsed = float(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{prefix} {name} must be a positive number") from exc
    if parsed <= 0:
        raise ConfigError(f"{prefix} {name} must be a positive number")
    return parsed
def _upstream_policy(
    value: dict[str, object], *, label_key: str, default_label: str, prefix: str
) -> tuple[str, float | None, float | None, float | None]:
    label = value.get(label_key, default_label)
    if not isinstance(label, str) or not _UPSTREAM_LABEL.fullmatch(label):
        raise ConfigError(f"invalid {prefix} label: {label!r}")
    attempt_timeout = _optional_positive(
        value, "attempt_timeout_seconds", prefix=prefix
    )
    slow_after = _optional_positive(value, "slow_after_seconds", prefix=prefix)
    min_speed = _optional_positive(value, "min_bytes_per_second", prefix=prefix)
    if (slow_after is None) != (min_speed is None):
        raise ConfigError(
            f"{prefix} slow_after_seconds and min_bytes_per_second must be configured together"
        )
    return label, attempt_timeout, slow_after, min_speed
def _upstream_from_dict(
    value: dict[str, object], *, default_proxy_mode: str, default_label: str
) -> UpstreamConfig:
    if not isinstance(value, dict):
        raise ConfigError("fallback_upstreams entries must be objects")
    try:
        raw_base_url = str(value["base_url"])
    except KeyError as exc:
        raise ConfigError("fallback upstream is missing base_url") from exc
    parsed = urlsplit(raw_base_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
    ):
        raise ConfigError(
            f"fallback base_url must be an absolute HTTP(S) URL without query/fragment: {raw_base_url!r}"
        )
    path = parsed.path or "/"
    if not path.endswith("/"):
        path += "/"
    base_url = urlunsplit(
        (parsed.scheme.lower(), parsed.netloc.lower(), path, "", "")
    )
    raw_mode = value.get("proxy_mode", default_proxy_mode)
    if not isinstance(raw_mode, str) or raw_mode not in _PROXY_MODES:
        raise ConfigError(f"invalid fallback proxy_mode: {raw_mode!r}")
    configured_origins = value.get("allowed_redirect_origins", [])
    if not isinstance(configured_origins, list):
        raise ConfigError("fallback allowed_redirect_origins must be an array")
    origins = {_origin(base_url)}
    for item in configured_origins:
        candidate = str(item).rstrip("/").lower()
        redirect = urlsplit(candidate)
        if (
            redirect.scheme not in {"http", "https"}
            or not redirect.netloc
            or redirect.path
            or redirect.query
            or redirect.fragment
        ):
            raise ConfigError(f"invalid fallback redirect origin: {item!r}")
        origins.add(candidate)
    label, attempt_timeout, slow_after, min_speed = _upstream_policy(
        value,
        label_key="label",
        default_label=default_label,
        prefix="fallback upstream",
    )
    return UpstreamConfig(
        base_url=base_url,
        allowed_redirect_origins=frozenset(origins),
        proxy_mode=raw_mode,
        label=label,
        attempt_timeout_seconds=attempt_timeout,
        slow_after_seconds=slow_after,
        min_bytes_per_second=min_speed,
    )

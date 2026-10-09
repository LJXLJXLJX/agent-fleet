"""YiCloud config: extracted from the legacy Harbor provider without behavior changes."""

from __future__ import annotations

import os
from typing import Any


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"required environment variable is unset: {name}")
    return value


def _required_registry_host() -> str:
    host = _required_env("YICLOUD_HARBOR_HOST")
    if (
        "://" in host
        or "/" in host
        or "@" in host
        or any(character.isspace() for character in host)
    ):
        raise ValueError(
            f"YICLOUD_HARBOR_HOST must be a bare OCI registry host, got: {host!r}"
        )
    return host


def _validate_managed_image_ref(ref: str, source: str) -> None:
    host = _required_registry_host()
    registry, separator, repository = ref.partition("/")
    if registry != host or not separator or not repository:
        raise RuntimeError(
            f"{source} must use the configured OpenSandbox registry "
            f"{host!r}, got: {ref!r}"
        )


def _positive_int(value: Any, name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive integer, got {value!r}") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer, got {value!r}")
    return parsed


def _bool_setting(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError(
        f"{name} must be one of 1/0, true/false, yes/no, or on/off; got {value!r}"
    )

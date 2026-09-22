"""Parse explicit build arguments and configure build proxy arguments."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from urllib.parse import urlparse

BUILD_ARG_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def parse_build_args(raw: str) -> dict[str, str]:
    if not raw.strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise TypeError("build args must be a JSON object")
    result: dict[str, str] = {}
    for key, value in data.items():
        if not isinstance(key, str) or not BUILD_ARG_NAME.fullmatch(key):
            raise ValueError(f"invalid build arg name: {key!r}")
        if not isinstance(value, (str, int, float, bool)):
            raise TypeError(f"invalid build arg value for {key!r}")
        result[key] = str(value).lower() if isinstance(value, bool) else str(value)
    return result


def proxy_build_args(
    enabled: bool,
    build_network: str = "default",
    direct_hosts: Iterable[str] = (),
) -> dict[str, str]:
    if not enabled:
        return {}
    configured_proxy = os.environ.get("HARBOR_TASK_IMAGE_BUILD_PROXY_URL", "").strip()
    proxy_names = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
    if configured_proxy:
        parsed_proxy = urlparse(configured_proxy)
        if parsed_proxy.scheme not in {"http", "https"} or not parsed_proxy.hostname:
            raise ValueError(
                "HARBOR_TASK_IMAGE_BUILD_PROXY_URL must be an http(s) proxy URL"
            )
        result = {
            "HTTP_PROXY": configured_proxy,
            "HTTPS_PROXY": configured_proxy,
            "http_proxy": configured_proxy,
            "https_proxy": configured_proxy,
        }
    else:
        result = {
            name: os.environ[name] for name in proxy_names if os.environ.get(name)
        }
    if not result:
        raise ValueError(
            "--use-proxy requires HARBOR_TASK_IMAGE_BUILD_PROXY_URL or an "
            "HTTP_PROXY/HTTPS_PROXY environment variable"
        )
    for name, value in result.items():
        if (
            urlparse(value).hostname in {"127.0.0.1", "localhost", "::1"}
            and build_network != "host"
        ):
            raise ValueError(
                f"--use-proxy cannot pass loopback proxy {name} into BuildKit "
                "without --build-network=host"
            )
    if "HTTP_PROXY" in result:
        result.setdefault("http_proxy", result["HTTP_PROXY"])
    if "HTTPS_PROXY" in result:
        result.setdefault("https_proxy", result["HTTPS_PROXY"])
    if "http_proxy" in result:
        result.setdefault("HTTP_PROXY", result["http_proxy"])
    if "https_proxy" in result:
        result.setdefault("HTTPS_PROXY", result["https_proxy"])
    for name in ("NO_PROXY", "no_proxy"):
        if os.environ.get(name):
            result[name] = os.environ[name]
    if direct_hosts:
        existing = result.get("NO_PROXY", result.get("no_proxy", ""))
        entries = [entry.strip() for entry in existing.split(",") if entry.strip()]
        for direct_host in sorted(set(direct_hosts)):
            if direct_host not in entries:
                entries.append(direct_host)
        result["NO_PROXY"] = ",".join(entries)
        result["no_proxy"] = result["NO_PROXY"]
    return result

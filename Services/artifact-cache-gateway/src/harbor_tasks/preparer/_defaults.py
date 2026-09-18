"""prepare CLI default path constants."""

from __future__ import annotations

from pathlib import Path

_PROJECT_ROOT = Path.cwd()


_DEFAULT_ENV_FILE = _PROJECT_ROOT / "config.local.env"


_DEFAULT_GATEWAY_CONFIG = _PROJECT_ROOT / "config" / "sources.json"


_DEFAULT_SOURCE_PREFIX_MAP_JSON = (
    '{"docker.io":['
    '"docker.m.daocloud.io","docker.1ms.run",'
    '"docker.1panel.live","docker.xuanyuan.me"],'
    '"mcr.microsoft.com":"m.daocloud.io/mcr.microsoft.com"}'
)


_DEFAULT_GIT_MIRROR_ROOT = Path(
    "/data/artifact-cache/github_mirrors"
)

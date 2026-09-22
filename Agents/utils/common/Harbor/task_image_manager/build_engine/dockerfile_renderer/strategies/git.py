"""Build-only Git mirror configuration rendering."""

from __future__ import annotations

from ....source_urls import validate_source_url

GITHUB_GIT_URL_PREFIXES = (
    "https://github.com/",
    "http://github.com/",
    "git@github.com:",
    "ssh://git@github.com/",
    "git://github.com/",
)
GITHUB_MIRROR_CONFIG_MOUNT_ID = "opensandbox-github-mirror-gitconfig"


def validate_github_mirror_url(value: str, build_network: str) -> str:
    """Validate a GitHub Smart HTTP mirror prefix used only during builds."""
    if not value.strip():
        return ""
    return (
        validate_source_url(value.strip(), "GitHub mirror", build_network).rstrip("/")
        + "/"
    )


def github_mirror_config_content(github_mirror_url: str) -> str:
    """Render transient system Git config mounted only during Dockerfile RUN."""
    if not github_mirror_url:
        return ""
    lines = [f'[url "{github_mirror_url}"]']
    lines.extend(f"\tinsteadOf = {prefix}" for prefix in GITHUB_GIT_URL_PREFIXES)
    return "\n".join(lines) + "\n"

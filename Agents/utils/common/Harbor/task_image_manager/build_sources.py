"""Select build sources once, after local cache resolution and before preparation."""

from __future__ import annotations

import argparse
import copy
import subprocess

from .build_engine.dockerfile_renderer.strategies.packages import (
    DEFAULT_CARGO_REGISTRY_URL,
    DEFAULT_GOPROXY,
    DEFAULT_GOSUMDB,
    DEFAULT_NPM_REGISTRY,
    DEFAULT_PIP_INDEX_URL,
    DEFAULT_RUSTUP_DIST_SERVER,
    DEFAULT_RUSTUP_UPDATE_ROOT,
)
from .dependency_gateway import GatewaySources
from .registry import log

DOMESTIC_SOURCES = {
    "pip_index_url": DEFAULT_PIP_INDEX_URL,
    "npm_registry": DEFAULT_NPM_REGISTRY,
    "goproxy": DEFAULT_GOPROXY,
    "gosumdb": DEFAULT_GOSUMDB,
    "cargo_registry_url": DEFAULT_CARGO_REGISTRY_URL,
    "rustup_dist_server": DEFAULT_RUSTUP_DIST_SERVER,
    "rustup_update_root": DEFAULT_RUSTUP_UPDATE_ROOT,
    "pub_hosted_url": "",
    "julia_pkg_server": "",
    "rustup_init_url": "",
    "pytorch_index_url": "",
}


def _sources_healthy(args) -> bool:
    if not args.health_url or args.dry_run:
        return True
    if not 0 < args.probe_timeout_sec <= 60:
        raise ValueError(
            "package source probe timeout must be greater than 0 and at most 60 seconds"
        )
    try:
        return (
            subprocess.run(
                [
                    "curl",
                    "--noproxy",
                    "*",
                    "--fail",
                    "--silent",
                    "--show-error",
                    "--max-time",
                    str(args.probe_timeout_sec),
                    args.health_url,
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=args.probe_timeout_sec + 1,
                check=False,
            ).returncode
            == 0
        )
    except (OSError, subprocess.TimeoutExpired):
        return False


def select_build_sources(args: argparse.Namespace) -> argparse.Namespace:
    """Choose Gateway or legacy mirror routes without retrying task operations.

    Gateway configuration takes precedence, including when its probe fails.
    Source selection never mutates the caller's settings.
    """
    if args.dry_run:
        return args
    if getattr(args, "dependency_gateway_url", "").strip():
        return GatewaySources(args).select(args)
    if not getattr(args, "health_url", "") or _sources_healthy(args):
        return args
    selected = copy.deepcopy(args)
    for name, value in DOMESTIC_SOURCES.items():
        setattr(selected, name, value)
    log("package source health check failed; using domestic package sources")
    return selected

"""prepare CLI argument parsing (parser construction and shared argument groups)."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from ._defaults import (
    _DEFAULT_ENV_FILE,
    _DEFAULT_GATEWAY_CONFIG,
    _DEFAULT_GIT_MIRROR_ROOT,
)


def add_analysis_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "dataset", type=Path, help="Harbor benchmark framework dataset root"
    )
    command.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="local analysis, plan, selection, and result directory",
    )
    command.add_argument(
        "--registry", help="target OCI Registry host; defaults from local env"
    )
    command.add_argument("--project", help="target Registry Project")
    command.add_argument("--platform", help="target os/architecture")
    command.add_argument(
        "--source-prefix-map-json",
        help="JSON source-prefix replacements; defaults from local env",
    )
    command.add_argument(
        "--shell-scope",
        choices=("referenced", "all", "none"),
        default="referenced",
    )
    command.add_argument(
        "--env-file",
        type=Path,
        default=_DEFAULT_ENV_FILE,
        help="project-local environment file",
    )
    command.add_argument(
        "--probe-domestic-packages",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="probe every discovered package against its domestic source (default: enabled)",
    )
    command.add_argument("--package-probe-timeout", type=float, default=15.0)
    command.add_argument("--package-probe-slow-seconds", type=float, default=3.0)
    command.add_argument("--package-probe-sample-bytes", type=int, default=128 * 1024)
    command.add_argument("--package-probe-concurrency", type=int, default=16)


def add_mirror_arguments(
    command: argparse.ArgumentParser, *, include_env_file: bool = True
) -> None:
    command.add_argument(
        "--concurrency",
        type=int,
        help="parallel image copies (1-32; defaults from local env, then 4)",
    )
    command.add_argument(
        "--authfile",
        type=Path,
        help="existing skopeo authfile; otherwise local-env credentials are used",
    )
    command.add_argument(
        "--upstream-proxy",
        help="explicit HTTP(S) proxy; ambient proxy is ignored",
    )
    command.add_argument(
        "--direct-upstream-with-proxy",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "after ordered mirrors fail, retry the original registry through "
            "--upstream-proxy"
        ),
    )
    command.add_argument(
        "--target-tls-verify",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="verify the target Registry TLS certificate",
    )
    if include_env_file:
        command.add_argument(
            "--env-file",
            type=Path,
            default=_DEFAULT_ENV_FILE,
            help="project-local environment file",
        )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description=(
            "Analyze a Harbor benchmark framework dataset and execute reviewed "
            "public base-image mirror plans."
        )
    )
    commands = result.add_subparsers(dest="command", required=True)

    analyze_parser = commands.add_parser(
        "analyze", help="analyze a dataset and write a local image mirror plan"
    )
    add_analysis_arguments(analyze_parser)

    mirror_parser = commands.add_parser(
        "mirror-images", help="execute one previously generated image mirror plan"
    )
    mirror_parser.add_argument("plan", type=Path, help="reviewed image-mirror-plan.json")
    mirror_parser.add_argument(
        "--execute",
        action="store_true",
        required=True,
        help="required acknowledgement that this command writes the target Registry",
    )
    mirror_parser.add_argument(
        "--output",
        type=Path,
        help="result JSON path (default: image-mirror-result.json beside the plan)",
    )
    add_mirror_arguments(mirror_parser)

    apt_parser = commands.add_parser(
        "configure-apt",
        help="compile reviewed APT candidates into a safe Gateway source config",
    )
    apt_parser.add_argument(
        "report", type=Path, help="apt-cache-candidates.json produced by analysis"
    )

    downloads_parser = commands.add_parser(
        "configure-downloads",
        help="compile reviewed curl/wget URLs into exact Gateway sources",
    )
    downloads_parser.add_argument(
        "report", type=Path, help="external-build-inputs.json produced by analysis"
    )
    downloads_parser.add_argument(
        "--base-config", type=Path, default=_DEFAULT_GATEWAY_CONFIG
    )
    downloads_parser.add_argument(
        "--output", type=Path, required=True, help="merged Gateway config JSON"
    )
    downloads_parser.add_argument(
        "--plan-output", type=Path, help="download plan JSON path"
    )

    warm_download_parser = commands.add_parser(
        "warm-downloads",
        help="warm every reviewed curl/wget object through fixed Gateway routes",
    )
    warm_download_parser.add_argument("plan", type=Path)
    warm_download_parser.add_argument(
        "--gateway-url",
        default=os.environ.get(
            "ARTIFACT_CACHE_GATEWAY_URL", "http://127.0.0.1:8080/v1/cache"
        ),
    )
    warm_download_parser.add_argument("--timeout", type=float, default=600.0)
    warm_download_parser.add_argument("--concurrency", type=int, default=4)
    warm_download_parser.add_argument("--output", type=Path)
    warm_download_parser.add_argument("--execute", action="store_true", required=True)

    refresh_download_parser = commands.add_parser(
        "refresh-downloads",
        help="explicitly refresh manual curl/wget objects using Gateway storage",
    )
    refresh_download_parser.add_argument("plan", type=Path)
    refresh_download_parser.add_argument(
        "--config", type=Path, default=_DEFAULT_GATEWAY_CONFIG
    )
    refresh_download_parser.add_argument(
        "--env-file", type=Path, default=_DEFAULT_ENV_FILE
    )
    refresh_download_parser.add_argument(
        "--storage",
        choices=("s3", "file"),
        default=os.environ.get("ARTIFACT_CACHE_STORAGE", "s3"),
    )
    refresh_download_parser.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(os.environ.get("ARTIFACT_CACHE_DIR", "/data/artifact-cache")),
    )
    refresh_download_parser.add_argument(
        "--upstream-proxy", default=os.environ.get("ARTIFACT_CACHE_UPSTREAM_PROXY")
    )
    refresh_download_parser.add_argument("--fetch-timeout", type=float, default=120.0)
    refresh_download_parser.add_argument("--max-object-gib", type=float, default=20.0)
    refresh_download_parser.add_argument("--concurrency", type=int, default=4)
    refresh_download_parser.add_argument("--include-immutable", action="store_true")
    refresh_download_parser.add_argument("--output", type=Path)
    refresh_download_parser.add_argument("--execute", action="store_true", required=True)
    warm_git_parser = commands.add_parser(
        "warm-git",
        help="prepare planned GitHub repositories in the persistent Git mirror",
    )
    warm_git_parser.add_argument("plan", type=Path)
    warm_git_parser.add_argument(
        "--root",
        type=Path,
        default=Path(
            os.environ.get(
                "ARTIFACT_CACHE_GIT_MIRROR_ROOT",
                str(_DEFAULT_GIT_MIRROR_ROOT),
            )
        ),
        help="persistent GPFS root for bare Git mirrors",
    )
    warm_git_parser.add_argument(
        "--upstream-proxy", default=os.environ.get("ARTIFACT_CACHE_UPSTREAM_PROXY")
    )
    warm_git_parser.add_argument("--timeout", type=float, default=1800.0)
    warm_git_parser.add_argument("--concurrency", type=int, default=4)
    warm_git_parser.add_argument(
        "--repository",
        action="append",
        help="warm only this owner/repository (repeatable; default: planned repositories)",
    )
    warm_git_parser.add_argument("--refresh", action="store_true")
    warm_git_parser.add_argument("--output", type=Path)
    warm_git_parser.add_argument("--execute", action="store_true", required=True)
    apt_parser.add_argument(
        "--base-config",
        type=Path,
        default=_DEFAULT_GATEWAY_CONFIG,
        help="existing Gateway source config to merge",
    )
    apt_parser.add_argument(
        "--output", type=Path, required=True, help="merged Gateway config JSON"
    )

    prepare_parser = commands.add_parser(
        "prepare",
        help="one-click analyze, filter existing target tags, and upload missing images",
    )
    add_analysis_arguments(prepare_parser)
    add_mirror_arguments(prepare_parser, include_env_file=False)
    prepare_parser.add_argument(
        "--execute",
        action="store_true",
        required=True,
        help="required acknowledgement that this command writes the target Registry",
    )
    prepare_parser.add_argument(
        "--gateway-url",
        default=os.environ.get(
            "ARTIFACT_CACHE_GATEWAY_URL", "http://127.0.0.1:8080/v1/cache"
        ),
        help="reachable Artifact Cache Gateway /v1/cache root",
    )
    prepare_parser.add_argument(
        "--warm-problem-packages",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="warm slow/unavailable packages through fixed Gateway sources",
    )
    prepare_parser.add_argument("--package-warm-timeout", type=float, default=600.0)

    warm_parser = commands.add_parser(
        "warm-packages",
        help="warm a reviewed package-probe-report.json without mirroring OCI images",
    )
    warm_parser.add_argument("report", type=Path)
    warm_parser.add_argument(
        "--gateway-url",
        default=os.environ.get(
            "ARTIFACT_CACHE_GATEWAY_URL", "http://127.0.0.1:8080/v1/cache"
        ),
    )
    warm_parser.add_argument("--timeout", type=float, default=600.0)
    warm_parser.add_argument("--output", type=Path)
    warm_parser.add_argument("--execute", action="store_true", required=True)
    return result

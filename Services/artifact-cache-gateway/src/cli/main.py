from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

from ..core.config import config_from_document, load_config
from ..harbor_tasks.preparer.direct_download import merge_download_plan_directory
from ..gateway.fetcher import Fetcher
from ..gateway.engine import Gateway
from ..gateway.services.git import GitMirrorStore, load_git_mirror_plan
from ..core.logging_utils import emit_event
from ..storage.s3 import S3Settings, S3Storage
from ..gateway.server import GatewayHTTPServer
from ..storage.base import StorageError
from ..storage.gpfs import FileStorage


_LOGGER = logging.getLogger("artifact_cache_gateway")
_DEFAULT_GIT_MIRROR_ROOT = Path(
    "/data/artifact-cache/github_mirrors"
)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="HTTP artifact cache gateway")
    result.add_argument("--host", default="127.0.0.1")
    result.add_argument("--port", default=8080, type=int)
    result.add_argument(
        "--storage",
        choices=("s3", "file"),
        default=os.environ.get("ARTIFACT_CACHE_STORAGE", "s3"),
        help="production default is s3; file is explicit development mode",
    )
    result.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(os.environ.get("ARTIFACT_CACHE_DIR", "/data/artifact-cache")),
        help="S3 mode: temporary staging; file mode: development storage root",
    )
    result.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ["ARTIFACT_CACHE_CONFIG"])
        if os.environ.get("ARTIFACT_CACHE_CONFIG")
        else None,
        help="JSON source config; omitted means built-in PyTorch source",
    )
    result.add_argument(
        "--upstream-proxy",
        default=os.environ.get("ARTIFACT_CACHE_UPSTREAM_PROXY"),
        help="proxy used only by the fetcher",
    )
    result.add_argument(
        "--download-plan-dir",
        type=Path,
        default=(
            Path(os.environ["ARTIFACT_CACHE_DOWNLOAD_PLAN_DIR"])
            if os.environ.get("ARTIFACT_CACHE_DOWNLOAD_PLAN_DIR")
            else None
        ),
        help="directory of reviewed download-gateway-plan.json files",
    )
    result.add_argument("--fetch-timeout", type=float, default=120.0)
    result.add_argument("--index-ttl", type=float, default=300.0)
    result.add_argument("--max-object-gib", type=float, default=20.0)
    result.add_argument(
        "--cache-mode",
        choices=("proxy-only", "all"),
        default=os.environ.get("ARTIFACT_CACHE_CACHE_MODE", "proxy-only"),
        help="proxy-only caches only downloads that actually used the configured proxy",
    )
    result.add_argument(
        "--git-mirror-plan",
        type=Path,
        default=(
            Path(os.environ["ARTIFACT_CACHE_GIT_MIRROR_PLAN"])
            if os.environ.get("ARTIFACT_CACHE_GIT_MIRROR_PLAN")
            else None
        ),
        help="optional GitHub prewarm/readiness plan; omitted disables Git Smart HTTP",
    )
    result.add_argument(
        "--git-mirror-root",
        type=Path,
        default=Path(
            os.environ.get(
                "ARTIFACT_CACHE_GIT_MIRROR_ROOT",
                str(_DEFAULT_GIT_MIRROR_ROOT),
            )
        ),
        help="persistent GPFS root for bare Git mirrors",
    )
    result.add_argument("--git-mirror-timeout", type=float, default=1800.0)
    result.add_argument("--git-mirror-concurrency", type=int, default=4)
    return result


def main() -> None:
    args = parser().parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    download_plans: tuple[Path, ...] = ()
    if args.download_plan_dir is not None:
        if args.config is None:
            parser().error("--download-plan-dir requires --config")
        with args.config.open("r", encoding="utf-8") as stream:
            base_document = json.load(stream)
        merged_document, download_plans = merge_download_plan_directory(
            base_document, args.download_plan_dir.expanduser().resolve()
        )
        config = config_from_document(merged_document)
    else:
        config = load_config(args.config)
    try:
        if args.storage == "s3":
            storage = S3Storage(S3Settings.from_env(), work_dir=args.cache_dir)
        else:
            storage = FileStorage(args.cache_dir)
            emit_event(
                _LOGGER,
                "development_storage_enabled",
                level=logging.WARNING,
                storage="file",
                path=str(args.cache_dir),
                production_safe=False,
            )
    except StorageError as exc:
        emit_event(
            _LOGGER,
            "storage_startup_failed",
            level=logging.ERROR,
            storage=args.storage,
            reason=str(exc),
        )
        raise SystemExit(2) from None

    fetcher = Fetcher(
        storage=storage,
        proxy_url=args.upstream_proxy,
        timeout=args.fetch_timeout,
        max_object_bytes=int(args.max_object_gib * 1024**3),
    )
    gateway = Gateway(
        config=config,
        storage=storage,
        fetcher=fetcher,
        index_ttl_seconds=args.index_ttl,
        persist_request_stats=True,
        cache_mode=args.cache_mode,
    )
    git_mirror = None
    if args.git_mirror_plan is not None:
        git_mirror = GitMirrorStore(
            plan=load_git_mirror_plan(args.git_mirror_plan),
            root=args.git_mirror_root.expanduser().resolve(),
            proxy_url=args.upstream_proxy,
            timeout=args.git_mirror_timeout,
            max_concurrent_fills=args.git_mirror_concurrency,
            metrics_callback=gateway.record_git_mirror_state,
        )
    server = GatewayHTTPServer((args.host, args.port), gateway, git_mirror)
    emit_event(
        _LOGGER,
        "gateway_started",
        host=args.host,
        port=args.port,
        sources=sorted(config.sources),
        storage=args.storage,
        work_dir=str(args.cache_dir),
        upstream_proxy="configured" if args.upstream_proxy else "disabled",
        source_proxy_modes={
            name: source.proxy_mode for name, source in sorted(config.sources.items())
        },
        cache_mode=args.cache_mode,
        download_plans=[path.name for path in download_plans],
        git_mirror=(
            {
                "enabled": True,
                "known_repositories": git_mirror.configured_count,
                "planned_repositories": git_mirror.status()["planned_repositories"],
                "storage": "persistent-filesystem",
                "root": str(git_mirror.root),
            }
            if git_mirror is not None
            else {"enabled": False}
        ),
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        gateway.close()
        server.server_close()

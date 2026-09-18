"""prepare CLI maintenance subcommands: the warm/configure family."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from ...core.config import load_config
from ...core.local_env import LocalEnvError, load_local_env
from ...gateway.engine import Gateway
from ...gateway.fetcher import Fetcher
from ...gateway.services.apt import (
    AptGatewayPlanError,
    compile_apt_gateway_plan,
    merge_gateway_config,
    read_json_object,
    write_json,
)
from ...gateway.services.git import (
    GitMirrorError,
    GitMirrorStore,
    load_git_mirror_plan,
    warm_git_mirrors,
)
from ...storage.base import StorageError
from ...storage.gpfs import FileStorage
from ...storage.s3 import S3Settings, S3Storage
from .direct_download import (
    DownloadGatewayPlanError,
    compile_download_gateway_plan,
    refresh_downloads,
    warm_downloads,
)
from .orchestrator import warm_packages
from .report import write_warm_result


def warm_packages_command(args: argparse.Namespace) -> int:
    report_path = args.report.expanduser().resolve()
    output_path = (
        args.output.expanduser().resolve()
        if args.output
        else report_path.with_name("package-warm-result.json")
    )
    try:
        report = read_json_object(report_path)
        with tempfile.TemporaryDirectory(prefix="artifact-cache-warm-") as temporary:
            result = warm_packages(
                report,
                gateway_url=args.gateway_url,
                timeout_seconds=args.timeout,
                work_dir=Path(temporary),
            )
        write_warm_result(output_path, result)
        print(
            json.dumps(
                {
                    "package_warm_result": str(output_path),
                    **result["summary"],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1 if result["summary"]["failures"] else 0
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def configure_apt_command(args: argparse.Namespace) -> int:
    report_path = args.report.expanduser().resolve()
    base_path = args.base_config.expanduser().resolve()
    output_path = args.output.expanduser().resolve()
    try:
        report = read_json_object(report_path)
        plan = compile_apt_gateway_plan(report)
        merged = merge_gateway_config(read_json_object(base_path), plan)
        write_json(output_path, merged)
        print(
            json.dumps(
                {
                    "apt_candidate_report": str(report_path),
                    "gateway_config": str(output_path),
                    "added_sources": len(plan["sources"]),
                    "rewrite_rules": len(plan["rewrites"]),
                    "rejected_candidates": len(plan["rejected"]),
                    "packages_needing_resolution": len(
                        plan["packages_needing_resolution"]
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    except (AptGatewayPlanError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def configure_downloads_command(args: argparse.Namespace) -> int:
    report_path = args.report.expanduser().resolve()
    base_path = args.base_config.expanduser().resolve()
    output_path = args.output.expanduser().resolve()
    plan_path = (
        args.plan_output.expanduser().resolve()
        if args.plan_output
        else output_path.with_name("download-gateway-plan.json")
    )
    try:
        plan = compile_download_gateway_plan(read_json_object(report_path))
        merged = merge_gateway_config(read_json_object(base_path), plan)
        configured_sources = {
            str(row.get("name"))
            for row in merged.get("sources", [])
            if isinstance(row, dict)
        }
        missing = sorted(
            {
                str(row.get("source"))
                for row in plan["rewrites"]
                if isinstance(row, dict)
            }
            - configured_sources
        )
        if missing:
            raise DownloadGatewayPlanError(
                f"download plan references missing existing sources: {', '.join(missing)}"
            )
        write_json(plan_path, plan)
        write_json(output_path, merged)
        print(
            json.dumps(
                {
                    "download_plan": str(plan_path),
                    "gateway_config": str(output_path),
                    **plan["summary"],
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    except (
        AptGatewayPlanError,
        DownloadGatewayPlanError,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def warm_downloads_command(args: argparse.Namespace) -> int:
    plan_path = args.plan.expanduser().resolve()
    output_path = (
        args.output.expanduser().resolve()
        if args.output
        else plan_path.with_name("download-warm-result.json")
    )
    try:
        result = warm_downloads(
            read_json_object(plan_path),
            gateway_url=args.gateway_url,
            timeout_seconds=args.timeout,
            concurrency=args.concurrency,
        )
        write_json(output_path, result)
        print(
            json.dumps(
                {"download_warm_result": str(output_path), **result["summary"]},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1 if result["summary"]["failures"] else 0
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def refresh_downloads_command(args: argparse.Namespace) -> int:
    plan_path = args.plan.expanduser().resolve()
    output_path = (
        args.output.expanduser().resolve()
        if args.output
        else plan_path.with_name("download-refresh-result.json")
    )
    gateway = None
    try:
        env_path = args.env_file.expanduser().resolve()
        if env_path.is_file():
            load_local_env(env_path)
        config = load_config(args.config.expanduser().resolve())
        cache_dir = args.cache_dir.expanduser().resolve()
        storage = (
            S3Storage(S3Settings.from_env(), work_dir=cache_dir)
            if args.storage == "s3"
            else FileStorage(cache_dir)
        )
        fetcher = Fetcher(
            storage=storage,
            proxy_url=(
                args.upstream_proxy
                or os.environ.get("ARTIFACT_CACHE_UPSTREAM_PROXY")
            ),
            timeout=args.fetch_timeout,
            max_object_bytes=int(args.max_object_gib * 1024**3),
        )
        gateway = Gateway(
            config=config,
            storage=storage,
            fetcher=fetcher,
            index_ttl_seconds=300,
            stale_if_error=False,
            persist_request_stats=False,
            cache_mode="all",
        )
        result = refresh_downloads(
            read_json_object(plan_path),
            gateway=gateway,
            concurrency=args.concurrency,
            include_immutable=args.include_immutable,
        )
        write_json(output_path, result)
        print(
            json.dumps(
                {"download_refresh_result": str(output_path), **result["summary"]},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1 if result["summary"]["failures"] else 0
    except (
        LocalEnvError,
        OSError,
        RuntimeError,
        StorageError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    finally:
        if gateway is not None:
            gateway.close()


def warm_git_command(args: argparse.Namespace) -> int:
    plan_path = args.plan.expanduser().resolve()
    output_path = (
        args.output.expanduser().resolve()
        if args.output
        else plan_path.with_name("git-mirror-result.json")
    )
    try:
        store = GitMirrorStore(
            plan=load_git_mirror_plan(plan_path),
            root=args.root.expanduser().resolve(),
            proxy_url=args.upstream_proxy,
            timeout=args.timeout,
            max_concurrent_fills=args.concurrency,
        )
        result = warm_git_mirrors(
            store,
            concurrency=args.concurrency,
            refresh=args.refresh,
            repositories=args.repository,
        )
        write_json(output_path, result)
        print(
            json.dumps(
                {"git_mirror_result": str(output_path), **result["summary"]},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1 if result["summary"]["error"] else 0
    except (GitMirrorError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

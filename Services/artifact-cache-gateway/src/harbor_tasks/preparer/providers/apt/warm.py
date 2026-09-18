"""APT warm: warm the Gateway cache from resolution results."""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path
from typing import Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from ...models import (
    AptEnvironment,
    WarmResult,
)
from .environments import environment_from_identity
from .repository import _RepositoryMixin


class _WarmMixin(_RepositoryMixin):
    @staticmethod
    def _fetch_to_cache(
        gateway_url: str,
        source: str,
        filename: str,
        timeout_seconds: float,
        expected_sha256: str | None,
    ) -> WarmResult:
        url = (
            f"{gateway_url.rstrip('/')}/{source}/"
            f"{quote(filename.lstrip('/'), safe='/+~._-')}"
        )
        opener = build_opener(ProxyHandler({}))
        request = Request(
            url,
            headers={"X-Artifact-Cache-Prefer-Fallback": "1"},
            method="GET",
        )
        digest = hashlib.sha256()
        size = 0
        try:
            with opener.open(request, timeout=timeout_seconds) as response:
                first_state = response.headers.get("X-Artifact-Cache", "")
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    size += len(chunk)
            verify = Request(url, method="HEAD")
            with opener.open(verify, timeout=timeout_seconds) as response:
                verification = response.headers.get("X-Artifact-Cache", "")
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            return WarmResult(
                "apt",
                "",
                None,
                "failed",
                f"Gateway cache warm failed: {type(exc).__name__}",
                gateway_url=url,
            )
        if expected_sha256 and digest.hexdigest() != expected_sha256:
            return WarmResult(
                "apt",
                "",
                None,
                "failed",
                "Gateway object digest differs from APT Packages metadata",
                gateway_url=url,
                first_cache_state=first_state,
                verification_cache_state=verification,
                size=size,
            )
        if verification != "HIT":
            return WarmResult(
                "apt",
                "",
                None,
                "failed",
                f"Gateway verification expected HIT, received {verification or 'missing'}",
                gateway_url=url,
                first_cache_state=first_state,
                verification_cache_state=verification,
                size=size,
            )
        return WarmResult(
            "apt",
            "",
            None,
            "already-cached" if first_state == "HIT" else "cached",
            "package blob is present in Gateway storage",
            gateway_url=url,
            first_cache_state=first_state,
            verification_cache_state=verification,
            size=size,
        )

    def warm(
        self,
        rows: Sequence[dict[str, object]],
        *,
        gateway_url: str,
        timeout_seconds: float,
        work_dir: Path,
    ) -> list[WarmResult]:
        parsed = urlsplit(gateway_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("gateway URL must be an absolute HTTP(S) URL")
        actionable = [
            row for row in rows if row.get("status") in {"slow", "unavailable"}
        ]
        results: list[WarmResult] = []
        grouped_missing: dict[
            tuple[str, str], tuple[AptEnvironment, set[str], dict[str, dict[str, object]]]
        ] = {}
        records: list[
            tuple[str, str, dict[str, object], dict[str, object]]
        ] = []
        repository_jobs: list[
            tuple[
                str,
                str,
                AptEnvironment,
                tuple[dict[str, object], ...],
                dict[str, object],
            ]
        ] = []
        for row in actionable:
            package = str(row.get("package", ""))
            identity = str(row.get("environment") or "")
            analysis_environment = str(row.get("environment_id") or "")
            environment = environment_from_identity(identity)
            if environment is None:
                results.append(
                    WarmResult(
                        "apt",
                        package,
                        identity or None,
                        "not-actionable",
                        "unsupported APT environment",
                        environment_id=analysis_environment or None,
                        build_contexts=tuple(
                            str(item)
                            for item in row.get("build_contexts", [])
                            if isinstance(row.get("build_contexts", []), (list, tuple))
                        ),
                    )
                )
                continue
            if row.get("filename"):
                records.append((identity, package, dict(row), row))
            else:
                raw_contexts = row.get("repository_contexts", [])
                contexts = (
                    tuple(item for item in raw_contexts if isinstance(item, dict))
                    if isinstance(raw_contexts, (list, tuple))
                    else ()
                )
                if contexts:
                    repository_jobs.append(
                        (identity, package, environment, contexts, row)
                    )
                else:
                    current = grouped_missing.setdefault(
                        (identity, analysis_environment), (environment, set(), {})
                    )
                    current[1].add(package)
                    current[2][package] = row
        for job_number, (
            identity,
            package,
            environment,
            contexts,
            consumer,
        ) in enumerate(repository_jobs, start=1):
            print(
                f"APT repository resolve {job_number}/{len(repository_jobs)}: "
                f"{package} ({identity}); declared-repositories={len(contexts)}",
                file=sys.stderr,
                flush=True,
            )
            matched = False
            unique_contexts: dict[
                tuple[str, str, tuple[str, ...]], dict[str, object]
            ] = {}
            for context in contexts:
                raw_components = context.get("components", [])
                components = (
                    tuple(str(item) for item in raw_components)
                    if isinstance(raw_components, list)
                    else ()
                )
                key = (
                    str(context.get("upstream_url", "")),
                    str(context.get("suite", "")),
                    components,
                )
                unique_contexts[key] = context
            for context in unique_contexts.values():
                record = self._resolve_repository_context(
                    package,
                    environment,
                    context,
                    gateway_url=gateway_url,
                    timeout_seconds=timeout_seconds,
                )
                if record is not None:
                    records.append((identity, package, record, consumer))
                    matched = True
            if not matched:
                results.append(
                    WarmResult(
                        "apt",
                        package,
                        identity,
                        "failed",
                        "package was not found in its exact task-declared Gateway repositories",
                        environment_id=str(consumer.get("environment_id") or "") or None,
                        build_contexts=tuple(
                            str(item)
                            for item in consumer.get("build_contexts", [])
                            if isinstance(consumer.get("build_contexts", []), (list, tuple))
                        ),
                    )
                )
        for (identity, _analysis_environment), (
            environment,
            packages,
            consumers,
        ) in grouped_missing.items():
            try:
                upstream = self._resolve(
                    environment,
                    sorted(packages),
                    source_mode="upstream",
                    gateway_url=gateway_url,
                    timeout_seconds=max(300.0, timeout_seconds * 20),
                    work_dir=work_dir,
                )
            except subprocess.TimeoutExpired:
                results.extend(
                    WarmResult(
                        "apt",
                        package,
                        identity,
                        "failed",
                        "Gateway fallback distribution metadata resolver timed out",
                        environment_id=str(
                            consumers[package].get("environment_id") or ""
                        )
                        or None,
                        build_contexts=tuple(
                            str(item)
                            for item in consumers[package].get(
                                "build_contexts", []
                            )
                            if isinstance(
                                consumers[package].get("build_contexts", []),
                                (list, tuple),
                            )
                        ),
                    )
                    for package in sorted(packages)
                )
                continue
            for package in packages:
                record = upstream[package]
                if record["state"] != "resolved":
                    results.append(
                        WarmResult(
                            "apt",
                            package,
                            identity,
                            "failed",
                            "package is unavailable in both domestic and official distribution metadata",
                            environment_id=str(consumers[package].get("environment_id") or "") or None,
                            build_contexts=tuple(
                                str(item)
                                for item in consumers[package].get("build_contexts", [])
                                if isinstance(consumers[package].get("build_contexts", []), (list, tuple))
                            ),
                        )
                    )
                    continue
                records.append((identity, package, record, consumers[package]))
        unique_records: dict[
            tuple[str, str, str, str, str],
            tuple[str, str, dict[str, object], dict[str, object]],
        ] = {}
        for identity, package, record, consumer in records:
            key = (
                identity,
                str(consumer.get("environment_id") or ""),
                package,
                str(record.get("site") or ""),
                str(record.get("filename") or ""),
            )
            unique_records[key] = (identity, package, record, consumer)
        ordered_records = sorted(
            unique_records.values(),
            key=lambda item: (
                item[1],
                item[0],
                str(item[2].get("filename") or ""),
                str(item[3].get("environment_id") or ""),
            ),
        )
        warmed_artifacts: dict[tuple[str, str, str], WarmResult] = {}
        for identity, package, record, consumer in ordered_records:
            environment = environment_from_identity(identity)
            assert environment is not None
            site = str(record.get("site") or "")
            source = self._gateway_source(site, environment)
            artifact_key = (
                source,
                str(record["filename"]),
                str(record.get("sha256") or ""),
            )
            warmed = warmed_artifacts.get(artifact_key)
            if warmed is None:
                warmed = self._fetch_to_cache(
                    gateway_url,
                    source,
                    str(record["filename"]),
                    timeout_seconds,
                    str(record.get("sha256") or "") or None,
                )
                warmed_artifacts[artifact_key] = warmed
            results.append(
                WarmResult(
                    manager="apt",
                    package=package,
                    environment=identity,
                    status=warmed.status,
                    reason=warmed.reason,
                    gateway_url=warmed.gateway_url,
                    first_cache_state=warmed.first_cache_state,
                    verification_cache_state=warmed.verification_cache_state,
                    size=warmed.size,
                    environment_id=str(consumer.get("environment_id") or "") or None,
                    build_contexts=tuple(
                        str(item)
                        for item in consumer.get("build_contexts", [])
                        if isinstance(consumer.get("build_contexts", []), (list, tuple))
                    ),
                )
            )
            print(
                f"APT cache warm {package} ({identity}): {warmed.status}",
                file=sys.stderr,
                flush=True,
            )
        return sorted(results, key=lambda row: (row.package, row.environment or ""))

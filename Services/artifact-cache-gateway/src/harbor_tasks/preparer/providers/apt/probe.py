"""APT probe: resolve packages inside a container and produce ProbeResults."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from typing import Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import ProxyHandler, Request, build_opener

from ...models import (
    AptEnvironment,
    ProbeResult,
    ProbeSettings,
)
from .environments import environment_for_image
from .resolver import (
    _APT_PACKAGE_NAME,
    _RESOLVER_SCRIPT,
)


class _ProbeMixin:
    @staticmethod
    def _row_environments(row: dict[str, object]) -> tuple[AptEnvironment, ...]:
        raw_images = row.get("images", [])
        images = raw_images if isinstance(raw_images, list) else []
        environments = {
            environment.identity: environment
            for image in images
            if (environment := environment_for_image(str(image))) is not None
        }
        return tuple(environments[key] for key in sorted(environments))

    @staticmethod
    def _contexts_for_environment(
        raw_contexts: object, environment: AptEnvironment
    ) -> tuple[dict[str, object], ...]:
        if not isinstance(raw_contexts, (list, tuple)):
            return ()
        selected: list[dict[str, object]] = []
        for context in raw_contexts:
            if not isinstance(context, dict):
                continue
            raw_images = context.get("images")
            if not isinstance(raw_images, list):
                # Backward compatibility for reports generated before the
                # task-image field was added.
                selected.append(context)
                continue
            identities = {
                candidate.identity
                for image in raw_images
                if (candidate := environment_for_image(str(image))) is not None
            }
            if environment.identity in identities:
                selected.append(context)
        return tuple(selected)

    def _resolve(
        self,
        environment: AptEnvironment,
        packages: Sequence[str],
        *,
        source_mode: str,
        gateway_url: str,
        timeout_seconds: float,
        work_dir: Path,
    ) -> dict[str, dict[str, object]]:
        invalid = [package for package in packages if not _APT_PACKAGE_NAME.fullmatch(package)]
        if invalid:
            raise ValueError(f"invalid APT package name: {invalid[0]!r}")
        work_dir.mkdir(parents=True, exist_ok=True)
        package_file = work_dir / (
            f"apt-{environment.distro}-{environment.codename}-{uuid.uuid4().hex}.txt"
        )
        package_file.write_text("\n".join(sorted(set(packages))) + "\n", encoding="utf-8")
        os.chmod(package_file, 0o644)
        container_name = f"artifact-cache-apt-probe-{uuid.uuid4().hex[:12]}"
        command = [
            "docker",
            "run",
            "--rm",
            "--name",
            container_name,
            "--network",
            "host",
            "--env",
            "HTTP_PROXY=",
            "--env",
            "HTTPS_PROXY=",
            "--env",
            "ALL_PROXY=",
            "--env",
            "NO_PROXY=*",
            "--env",
            f"APT_DISTRO={environment.distro}",
            "--env",
            f"APT_CODENAME={environment.codename}",
            "--env",
            f"APT_SOURCE_MODE={source_mode}",
            "--env",
            f"APT_GATEWAY={gateway_url.rstrip('/')}",
            "--volume",
            f"{package_file}:/probe/packages.txt:ro",
            environment.resolver_image,
            "bash",
            "-euc",
            _RESOLVER_SCRIPT,
        ]
        print(
            f"APT {source_mode} resolver {environment.identity}: {len(set(packages))} packages; "
            f"container={container_name}; observe with: docker logs -f {container_name}",
            file=sys.stderr,
            flush=True,
        )
        try:
            output = self.command_runner(command, timeout_seconds, container_name)
        finally:
            package_file.unlink(missing_ok=True)
        resolved: dict[str, dict[str, object]] = {}
        for line in output.splitlines():
            if not line.startswith("ACG\t"):
                continue
            fields = line.split("\t")
            fields += [""] * (8 - len(fields))
            _, package, state, version, filename, sha256, size, site = fields[:8]
            resolved[package] = {
                "state": state,
                "version": version or None,
                "filename": filename or None,
                "sha256": sha256 or None,
                "size": int(size) if size.isdigit() else None,
                "site": site or None,
            }
        for package in packages:
            resolved.setdefault(
                package,
                {
                    "state": "unavailable",
                    "version": None,
                    "filename": None,
                    "sha256": None,
                    "size": None,
                    "site": None,
                },
            )
        return resolved

    @staticmethod
    def _sample(
        package: str,
        environment: AptEnvironment,
        record: dict[str, object],
        settings: ProbeSettings,
    ) -> ProbeResult:
        filename = str(record["filename"])
        site = str(record["site"]).rstrip("/")
        url = f"{site}/{quote(filename, safe='/+~._-')}"
        request = Request(
            url,
            headers={
                "Range": f"bytes=0-{settings.sample_bytes - 1}",
                "Accept-Encoding": "identity",
                "User-Agent": "artifact-cache-package-probe/0.2",
            },
            method="GET",
        )
        opener = build_opener(ProxyHandler({}))
        started = time.monotonic()
        last_error: Exception | None = None
        content = b""
        for _attempt in range(2):
            try:
                with opener.open(request, timeout=settings.timeout_seconds) as response:
                    content = response.read(settings.sample_bytes)
                last_error = None
                break
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                last_error = exc
        if last_error is not None:
            reason = (
                f"domestic artifact returned HTTP {last_error.code} twice"
                if isinstance(last_error, HTTPError)
                else f"domestic artifact request failed twice: {type(last_error).__name__}"
            )
            return ProbeResult(
                "apt",
                package,
                environment.identity,
                "unavailable",
                reason,
                domestic_source=site,
                domestic_url=url,
                filename=filename,
                version=record.get("version"),
                sha256=record.get("sha256"),
                size=record.get("size"),
            )
        elapsed = max(time.monotonic() - started, 1e-9)
        speed = len(content) / elapsed
        slow_by_rate = (
            len(content) >= settings.sample_bytes
            and speed < settings.sample_bytes / settings.slow_seconds
        )
        status = "slow" if elapsed >= settings.slow_seconds or slow_by_rate else "fast"
        reason = (
            f"sample exceeded {settings.slow_seconds:.3f}s threshold"
            if status == "slow"
            else "domestic artifact sample completed within threshold"
        )
        return ProbeResult(
            "apt",
            package,
            environment.identity,
            status,
            reason,
            domestic_source=site,
            domestic_url=url,
            filename=filename,
            version=record.get("version"),
            sha256=record.get("sha256"),
            size=record.get("size"),
            elapsed_seconds=elapsed,
            bytes_sampled=len(content),
            bytes_per_second=speed,
        )

    def probe(
        self,
        package_rows: Sequence[dict[str, object]],
        settings: ProbeSettings,
        *,
        work_dir: Path,
    ) -> list[ProbeResult]:
        settings.validate()
        grouped: dict[
            tuple[str, str], tuple[AptEnvironment, set[str]]
        ] = {}
        repository_contexts: dict[
            tuple[str, str, str], tuple[dict[str, object], ...]
        ] = {}
        build_contexts: dict[tuple[str, str, str], tuple[str, ...]] = {}
        unsupported: list[ProbeResult] = []
        for row in package_rows:
            package = str(row.get("package", ""))
            raw_package_contexts = row.get("contexts", [])
            package_contexts = (
                [item for item in raw_package_contexts if isinstance(item, dict)]
                if isinstance(raw_package_contexts, list)
                else []
            )
            inputs = package_contexts or [row]
            environments_found = False
            for package_context in inputs:
                analysis_environment = str(
                    package_context.get("environment_id") or ""
                )
                raw_build_contexts = package_context.get("build_contexts", [])
                consumer_contexts = tuple(
                    sorted(
                        str(item)
                        for item in raw_build_contexts
                        if isinstance(raw_build_contexts, (list, tuple))
                    )
                )
                environments = self._row_environments(package_context)
                for environment in environments:
                    environments_found = True
                    group_key = (
                        environment.identity,
                        analysis_environment or environment.identity,
                    )
                    current = grouped.setdefault(
                        group_key, (environment, set())
                    )
                    current[1].add(package)
                    contexts = self._contexts_for_environment(
                        row.get("repository_contexts", []), environment
                    )
                    if analysis_environment:
                        contexts = tuple(
                            context
                            for context in contexts
                            if not context.get("environment_id")
                            or context.get("environment_id") == analysis_environment
                        )
                    lookup = (*group_key, package)
                    repository_contexts[lookup] = contexts
                    build_contexts[lookup] = consumer_contexts
            if not environments_found:
                unsupported.append(
                    ProbeResult(
                        "apt",
                        package,
                        None,
                        "unsupported",
                        "no supported distro/codename could be derived from task images",
                    )
                )

        resolved_rows: list[
            tuple[str, AptEnvironment, str, dict[str, object]]
        ] = []
        for group_key, (environment, packages) in grouped.items():
            analysis_environment = (
                group_key[1] if group_key[1] != environment.identity else ""
            )
            try:
                resolved = self._resolve(
                    environment,
                    sorted(packages),
                    source_mode="domestic",
                    gateway_url="http://127.0.0.1:8080/v1/cache",
                    timeout_seconds=max(300.0, settings.timeout_seconds * 20),
                    work_dir=work_dir,
                )
            except subprocess.TimeoutExpired:
                for package in sorted(packages):
                    unsupported.append(
                        ProbeResult(
                            "apt",
                            package,
                            environment.identity,
                            "unavailable",
                            "domestic distribution metadata resolver timed out",
                            environment_id=analysis_environment or None,
                            build_contexts=build_contexts.get(
                                (*group_key, package), ()
                            ),
                            repository_contexts=repository_contexts.get(
                                (*group_key, package), ()
                            ),
                        )
                    )
                continue
            for package in sorted(packages):
                record = resolved[package]
                if record["state"] != "resolved":
                    unsupported.append(
                        ProbeResult(
                            "apt",
                            package,
                            environment.identity,
                            "unavailable",
                            "package has no candidate in the domestic distribution metadata",
                            version=record.get("version"),
                            environment_id=analysis_environment or None,
                            build_contexts=build_contexts.get(
                                (*group_key, package), ()
                            ),
                            repository_contexts=repository_contexts.get(
                                (*group_key, package), ()
                            ),
                        )
                    )
                else:
                    resolved_rows.append(
                        (package, environment, analysis_environment, record)
                    )

        sampled: list[ProbeResult] = []
        completed = 0
        problems = len([row for row in unsupported if row.status == "unavailable"])
        lock = threading.Lock()
        with ThreadPoolExecutor(max_workers=settings.concurrency) as executor:
            futures = {
                executor.submit(
                    self._sample, package, environment, record, settings
                ): (package, environment.identity, analysis_environment)
                for package, environment, analysis_environment, record in resolved_rows
            }
            for future in as_completed(futures):
                package, identity, analysis_environment = futures[future]
                group_key = (identity, analysis_environment or identity)
                result = replace(
                    future.result(),
                    environment_id=analysis_environment or None,
                    build_contexts=build_contexts.get(
                        (*group_key, package), ()
                    ),
                    repository_contexts=repository_contexts.get(
                        (*group_key, package), ()
                    ),
                )
                sampled.append(result)
                with lock:
                    completed += 1
                    if result.status != "fast":
                        problems += 1
                    if completed % 25 == 0 or completed == len(futures):
                        print(
                            f"APT domestic artifact probes: {completed}/{len(futures)}; "
                            f"slow-or-unavailable={problems}",
                            file=sys.stderr,
                            flush=True,
                        )
        return sorted(
            [*sampled, *unsupported],
            key=lambda row: (row.package, row.environment or "", row.status),
        )

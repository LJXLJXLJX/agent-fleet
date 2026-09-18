"""PipProvider: probing and warming."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from typing import Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from ...models import (
    PipEnvironment,
    ProbeResult,
    ProbeSettings,
    WarmResult,
)
from .environments import (
    _environment,
    _normalized_python_version,
    environment_for_image,
    environment_from_identity,
)
from .resolver import (
    _PYPI_FILES_ORIGIN,
    _RESOLVER_SCRIPT,
    _TUNA_SIMPLE,
    CommandRunner,
    _run_container,
)


class PipProvider:
    manager = "pip"

    def __init__(self, command_runner: CommandRunner = _run_container):
        self.command_runner = command_runner

    @staticmethod
    def _row_environments(row: dict[str, object]) -> tuple[PipEnvironment, ...]:
        raw_images = row.get("images", [])
        images = raw_images if isinstance(raw_images, list) else []
        raw_options = row.get("resolution_options", {})
        options = raw_options if isinstance(raw_options, dict) else {}
        python_override = _normalized_python_version(
            options.get("python_version") or row.get("python_version")
        )
        environments: dict[str, PipEnvironment] = {}
        for image in images:
            value = environment_for_image(str(image))
            if value is None:
                continue
            if python_override and python_override != value.python_version:
                value = _environment(
                    python_override,
                    value.platform,
                    "m.daocloud.io/docker.io/library/"
                    f"python:{python_override}-slim",
                )
            platform = str(options.get("platform") or value.platform)
            implementation_value = str(
                options.get("implementation") or value.implementation
            ).lower()
            implementation = {
                "cp": "cpython",
                "cpython": "cpython",
                "pp": "pypy",
                "pypy": "pypy",
            }.get(implementation_value, implementation_value)
            abi = str(options.get("abi") or value.abi)
            architecture = (
                "arm64"
                if platform.endswith(("_aarch64", "_arm64"))
                else value.architecture
            )
            value = PipEnvironment(
                implementation=implementation,
                python_version=value.python_version,
                abi=abi,
                platform=platform,
                architecture=architecture,
                resolver_image=value.resolver_image,
            )
            environments[value.identity] = value
        return tuple(environments[key] for key in sorted(environments))

    @staticmethod
    def _requirements(row: dict[str, object]) -> tuple[str, ...]:
        raw_specs = row.get("specs", {})
        if isinstance(raw_specs, dict):
            values = [str(value) for value in raw_specs]
        elif isinstance(raw_specs, list):
            values = [str(value) for value in raw_specs]
        else:
            values = []
        return tuple(sorted(set(values or [str(row.get("package", ""))])))

    def _resolve(
        self,
        environment: PipEnvironment,
        requirements: Sequence[tuple[str, str]],
        *,
        index_url: str,
        prefer_fallback: bool = False,
        timeout_seconds: float,
        work_dir: Path,
    ) -> dict[tuple[str, str], dict[str, object]]:
        work_dir.mkdir(parents=True, exist_ok=True)
        requirement_file = work_dir / f"pip-{uuid.uuid4().hex}.jsonl"
        requirement_file.write_text(
            "".join(
                json.dumps({"package": package, "requirement": requirement})
                + "\n"
                for package, requirement in sorted(set(requirements))
            ),
            encoding="utf-8",
        )
        os.chmod(requirement_file, 0o644)
        container_name = f"artifact-cache-pip-probe-{uuid.uuid4().hex[:12]}"
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
            f"PIP_PROBE_INDEX={index_url.rstrip('/')}",
            "--env",
            f"PIP_PROBE_TIMEOUT={timeout_seconds}",
            "--env",
            f"PIP_PROBE_PYTHON={environment.python_version}",
            "--env",
            f"PIP_PROBE_PLATFORM={environment.platform}",
            "--env",
            f"PIP_PROBE_PREFER_FALLBACK={'1' if prefer_fallback else '0'}",
            "--volume",
            f"{requirement_file}:/probe/requirements.jsonl:ro",
            environment.resolver_image,
            "python",
            "-c",
            _RESOLVER_SCRIPT,
        ]
        print(
            f"pip resolver {environment.identity}: {len(set(requirements))} requirements; "
            f"index={index_url}; container={container_name}; observe with: "
            f"docker logs -f {container_name}",
            file=sys.stderr,
            flush=True,
        )
        try:
            output = self.command_runner(command, timeout_seconds, container_name)
        finally:
            requirement_file.unlink(missing_ok=True)
        resolved: dict[tuple[str, str], dict[str, object]] = {}
        for line in output.splitlines():
            if not line.startswith("ACG\t"):
                continue
            value = json.loads(line.split("\t", 1)[1])
            key = str(value.get("package", "")), str(
                value.get("requirement", "")
            )
            resolved[key] = value
        for package, requirement in requirements:
            resolved.setdefault(
                (package, requirement),
                {
                    "package": package,
                    "requirement": requirement,
                    "state": "unavailable",
                    "reason": "resolver returned no result",
                },
            )
        return resolved

    @staticmethod
    def _result_fields(
        environment: PipEnvironment, requirement: str
    ) -> dict[str, object]:
        return {
            "requirement": requirement,
            "python_version": environment.python_version,
            "implementation": environment.implementation,
            "abi": environment.abi,
            "platform": environment.platform,
        }

    @classmethod
    def _sample(
        cls,
        package: str,
        requirement: str,
        environment: PipEnvironment,
        record: dict[str, object],
        settings: ProbeSettings,
    ) -> ProbeResult:
        url = str(record["url"])
        request = Request(
            url,
            headers={
                "Range": f"bytes=0-{settings.sample_bytes - 1}",
                "Accept-Encoding": "identity",
                "User-Agent": "artifact-cache-pip-probe/0.2",
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
        common = cls._result_fields(environment, requirement)
        if last_error is not None:
            return ProbeResult(
                "pip",
                package,
                environment.identity,
                "unavailable",
                f"domestic artifact request failed twice: {type(last_error).__name__}",
                domestic_source=_TUNA_SIMPLE,
                domestic_url=url,
                filename=str(record.get("filename") or "") or None,
                version=str(record.get("version") or "") or None,
                sha256=str(record.get("sha256") or "") or None,
                **common,
            )
        elapsed = max(time.monotonic() - started, 1e-9)
        speed = len(content) / elapsed
        slow = elapsed >= settings.slow_seconds or (
            len(content) >= settings.sample_bytes
            and speed < settings.sample_bytes / settings.slow_seconds
        )
        return ProbeResult(
            "pip",
            package,
            environment.identity,
            "slow" if slow else "fast",
            "sample exceeded threshold"
            if slow
            else "domestic compatible artifact sample completed within threshold",
            domestic_source=_TUNA_SIMPLE,
            domestic_url=url,
            filename=str(record.get("filename") or "") or None,
            version=str(record.get("version") or "") or None,
            sha256=str(record.get("sha256") or "") or None,
            elapsed_seconds=elapsed,
            bytes_sampled=len(content),
            bytes_per_second=speed,
            **common,
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
            tuple[str, str], tuple[PipEnvironment, set[tuple[str, str]]]
        ] = {}
        consumers: dict[tuple[str, str, str, str], set[str]] = {}
        results: list[ProbeResult] = []
        for row in package_rows:
            package = str(row.get("package", ""))
            raw_contexts = row.get("contexts", [])
            contexts = (
                [value for value in raw_contexts if isinstance(value, dict)]
                if isinstance(raw_contexts, list)
                else []
            )
            if not contexts:
                contexts = [
                    {
                        "requirement": requirement,
                        "images": row.get("images", []),
                    }
                    for requirement in self._requirements(row)
                ]
            environments_found = False
            for context in contexts:
                requirement = str(context.get("requirement") or package)
                analysis_environment = str(context.get("environment_id") or "")
                raw_build_contexts = context.get("build_contexts", [])
                build_contexts = {
                    str(value)
                    for value in raw_build_contexts
                    if isinstance(raw_build_contexts, (list, tuple))
                }
                environments = self._row_environments(context)
                for environment in environments:
                    environments_found = True
                    expected_abi = f"cp{environment.python_version.replace('.', '')}"
                    if (
                        environment.implementation != "cpython"
                        or environment.architecture != "amd64"
                        or environment.abi != expected_abi
                        or not environment.platform.startswith(
                            ("manylinux_", "native-linux-")
                        )
                    ):
                        results.append(
                            ProbeResult(
                                "pip",
                                package,
                                environment.identity,
                                "unsupported",
                                "explicit pip implementation/ABI/platform is outside the current Linux amd64 resolver",
                                requirement=requirement,
                                python_version=environment.python_version,
                                implementation=environment.implementation,
                                abi=environment.abi,
                                platform=environment.platform,
                                environment_id=analysis_environment or None,
                                build_contexts=tuple(sorted(build_contexts)),
                            )
                        )
                        continue
                    group_key = (
                        environment.identity,
                        analysis_environment or environment.identity,
                    )
                    current = grouped.setdefault(
                        group_key, (environment, set())
                    )
                    current[1].add((package, requirement))
                    consumers.setdefault(
                        (*group_key, package, requirement), set()
                    ).update(build_contexts)
            if not environments_found:
                results.append(
                    ProbeResult(
                        "pip",
                        package,
                        None,
                        "unsupported",
                        "no supported Python/ABI/platform could be derived from task images",
                    )
                )

        samples: list[
            tuple[str, str, PipEnvironment, str, dict[str, object]]
        ] = []
        for group_key, (environment, requirements) in grouped.items():
            analysis_environment = (
                group_key[1] if group_key[1] != environment.identity else ""
            )
            try:
                resolved = self._resolve(
                    environment,
                    sorted(requirements),
                    index_url=_TUNA_SIMPLE,
                    prefer_fallback=False,
                    timeout_seconds=max(300.0, settings.timeout_seconds * 20),
                    work_dir=work_dir,
                )
            except subprocess.TimeoutExpired:
                for package, requirement in sorted(requirements):
                    results.append(
                        ProbeResult(
                            "pip",
                            package,
                            environment.identity,
                            "unavailable",
                            "domestic PyPI resolver timed out",
                            domestic_source=_TUNA_SIMPLE,
                            environment_id=analysis_environment or None,
                            build_contexts=tuple(
                                sorted(
                                    consumers.get(
                                        (*group_key, package, requirement), set()
                                    )
                                )
                            ),
                            **self._result_fields(environment, requirement),
                        )
                    )
                continue
            for package, requirement in sorted(requirements):
                record = resolved[(package, requirement)]
                state = str(record.get("state", "unavailable"))
                if state == "resolved":
                    samples.append(
                        (package, requirement, environment, analysis_environment, record)
                    )
                    continue
                status = (
                    state
                    if state in {"unsupported", "not-applicable"}
                    else "unavailable"
                )
                results.append(
                    ProbeResult(
                        "pip",
                        package,
                        environment.identity,
                        status,  # type: ignore[arg-type]
                        str(record.get("reason") or "no compatible domestic artifact"),
                        domestic_source=_TUNA_SIMPLE,
                        environment_id=analysis_environment or None,
                        build_contexts=tuple(
                            sorted(
                                consumers.get(
                                    (*group_key, package, requirement), set()
                                )
                            )
                        ),
                        **self._result_fields(environment, requirement),
                    )
                )

        completed = 0
        with ThreadPoolExecutor(max_workers=settings.concurrency) as executor:
            futures = {
                executor.submit(
                    self._sample,
                    package,
                    requirement,
                    environment,
                    record,
                    settings,
                ): (package, requirement, environment, analysis_environment)
                for package, requirement, environment, analysis_environment, record in samples
            }
            for future in as_completed(futures):
                package, requirement, environment, analysis_environment = futures[future]
                group_key = (
                    environment.identity,
                    analysis_environment or environment.identity,
                )
                results.append(
                    replace(
                        future.result(),
                        environment_id=analysis_environment or None,
                        build_contexts=tuple(
                            sorted(
                                consumers.get(
                                    (*group_key, package, requirement), set()
                                )
                            )
                        ),
                    )
                )
                completed += 1
                if completed % 25 == 0 or completed == len(futures):
                    problems = sum(
                        row.status in {"slow", "unavailable"} for row in results
                    )
                    print(
                        f"pip domestic artifact probes: {completed}/{len(futures)}; "
                        f"slow-or-unavailable={problems}",
                        file=sys.stderr,
                        flush=True,
                    )
        return sorted(
            results,
            key=lambda row: (
                row.package,
                row.requirement or "",
                row.environment or "",
            ),
        )

    @staticmethod
    def _fetch_to_cache(
        package: str,
        requirement: str,
        environment: PipEnvironment,
        record: dict[str, object],
        *,
        gateway_url: str,
        timeout_seconds: float,
    ) -> WarmResult:
        upstream_url = str(record.get("url") or "")
        parsed = urlsplit(upstream_url)
        gateway_prefix = f"{gateway_url.rstrip('/')}/pypi-files/"
        if upstream_url.startswith(gateway_prefix):
            relative_path = upstream_url[len(gateway_prefix) :]
        elif f"{parsed.scheme}://{parsed.netloc}" == _PYPI_FILES_ORIGIN:
            relative_path = parsed.path.lstrip("/")
        else:
            relative_path = ""
        if not relative_path.startswith("packages/") or parsed.query:
            return WarmResult(
                "pip",
                package,
                environment.identity,
                "failed",
                "resolved artifact is outside the fixed files.pythonhosted.org/packages allowlist",
                requirement=requirement,
            )
        gateway_artifact = f"{gateway_prefix}{quote(relative_path, safe='/+~._-')}"
        opener = build_opener(ProxyHandler({}))
        digest = hashlib.sha256()
        size = 0
        try:
            with opener.open(
                Request(
                    gateway_artifact,
                    headers={"X-Artifact-Cache-Prefer-Fallback": "1"},
                    method="GET",
                ),
                timeout=timeout_seconds,
            ) as response:
                first_state = response.headers.get("X-Artifact-Cache", "")
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    size += len(chunk)
            with opener.open(
                Request(gateway_artifact, method="HEAD"), timeout=timeout_seconds
            ) as response:
                verification = response.headers.get("X-Artifact-Cache", "")
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            return WarmResult(
                "pip",
                package,
                environment.identity,
                "failed",
                f"Gateway cache request failed: {type(exc).__name__}",
                requirement=requirement,
                gateway_url=gateway_artifact,
            )
        expected = str(record.get("sha256") or "")
        if expected and digest.hexdigest() != expected:
            return WarmResult(
                "pip",
                package,
                environment.identity,
                "failed",
                "Gateway object SHA-256 does not match PyPI simple metadata",
                requirement=requirement,
                gateway_url=gateway_artifact,
                first_cache_state=first_state,
                verification_cache_state=verification,
                size=size,
            )
        if verification != "HIT":
            return WarmResult(
                "pip",
                package,
                environment.identity,
                "failed",
                f"Gateway verification returned {verification or 'no cache state'}",
                requirement=requirement,
                gateway_url=gateway_artifact,
                first_cache_state=first_state,
                verification_cache_state=verification,
                size=size,
            )
        return WarmResult(
            "pip",
            package,
            environment.identity,
            "already-cached" if first_state == "HIT" else "cached",
            "compatible PyPI artifact is cached and verified",
            requirement=requirement,
            gateway_url=gateway_artifact,
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
        grouped: dict[
            tuple[str, str], tuple[PipEnvironment, list[dict[str, object]]]
        ] = {}
        results: list[WarmResult] = []
        warmed_artifacts: dict[tuple[str, str], WarmResult] = {}
        for row in rows:
            identity = str(row.get("environment") or "")
            analysis_environment = str(row.get("environment_id") or "")
            environment = environment_from_identity(identity)
            package = str(row.get("package", ""))
            requirement = str(row.get("requirement") or package)
            if environment is None:
                results.append(
                    WarmResult(
                        "pip",
                        package,
                        identity or None,
                        "not-actionable",
                        "pip environment identity is unsupported",
                        requirement=requirement,
                        environment_id=analysis_environment or None,
                        build_contexts=tuple(
                            str(item)
                            for item in row.get("build_contexts", [])
                            if isinstance(row.get("build_contexts", []), (list, tuple))
                        ),
                    )
                )
                continue
            grouped.setdefault(
                (identity, analysis_environment), (environment, [])
            )[1].append(row)

        for environment, environment_rows in grouped.values():
            requirements: list[tuple[str, str]] = []
            original: dict[tuple[str, str], dict[str, object]] = {}
            for row in environment_rows:
                package = str(row.get("package", ""))
                requirement = str(row.get("requirement") or package)
                version = str(row.get("version") or "")
                resolution_requirement = (
                    f"{package}=={version}" if version else requirement
                )
                requirements.append((package, resolution_requirement))
                original[(package, resolution_requirement)] = row
            try:
                resolved = self._resolve(
                    environment,
                    requirements,
                    index_url=f"{gateway_url.rstrip('/')}/pypi-simple",
                    prefer_fallback=True,
                    timeout_seconds=timeout_seconds,
                    work_dir=work_dir,
                )
            except subprocess.TimeoutExpired:
                results.extend(
                    WarmResult(
                        "pip",
                        str(row.get("package", "")),
                        environment.identity,
                        "failed",
                        "Gateway fallback PyPI resolver timed out",
                        requirement=str(
                            row.get("requirement") or row.get("package", "")
                        ),
                        environment_id=str(row.get("environment_id") or "")
                        or None,
                        build_contexts=tuple(
                            str(item)
                            for item in row.get("build_contexts", [])
                            if isinstance(
                                row.get("build_contexts", []), (list, tuple)
                            )
                        ),
                    )
                    for row in environment_rows
                )
                continue
            for package, resolution_requirement in requirements:
                row = original[(package, resolution_requirement)]
                requirement = str(row.get("requirement") or package)
                record = resolved[(package, resolution_requirement)]
                if record.get("state") != "resolved":
                    results.append(
                        WarmResult(
                            "pip",
                            package,
                            environment.identity,
                            "failed",
                            str(record.get("reason") or "official PyPI resolution failed"),
                            requirement=requirement,
                            environment_id=str(row.get("environment_id") or "") or None,
                            build_contexts=tuple(
                                str(item)
                                for item in row.get("build_contexts", [])
                                if isinstance(row.get("build_contexts", []), (list, tuple))
                            ),
                        )
                    )
                    continue
                artifact_key = (
                    str(record.get("url") or ""),
                    str(record.get("sha256") or ""),
                )
                warmed = warmed_artifacts.get(artifact_key)
                if warmed is None:
                    warmed = self._fetch_to_cache(
                        package,
                        requirement,
                        environment,
                        record,
                        gateway_url=gateway_url,
                        timeout_seconds=timeout_seconds,
                    )
                    warmed_artifacts[artifact_key] = warmed
                results.append(
                    replace(
                        warmed,
                        environment_id=str(row.get("environment_id") or "") or None,
                        build_contexts=tuple(
                            str(item)
                            for item in row.get("build_contexts", [])
                            if isinstance(row.get("build_contexts", []), (list, tuple))
                        ),
                    )
                )
        return results

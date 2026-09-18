from __future__ import annotations

import base64
import hashlib
import hmac
import importlib.resources
import json
import os
import re
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from typing import Callable, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from ..models import ProbeResult, ProbeSettings, WarmResult

_DOMESTIC_REGISTRY = "https://registry.npmmirror.com"
_OFFICIAL_REGISTRY = "https://registry.npmjs.org"
_GATEWAY_SOURCE = "npm-registry"
_ENVIRONMENT = "npm:registry-v1"
_DEFAULT_RESOLVER_IMAGE = (
    "node:20-bookworm"
)
_IMAGE_REFERENCE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9./:@_-]{0,511}$")
_PACKAGE_NAME = re.compile(
    r"^(?:@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9][a-z0-9._~-]*$",
    re.IGNORECASE,
)
_PUBLIC_REGISTRY_ORIGINS = {_DOMESTIC_REGISTRY, _OFFICIAL_REGISTRY}


_RESOLVER_SCRIPT = (
    importlib.resources.files(__package__)
    .joinpath("npm_resolver.js")
    .read_text(encoding="utf-8")
)


CommandRunner = Callable[[list[str], float, str], str]


def _run_container(command: list[str], timeout: float, name: str) -> str:
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        subprocess.run(
            ["docker", "stop", "--timeout", "5", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        raise
    if process.returncode != 0:
        tail = "\n".join(stderr.splitlines()[-20:])
        raise RuntimeError(
            f"npm resolver container failed with exit {process.returncode}: {tail}"
        )
    return stdout


def _resolution_environment_id(registry: str) -> str:
    digest = hashlib.sha256(
        json.dumps(
            {"manager": "npm", "registry": registry},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:16]
    return f"env-{digest}"


def _registry_origin(value: object) -> str | None:
    text = str(value or "").rstrip("/")
    try:
        parsed = urlsplit(text)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme.lower()}://{parsed.hostname.lower()}{port}"


def _consumer_fields(
    contexts: Sequence[dict[str, object]],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    environment_ids: set[str] = set()
    build_contexts: set[str] = set()
    for context in contexts:
        environment_id = str(context.get("environment_id") or "")
        if environment_id:
            environment_ids.add(environment_id)
        raw_build_contexts = context.get("build_contexts", [])
        if isinstance(raw_build_contexts, (list, tuple)):
            build_contexts.update(str(item) for item in raw_build_contexts)
    return tuple(sorted(environment_ids)), tuple(sorted(build_contexts))


def _integrity_digest(record: dict[str, object]) -> tuple[str, bytes] | None:
    ranks = {"sha512": 4, "sha384": 3, "sha256": 2, "sha1": 1}
    candidates: list[tuple[int, str, bytes]] = []
    for token in str(record.get("integrity") or "").split():
        value = token.split("?", 1)[0]
        algorithm, separator, encoded = value.partition("-")
        if not separator or algorithm not in ranks:
            continue
        try:
            expected = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            continue
        candidates.append((ranks[algorithm], algorithm, expected))
    if candidates:
        _rank, algorithm, expected = max(candidates)
        return algorithm, expected
    shasum = str(record.get("shasum") or "").lower()
    if re.fullmatch(r"[0-9a-f]{40}", shasum):
        return "sha1", bytes.fromhex(shasum)
    return None


def _tarball_relative_path(
    package: str, url: str, *, gateway_url: str | None = None
) -> str | None:
    parsed = urlsplit(url)
    origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
    if parsed.query or parsed.fragment:
        return None
    path = parsed.path
    if origin not in _PUBLIC_REGISTRY_ORIGINS:
        if not gateway_url:
            return None
        gateway = urlsplit(gateway_url.rstrip("/"))
        gateway_origin = f"{gateway.scheme.lower()}://{gateway.netloc.lower()}"
        gateway_prefix = f"{gateway.path.rstrip('/')}/{_GATEWAY_SOURCE}/"
        if origin != gateway_origin or not path.startswith(gateway_prefix):
            return None
        path = path[len(gateway_prefix) :]
    decoded = unquote(path).lstrip("/")
    segments = decoded.split("/")
    package_parts = package.split("/")
    expected = [*package_parts, "-"]
    if segments[: len(expected)] != expected or len(segments) != len(expected) + 1:
        return None
    if not segments[-1].endswith(".tgz"):
        return None
    return quote(decoded, safe="/@+~._-")


class NpmProvider:
    manager = "npm"

    def __init__(self, command_runner: CommandRunner = _run_container):
        self.command_runner = command_runner

    @staticmethod
    def _resolver_image() -> str:
        image = os.environ.get(
            "ARTIFACT_CACHE_NPM_RESOLVER_IMAGE", _DEFAULT_RESOLVER_IMAGE
        )
        if not _IMAGE_REFERENCE.fullmatch(image):
            raise ValueError("invalid npm resolver image reference")
        return image

    @staticmethod
    def _requirements(row: dict[str, object]) -> tuple[str, ...]:
        raw_specs = row.get("specs", {})
        if isinstance(raw_specs, dict):
            values = [str(item) for item in raw_specs]
        elif isinstance(raw_specs, list):
            values = [str(item) for item in raw_specs]
        else:
            values = []
        return tuple(sorted(set(values or [str(row.get("package", ""))])))

    def _resolve(
        self,
        requirements: Sequence[tuple[str, str]],
        *,
        registry: str,
        timeout_seconds: float,
        work_dir: Path,
    ) -> dict[tuple[str, str], dict[str, object]]:
        for package, requirement in requirements:
            if not _PACKAGE_NAME.fullmatch(package):
                raise ValueError(f"invalid npm package name: {package!r}")
            if not requirement or len(requirement) > 1024 or any(
                character in requirement for character in "\x00\r\n"
            ):
                raise ValueError(f"invalid npm requirement for {package!r}")
        work_dir.mkdir(parents=True, exist_ok=True)
        requirement_file = work_dir / f"npm-{uuid.uuid4().hex}.jsonl"
        requirement_file.write_text(
            "".join(
                json.dumps({"package": package, "requirement": requirement})
                + "\n"
                for package, requirement in sorted(set(requirements))
            ),
            encoding="utf-8",
        )
        os.chmod(requirement_file, 0o644)
        container_name = f"artifact-cache-npm-probe-{uuid.uuid4().hex[:12]}"
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
            f"NPM_PROBE_REGISTRY={registry.rstrip('/')}",
            "--env",
            f"NPM_PROBE_TIMEOUT={timeout_seconds}",
            "--volume",
            f"{requirement_file}:/probe/requirements.jsonl:ro",
            self._resolver_image(),
            "node",
            "-e",
            _RESOLVER_SCRIPT,
        ]
        print(
            f"npm resolver {_ENVIRONMENT}: {len(set(requirements))} requirements; "
            f"registry={registry}; container={container_name}; observe with: "
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
    def _sample(
        package: str,
        requirement: str,
        record: dict[str, object],
        settings: ProbeSettings,
    ) -> ProbeResult:
        url = str(record.get("url") or "")
        if _tarball_relative_path(package, url) is None:
            return ProbeResult(
                "npm",
                package,
                _ENVIRONMENT,
                "unavailable",
                "resolved tarball is outside the public npm Registry path policy",
                requirement=requirement,
                domestic_source=_DOMESTIC_REGISTRY,
            )
        request = Request(
            url,
            headers={
                "Range": f"bytes=0-{settings.sample_bytes - 1}",
                "Accept-Encoding": "identity",
                "User-Agent": "artifact-cache-npm-probe/0.2",
            },
        )
        opener = build_opener(ProxyHandler({}))
        started = time.monotonic()
        content = b""
        last_error: Exception | None = None
        for _attempt in range(2):
            try:
                with opener.open(request, timeout=settings.timeout_seconds) as response:
                    content = response.read(settings.sample_bytes)
                last_error = None
                break
            except (HTTPError, URLError, TimeoutError, OSError) as exc:
                last_error = exc
        common = {
            "requirement": requirement,
            "domestic_source": _DOMESTIC_REGISTRY,
            "domestic_url": url,
            "filename": str(record.get("filename") or "") or None,
            "version": str(record.get("version") or "") or None,
            "integrity": str(record.get("integrity") or "") or None,
            "shasum": str(record.get("shasum") or "") or None,
        }
        if last_error is not None:
            return ProbeResult(
                "npm",
                package,
                _ENVIRONMENT,
                "unavailable",
                f"domestic tarball request failed twice: {type(last_error).__name__}",
                **common,
            )
        elapsed = max(time.monotonic() - started, 1e-9)
        speed = len(content) / elapsed
        slow = elapsed >= settings.slow_seconds or (
            len(content) >= settings.sample_bytes
            and speed < settings.sample_bytes / settings.slow_seconds
        )
        return ProbeResult(
            "npm",
            package,
            _ENVIRONMENT,
            "slow" if slow else "fast",
            "sample exceeded threshold"
            if slow
            else "domestic npm tarball sample completed within threshold",
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
        jobs: dict[tuple[str, str], list[dict[str, object]]] = {}
        results: list[ProbeResult] = []
        for row in package_rows:
            package = str(row.get("package", ""))
            raw_contexts = row.get("contexts", [])
            contexts = (
                [item for item in raw_contexts if isinstance(item, dict)]
                if isinstance(raw_contexts, list)
                else []
            )
            if not contexts:
                contexts = [
                    {"requirement": requirement}
                    for requirement in self._requirements(row)
                ]
            for context in contexts:
                requirement = str(context.get("requirement") or package)
                raw_options = context.get("resolution_options", {})
                options = raw_options if isinstance(raw_options, dict) else {}
                registry = options.get("registry")
                if registry and _registry_origin(registry) not in _PUBLIC_REGISTRY_ORIGINS:
                    consumer_ids, build_contexts = _consumer_fields([context])
                    results.append(
                        ProbeResult(
                            "npm",
                            package,
                            _ENVIRONMENT,
                            "unsupported",
                            "explicit private or unknown npm Registry is outside the source policy",
                            requirement=requirement,
                            environment_id=_resolution_environment_id(
                                _DOMESTIC_REGISTRY
                            ),
                            consumer_environment_ids=consumer_ids,
                            build_contexts=build_contexts,
                        )
                    )
                    continue
                jobs.setdefault((package, requirement), []).append(context)
        requirements = sorted(jobs)
        if not requirements:
            return results
        try:
            resolved = self._resolve(
                requirements,
                registry=_DOMESTIC_REGISTRY,
                timeout_seconds=max(300.0, settings.timeout_seconds * 20),
                work_dir=work_dir,
            )
        except subprocess.TimeoutExpired:
            for package, requirement in requirements:
                consumer_ids, build_contexts = _consumer_fields(jobs[(package, requirement)])
                results.append(
                    ProbeResult(
                        "npm",
                        package,
                        _ENVIRONMENT,
                        "unavailable",
                        "domestic npm Registry resolver timed out",
                        requirement=requirement,
                        domestic_source=_DOMESTIC_REGISTRY,
                        environment_id=_resolution_environment_id(
                            _DOMESTIC_REGISTRY
                        ),
                        consumer_environment_ids=consumer_ids,
                        build_contexts=build_contexts,
                    )
                )
            return results
        samples: list[tuple[str, str, dict[str, object]]] = []
        for package, requirement in requirements:
            record = resolved[(package, requirement)]
            state = str(record.get("state") or "unavailable")
            consumer_ids, build_contexts = _consumer_fields(jobs[(package, requirement)])
            if state == "resolved":
                samples.append((package, requirement, record))
                continue
            status = "unsupported" if state == "unsupported" else "unavailable"
            results.append(
                ProbeResult(
                    "npm",
                    package,
                    _ENVIRONMENT,
                    status,  # type: ignore[arg-type]
                    str(record.get("reason") or "npm Registry resolution failed"),
                    requirement=requirement,
                    domestic_source=_DOMESTIC_REGISTRY,
                    environment_id=_resolution_environment_id(
                        _DOMESTIC_REGISTRY
                    ),
                    consumer_environment_ids=consumer_ids,
                    build_contexts=build_contexts,
                )
            )
        completed = 0
        with ThreadPoolExecutor(max_workers=settings.concurrency) as executor:
            futures = {
                executor.submit(
                    self._sample, package, requirement, record, settings
                ): (package, requirement)
                for package, requirement, record in samples
            }
            for future in as_completed(futures):
                package, requirement = futures[future]
                consumer_ids, build_contexts = _consumer_fields(
                    jobs[(package, requirement)]
                )
                results.append(
                    replace(
                        future.result(),
                        environment_id=_resolution_environment_id(
                            _DOMESTIC_REGISTRY
                        ),
                        consumer_environment_ids=consumer_ids,
                        build_contexts=build_contexts,
                    )
                )
                completed += 1
                if completed % 25 == 0 or completed == len(futures):
                    problems = sum(
                        row.status in {"slow", "unavailable"} for row in results
                    )
                    print(
                        f"npm domestic artifact probes: {completed}/{len(futures)}; "
                        f"slow-or-unavailable={problems}",
                        file=sys.stderr,
                        flush=True,
                    )
        return sorted(
            results,
            key=lambda row: (row.package, row.requirement or ""),
        )

    @staticmethod
    def _fetch_to_cache(
        package: str,
        requirement: str,
        record: dict[str, object],
        *,
        gateway_url: str,
        timeout_seconds: float,
    ) -> WarmResult:
        relative_path = _tarball_relative_path(
            package,
            str(record.get("url") or ""),
            gateway_url=gateway_url,
        )
        if relative_path is None:
            return WarmResult(
                "npm",
                package,
                _ENVIRONMENT,
                "failed",
                "resolved tarball is outside the public npm Registry path policy",
                requirement=requirement,
            )
        expected = _integrity_digest(record)
        if expected is None:
            return WarmResult(
                "npm",
                package,
                _ENVIRONMENT,
                "failed",
                "npm Registry metadata has no supported integrity or shasum",
                requirement=requirement,
            )
        algorithm, expected_digest = expected
        gateway_artifact = (
            f"{gateway_url.rstrip('/')}/{_GATEWAY_SOURCE}/{relative_path}"
        )
        digest = hashlib.new(algorithm)
        size = 0
        opener = build_opener(ProxyHandler({}))
        try:
            with opener.open(
                Request(
                    gateway_artifact,
                    headers={"X-Artifact-Cache-Prefer-Fallback": "1"},
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
                Request(gateway_artifact, method="HEAD"),
                timeout=timeout_seconds,
            ) as response:
                verification = response.headers.get("X-Artifact-Cache", "")
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            return WarmResult(
                "npm",
                package,
                _ENVIRONMENT,
                "failed",
                f"Gateway npm cache request failed: {type(exc).__name__}",
                requirement=requirement,
                gateway_url=gateway_artifact,
            )
        if not hmac.compare_digest(digest.digest(), expected_digest):
            return WarmResult(
                "npm",
                package,
                _ENVIRONMENT,
                "failed",
                f"Gateway object {algorithm} does not match npm Registry metadata",
                requirement=requirement,
                gateway_url=gateway_artifact,
                first_cache_state=first_state,
                verification_cache_state=verification,
                size=size,
            )
        if verification != "HIT":
            return WarmResult(
                "npm",
                package,
                _ENVIRONMENT,
                "failed",
                f"Gateway verification returned {verification or 'no cache state'}",
                requirement=requirement,
                gateway_url=gateway_artifact,
                first_cache_state=first_state,
                verification_cache_state=verification,
                size=size,
            )
        return WarmResult(
            "npm",
            package,
            _ENVIRONMENT,
            "already-cached" if first_state == "HIT" else "cached",
            "npm tarball is cached, integrity-checked, and verified",
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
        requirements: list[tuple[str, str]] = []
        original: dict[tuple[str, str], dict[str, object]] = {}
        results: list[WarmResult] = []
        for row in rows:
            package = str(row.get("package", ""))
            requirement = str(row.get("requirement") or package)
            version = str(row.get("version") or "")
            resolution_requirement = f"{package}@{version}" if version else requirement
            key = package, resolution_requirement
            requirements.append(key)
            original[key] = row
        try:
            resolved = self._resolve(
                requirements,
                registry=f"{gateway_url.rstrip('/')}/{_GATEWAY_SOURCE}",
                timeout_seconds=max(300.0, timeout_seconds),
                work_dir=work_dir,
            )
        except subprocess.TimeoutExpired:
            return [
                WarmResult(
                    "npm",
                    str(row.get("package", "")),
                    _ENVIRONMENT,
                    "failed",
                    "Gateway npm Registry resolver timed out",
                    requirement=str(
                        row.get("requirement") or row.get("package", "")
                    ),
                    environment_id=str(row.get("environment_id") or "") or None,
                    consumer_environment_ids=tuple(
                        str(item)
                        for item in row.get("consumer_environment_ids", [])
                        if isinstance(
                            row.get("consumer_environment_ids", []),
                            (list, tuple),
                        )
                    ),
                    build_contexts=tuple(
                        str(item)
                        for item in row.get("build_contexts", [])
                        if isinstance(row.get("build_contexts", []), (list, tuple))
                    ),
                )
                for row in rows
            ]
        warmed_artifacts: dict[tuple[str, str, str], WarmResult] = {}
        for package, resolution_requirement in requirements:
            row = original[(package, resolution_requirement)]
            requirement = str(row.get("requirement") or package)
            record = resolved[(package, resolution_requirement)]
            if record.get("state") != "resolved":
                warmed = WarmResult(
                    "npm",
                    package,
                    _ENVIRONMENT,
                    "failed",
                    str(record.get("reason") or "npm Registry fallback resolution failed"),
                    requirement=requirement,
                )
            else:
                artifact_key = (
                    str(record.get("url") or ""),
                    str(record.get("integrity") or ""),
                    str(record.get("shasum") or ""),
                )
                warmed = warmed_artifacts.get(artifact_key)
                if warmed is None:
                    warmed = self._fetch_to_cache(
                        package,
                        requirement,
                        record,
                        gateway_url=gateway_url,
                        timeout_seconds=timeout_seconds,
                    )
                    warmed_artifacts[artifact_key] = warmed
            raw_consumer_ids = row.get("consumer_environment_ids", [])
            raw_build_contexts = row.get("build_contexts", [])
            results.append(
                replace(
                    warmed,
                    requirement=requirement,
                    environment_id=str(row.get("environment_id") or "") or None,
                    consumer_environment_ids=tuple(
                        str(item)
                        for item in raw_consumer_ids
                        if isinstance(raw_consumer_ids, (list, tuple))
                    ),
                    build_contexts=tuple(
                        str(item)
                        for item in raw_build_contexts
                        if isinstance(raw_build_contexts, (list, tuple))
                    ),
                )
            )
        return results

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, Mapping, Sequence
from urllib.parse import urlsplit


_DIGEST = re.compile(r"^[a-z0-9_+.-]+:[A-Za-z0-9=_-]+$")
_NAME_COMPONENT = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
_PROJECT = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
_PROGRESS_PREFIX_ENV = "_ARTIFACT_MIRROR_PROGRESS_PREFIX"
_ACTIVE_PROCESSES: set[subprocess.Popen] = set()
_ACTIVE_PROCESSES_LOCK = threading.Lock()
_OUTPUT_LOCK = threading.Lock()


class MirrorError(RuntimeError):
    """Raised when an image reference or mirror operation is invalid."""


@dataclass(frozen=True)
class Platform:
    os: str
    architecture: str

    @classmethod
    def parse(cls, value: str) -> "Platform":
        operating_system, separator, architecture = value.strip().partition("/")
        if (
            not separator
            or not operating_system
            or not architecture
            or "/" in architecture
            or not re.fullmatch(r"[a-z0-9_-]+", operating_system)
            or not re.fullmatch(r"[a-z0-9_-]+", architecture)
        ):
            raise MirrorError(f"platform must have the form os/architecture: {value!r}")
        return cls(operating_system, architecture)

    def __str__(self) -> str:
        return f"{self.os}/{self.architecture}"


@dataclass(frozen=True)
class ImageReference:
    registry: str
    repository: str
    tag: str | None
    digest: str | None

    @property
    def canonical(self) -> str:
        if self.digest:
            tagged = f":{self.tag}" if self.tag else ""
            suffix = f"{tagged}@{self.digest}"
        else:
            suffix = f":{self.tag or 'latest'}"
        return f"{self.registry}/{self.repository}{suffix}"


def parse_image_reference(value: str) -> ImageReference:
    raw = value.strip()
    if not raw or "$" in raw:
        raise MirrorError(f"image reference is empty or unresolved: {value!r}")

    name, digest_separator, digest = raw.partition("@")
    if digest_separator and (not digest or not _DIGEST.fullmatch(digest)):
        raise MirrorError(f"invalid image digest: {value!r}")

    last_component = name.rsplit("/", 1)[-1]
    tag: str | None = None
    if ":" in last_component:
        name, tag = name.rsplit(":", 1)
        if not tag or not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag):
            raise MirrorError(f"empty image tag: {value!r}")

    first, separator, remainder = name.partition("/")
    if separator and ("." in first or ":" in first or first == "localhost"):
        registry, repository = first.lower(), remainder.lower()
    else:
        registry = "docker.io"
        repository = name.lower()
        if "/" not in repository:
            repository = f"library/{repository}"

    if not repository or any(
        not _NAME_COMPONENT.fullmatch(component) for component in repository.split("/")
    ):
        raise MirrorError(f"invalid image repository: {value!r}")
    if not re.fullmatch(r"[a-z0-9.-]+(?::[0-9]+)?", registry):
        raise MirrorError(f"invalid image registry: {value!r}")
    return ImageReference(
        registry=registry,
        repository=repository,
        tag=tag or (None if digest else "latest"),
        digest=digest or None,
    )


def normalize_registry(value: str) -> str:
    raw = value.strip().rstrip("/")
    if "://" in raw:
        parsed = urlsplit(raw)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise MirrorError(f"registry must be a host[:port] or origin URL: {value!r}")
        raw = parsed.netloc
    if not raw or "/" in raw or not re.fullmatch(r"[A-Za-z0-9.-]+(?::[0-9]+)?", raw):
        raise MirrorError(f"invalid target registry: {value!r}")
    return raw.lower()


def validate_project(value: str) -> str:
    project = value.strip().lower()
    if not _PROJECT.fullmatch(project):
        raise MirrorError(f"invalid Registry Project: {value!r}")
    return project


def target_repository(
    source: ImageReference, target_registry: str, project: str
) -> str:
    source_path = (
        source.repository
        if source.registry == "docker.io"
        else f"{source.registry}/{source.repository}"
    )
    return (
        f"{normalize_registry(target_registry)}/{validate_project(project)}/"
        f"{source_path}"
    )


def standard_target_tag(source: ImageReference) -> str:
    if source.tag:
        return source.tag
    if source.digest:
        algorithm, value = source.digest.split(":", 1)
        return f"{algorithm}-{value[:32]}"
    return "latest"


def parse_source_prefix_map(raw: str | None) -> dict[str, list[str]]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MirrorError("image source prefix map must be valid JSON") from exc
    if not isinstance(value, dict):
        raise MirrorError("image source prefix map must be a JSON object")
    result: dict[str, list[str]] = {}
    for source, replacement in value.items():
        if not isinstance(source, str) or not isinstance(replacement, (str, list)):
            raise MirrorError(
                "image source prefix map values must be strings or ordered string arrays"
            )
        replacements = [replacement] if isinstance(replacement, str) else replacement
        if not replacements or not all(isinstance(item, str) for item in replacements):
            raise MirrorError(
                "image source prefix map replacement arrays must contain strings"
            )
        source_prefix = source.strip().lower().strip("/")
        replacement_prefixes = [
            item.strip().lower().strip("/") for item in replacements
        ]
        if (
            not source_prefix
            or "://" in source_prefix
            or not all(source_prefix.split("/"))
        ):
            raise MirrorError("image source prefix map contains an invalid prefix")
        if any(
            not item or "://" in item or not all(item.split("/"))
            for item in replacement_prefixes
        ):
            raise MirrorError("image source prefix map contains an invalid replacement")
        result[source_prefix] = list(dict.fromkeys(replacement_prefixes))
    return result


def mapped_source_references(
    source: ImageReference, source_prefix_map: Mapping[str, Sequence[str]]
) -> list[str]:
    source_name = f"{source.registry}/{source.repository}"
    selected: tuple[str, Sequence[str]] | None = None
    for prefix, replacements in source_prefix_map.items():
        if source_name == prefix or source_name.startswith(f"{prefix}/"):
            if selected is None or len(prefix) > len(selected[0]):
                selected = prefix, replacements
    names = [source_name]
    if selected:
        prefix, replacements = selected
        names = [f"{replacement}{source_name[len(prefix):]}" for replacement in replacements]
    if source.digest:
        tagged = f":{source.tag}" if source.tag else ""
        suffix = f"{tagged}@{source.digest}"
    else:
        suffix = f":{source.tag or 'latest'}"
    return [f"{name}{suffix}" for name in names]


def mapped_source_reference(
    source: ImageReference, source_prefix_map: Mapping[str, Sequence[str]]
) -> str:
    return mapped_source_references(source, source_prefix_map)[0]


def build_image_plan(
    analysis: Mapping[str, object],
    *,
    registry: str,
    project: str,
    platform: Platform,
    source_prefix_map: Mapping[str, str | Sequence[str]] | None = None,
) -> dict[str, object]:
    raw_images = analysis.get("images")
    if not isinstance(raw_images, list):
        raise MirrorError("dataset analysis has no images list")
    images: list[dict[str, object]] = []
    prefix_map = parse_source_prefix_map(
        json.dumps(dict(source_prefix_map or {}), separators=(",", ":"))
    )
    for row in raw_images:
        if not isinstance(row, dict) or not isinstance(row.get("image"), str):
            raise MirrorError("dataset analysis contains an invalid image row")
        source = parse_image_reference(str(row["image"]))
        repository = target_repository(source, registry, project)
        target_tag = standard_target_tag(source)
        mirror_sources = mapped_source_references(source, prefix_map)
        images.append(
            {
                "source_input": row["image"],
                "source_ref": source.canonical,
                "occurrences": int(row.get("occurrences", 0)),
                "tasks": int(row.get("tasks", 0)),
                "platform": str(platform),
                "mirror_source_ref": mirror_sources[0],
                "mirror_source_refs": mirror_sources,
                "target_repository": repository,
                "target_tag": target_tag,
                "target_tag_ref": f"{repository}:{target_tag}",
            }
        )
    images.sort(key=lambda item: str(item["source_ref"]))
    return {
        "schema_version": 1,
        "kind": "artifact-cache-image-mirror-plan",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "dataset": analysis.get("dataset"),
        "target": {
            "registry": normalize_registry(registry),
            "project": validate_project(project),
            "platform": str(platform),
            "source_prefix_map": prefix_map,
        },
        "images": images,
    }


def atomic_write_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


@contextmanager
def temporary_registry_authfile(
    registry: str, username: str, password: str
) -> Iterator[Path]:
    if not username or not password:
        raise MirrorError("Registry username and password must both be configured")
    encoded = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode(
        "ascii"
    )
    with tempfile.TemporaryDirectory(prefix="artifact-mirror-auth-") as directory:
        path = Path(directory) / "auth.json"
        path.write_text(
            json.dumps(
                {"auths": {normalize_registry(registry): {"auth": encoded}}},
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        path.chmod(0o600)
        yield path


CommandRunner = Callable[[Sequence[str], Mapping[str, str], float], str]


def run_command(
    command: Sequence[str], environment: Mapping[str, str], timeout: float
) -> str:
    subprocess_environment = dict(environment)
    progress_prefix = subprocess_environment.pop(_PROGRESS_PREFIX_ENV, "")
    process = subprocess.Popen(
        list(command),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=subprocess_environment,
    )
    with _ACTIVE_PROCESSES_LOCK:
        _ACTIVE_PROCESSES.add(process)
    stdout: list[str] = []
    stderr: list[str] = []
    echo_subprocess_output = "copy" in command

    def drain(stream, chunks: list[str], *, echo: bool) -> None:  # noqa: ANN001
        for line in iter(stream.readline, ""):
            chunks.append(line)
            if echo:
                with _OUTPUT_LOCK:
                    if progress_prefix:
                        print(
                            f"{progress_prefix} {line}",
                            file=sys.stderr,
                            end="" if line.endswith("\n") else "\n",
                            flush=True,
                        )
                    else:
                        print(line, file=sys.stderr, end="", flush=True)
        stream.close()

    assert process.stdout is not None and process.stderr is not None
    stdout_thread = threading.Thread(
        target=drain,
        args=(process.stdout, stdout),
        kwargs={"echo": echo_subprocess_output},
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=drain,
        args=(process.stderr, stderr),
        kwargs={"echo": echo_subprocess_output},
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    try:
        return_code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait()
        raise MirrorError(f"skopeo timed out after {timeout:g} seconds") from exc
    except BaseException:
        process.terminate()
        try:
            process.wait(timeout=10.0)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise
    finally:
        stdout_thread.join()
        stderr_thread.join()
        with _ACTIVE_PROCESSES_LOCK:
            _ACTIVE_PROCESSES.discard(process)
    if return_code:
        detail = "".join(stderr).strip()[-1000:] or "<no stderr>"
        raise MirrorError(f"skopeo failed with exit {return_code}: {detail}")
    return "".join(stdout)


def terminate_active_commands() -> None:
    """Terminate skopeo children when the coordinating thread is interrupted."""
    with _ACTIVE_PROCESSES_LOCK:
        processes = list(_ACTIVE_PROCESSES)
    for process in processes:
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
    deadline = time.monotonic() + 10.0
    for process in processes:
        if process.poll() is not None:
            continue
        try:
            process.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            process.wait()


def mirror_environment(
    target_registry: str, upstream_proxy: str | None
) -> dict[str, str]:
    environment = os.environ.copy()
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        environment.pop(name, None)
    if upstream_proxy:
        parsed = urlsplit(upstream_proxy)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise MirrorError("upstream proxy must be an absolute HTTP(S) URL")
        environment["HTTP_PROXY"] = upstream_proxy
        environment["HTTPS_PROXY"] = upstream_proxy
        environment["http_proxy"] = upstream_proxy
        environment["https_proxy"] = upstream_proxy
    no_proxy = [
        item.strip()
        for item in environment.get("NO_PROXY", "").split(",")
        if item.strip()
    ]
    host = normalize_registry(target_registry).split(":", 1)[0]
    if host not in no_proxy:
        no_proxy.append(host)
    environment["NO_PROXY"] = ",".join(no_proxy)
    environment["no_proxy"] = environment["NO_PROXY"]
    return environment


def _auth_options(authfile: Path | None, *, source: bool, destination: bool) -> list[str]:
    if authfile is None:
        return []
    options: list[str] = []
    if source:
        options.extend(["--src-authfile", str(authfile)])
    if destination:
        options.extend(["--dest-authfile", str(authfile)])
    return options


def _inspect_digest(
    image_ref: str,
    *,
    platform: Platform,
    authfile: Path | None,
    tls_verify: bool | None,
    environment: Mapping[str, str],
    command_runner: CommandRunner,
) -> str:
    command = [
        "skopeo",
        "--override-os",
        platform.os,
        "--override-arch",
        platform.architecture,
        "inspect",
        "--format",
        "{{.Digest}}",
    ]
    if authfile is not None:
        command.extend(["--authfile", str(authfile)])
    if tls_verify is not None:
        command.append(f"--tls-verify={'true' if tls_verify else 'false'}")
    command.append(f"docker://{image_ref}")
    digest = command_runner(command, environment, 300.0).strip()
    if not _DIGEST.fullmatch(digest):
        raise MirrorError(f"skopeo returned an invalid digest for {image_ref!r}")
    return digest


def _is_missing_manifest(error: MirrorError) -> bool:
    message = str(error).lower()
    return bool(
        re.search(r"unknown:\s+(?:repository|artifact)\s+.+\s+not found", message)
    ) or any(
        marker in message
        for marker in (
            "manifest unknown",
            "name unknown",
            "repository not found",
            "repository does not exist",
            "status code: 404",
            "status code 404",
            "status 404",
        )
    )


def _inspect_digest_optional(
    image_ref: str,
    *,
    platform: Platform,
    authfile: Path | None,
    tls_verify: bool,
    environment: Mapping[str, str],
    command_runner: CommandRunner,
) -> str | None:
    try:
        return _inspect_digest(
            image_ref,
            platform=platform,
            authfile=authfile,
            tls_verify=tls_verify,
            environment=environment,
            command_runner=command_runner,
        )
    except MirrorError as exc:
        if _is_missing_manifest(exc):
            return None
        raise


def mirror_images(
    plan: Mapping[str, object],
    *,
    output_path: Path,
    selection_path: Path | None = None,
    authfile: Path | None = None,
    target_tls_verify: bool = True,
    upstream_proxy: str | None = None,
    direct_upstream_with_proxy: bool = False,
    concurrency: int = 1,
    command_runner: CommandRunner = run_command,
) -> dict[str, object]:
    target = plan.get("target")
    raw_images = plan.get("images")
    if not isinstance(target, dict) or not isinstance(raw_images, list):
        raise MirrorError("invalid image mirror plan")
    registry = normalize_registry(str(target.get("registry", "")))
    project = validate_project(str(target.get("project", "")))
    platform = Platform.parse(str(target.get("platform", "")))
    configured_prefix_map = target.get("source_prefix_map", {})
    if not isinstance(configured_prefix_map, dict):
        raise MirrorError("image mirror plan source_prefix_map must be an object")
    source_prefix_map = parse_source_prefix_map(
        json.dumps(configured_prefix_map, separators=(",", ":"))
    )
    if authfile is not None and not authfile.is_file():
        raise MirrorError(f"authfile does not exist: {authfile}")
    if command_runner is run_command and shutil.which("skopeo") is None:
        raise MirrorError("skopeo is required to execute an image mirror plan")
    if (
        isinstance(concurrency, bool)
        or not isinstance(concurrency, int)
        or concurrency < 1
        or concurrency > 32
    ):
        raise MirrorError("concurrency must be an integer between 1 and 32")
    if direct_upstream_with_proxy and not upstream_proxy:
        raise MirrorError(
            "direct upstream fallback requires an explicit upstream proxy"
        )

    source_environment = mirror_environment(
        registry, None if direct_upstream_with_proxy else upstream_proxy
    )
    direct_source_environment = mirror_environment(registry, upstream_proxy)
    target_environment = mirror_environment(registry, None)
    existing: list[dict[str, object]] = []
    missing: list[dict[str, object]] = []

    print(
        f"[image-mirror] checking {len(raw_images)} target tag(s) in "
        f"{registry}/{project}",
        file=sys.stderr,
        flush=True,
    )
    for raw in raw_images:
        if not isinstance(raw, dict):
            raise MirrorError("invalid image row in mirror plan")
        source = parse_image_reference(str(raw.get("source_ref", "")))
        expected_repository = target_repository(source, registry, project)
        expected_tag = standard_target_tag(source)
        expected_target = f"{expected_repository}:{expected_tag}"
        expected_mirror_sources = mapped_source_references(source, source_prefix_map)
        if raw.get("target_repository") != expected_repository:
            raise MirrorError(
                "target repository does not match the configured registry/project: "
                f"{raw.get('target_repository')!r}"
            )
        if raw.get("target_tag") != expected_tag or raw.get("target_tag_ref") != expected_target:
            raise MirrorError(
                f"target tag does not preserve the source tag: {raw.get('target_tag_ref')!r}"
            )
        configured_mirror_sources = raw.get("mirror_source_refs")
        if configured_mirror_sources is None:
            configured_mirror_sources = [raw.get("mirror_source_ref")]
        if configured_mirror_sources != expected_mirror_sources:
            raise MirrorError(
                "mirror sources do not match source_prefix_map: "
                f"{configured_mirror_sources!r}"
            )
        if raw.get("mirror_source_ref") != expected_mirror_sources[0]:
            raise MirrorError("primary mirror source does not match ordered sources")
        target_digest = _inspect_digest_optional(
            expected_target,
            platform=platform,
            authfile=authfile,
            tls_verify=target_tls_verify,
            environment=target_environment,
            command_runner=command_runner,
        )
        if target_digest:
            existing.append(
                {
                    **raw,
                    "target_digest": target_digest,
                    "target_digest_ref": f"{expected_repository}@{target_digest}",
                    "status": "already-present",
                }
            )
        else:
            missing.append(dict(raw))

    print(
        f"[image-mirror] target filter complete: existing={len(existing)} "
        f"missing={len(missing)} concurrency={concurrency}",
        file=sys.stderr,
        flush=True,
    )

    selection: dict[str, object] = {
        "schema_version": 1,
        "kind": "artifact-cache-image-mirror-selection",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "target": target,
        "concurrency": concurrency,
        "direct_upstream_with_proxy": direct_upstream_with_proxy,
        "existing": existing,
        "missing": missing,
    }
    if selection_path is not None:
        atomic_write_json(selection_path, selection)

    result: dict[str, object] = {
        "schema_version": 1,
        "kind": "artifact-cache-image-mirror-result",
        "plan_sha256": hashlib.sha256(
            json.dumps(plan, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
        "target": target,
        "concurrency": concurrency,
        "direct_upstream_with_proxy": direct_upstream_with_proxy,
        "images": list(existing),
    }
    results = result["images"]
    assert isinstance(results, list)
    atomic_write_json(output_path, result)

    def copy_one(index: int, raw: Mapping[str, object]) -> dict[str, object]:
        prefix = f"[image-mirror] [{index}/{len(missing)}]"
        original_source = parse_image_reference(str(raw.get("source_ref", "")))
        raw_mirror_sources = raw.get("mirror_source_refs")
        mirror_sources = (
            [str(item) for item in raw_mirror_sources]
            if isinstance(raw_mirror_sources, list)
            else [str(raw["mirror_source_ref"])]
        )
        attempts = [(item, source_environment, "mirror") for item in mirror_sources]
        if direct_upstream_with_proxy:
            attempts.append(
                (original_source.canonical, direct_source_environment, "original-proxy")
            )
        repository = str(raw.get("target_repository", ""))
        destination = str(raw["target_tag_ref"])
        failures: list[str] = []
        selected_source = ""
        selected_mode = ""
        source_digest = ""
        for attempt, (candidate, environment, mode) in enumerate(attempts, start=1):
            print(
                f"{prefix} source {attempt}/{len(attempts)} {candidate}",
                file=sys.stderr,
                flush=True,
            )
            try:
                candidate_digest = _inspect_digest(
                    candidate,
                    platform=platform,
                    authfile=authfile,
                    tls_verify=None,
                    environment=environment,
                    command_runner=command_runner,
                )
                command = [
                    "skopeo",
                    "--override-os",
                    platform.os,
                    "--override-arch",
                    platform.architecture,
                    "copy",
                    *_auth_options(authfile, source=True, destination=True),
                    f"--dest-tls-verify={'true' if target_tls_verify else 'false'}",
                    f"docker://{candidate}",
                    f"docker://{destination}",
                ]
                copy_environment = dict(environment)
                copy_environment[_PROGRESS_PREFIX_ENV] = prefix
                command_runner(command, copy_environment, 7200.0)
            except MirrorError as exc:
                failures.append(f"{candidate}: {exc}")
                print(
                    f"{prefix} source failed; trying next candidate",
                    file=sys.stderr,
                    flush=True,
                )
                continue
            source_digest = candidate_digest
            selected_source = candidate
            selected_mode = mode
            break
        if not selected_source:
            raise MirrorError(
                f"all {len(attempts)} source candidates failed: " + " | ".join(failures)
            )
        target_digest = _inspect_digest(
            destination,
            platform=platform,
            authfile=authfile,
            tls_verify=target_tls_verify,
            environment=target_environment,
            command_runner=command_runner,
        )
        print(
            f"{prefix} uploaded {destination}",
            file=sys.stderr,
            flush=True,
        )
        return {
            **raw,
            "transfer_source_ref": selected_source,
            "transfer_source_mode": selected_mode,
            "source_digest": source_digest,
            "target_digest": target_digest,
            "target_digest_ref": f"{repository}@{target_digest}",
            "status": "uploaded",
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }

    failures: list[dict[str, object]] = []
    executor = ThreadPoolExecutor(
        max_workers=min(concurrency, max(1, len(missing))),
        thread_name_prefix="image-mirror",
    )
    futures = {
        executor.submit(copy_one, index, raw): (index, raw)
        for index, raw in enumerate(missing, start=1)
    }
    try:
        for future in as_completed(futures):
            _index, raw = futures[future]
            try:
                row = future.result()
            except (MirrorError, OSError, subprocess.SubprocessError) as exc:
                row = {
                    **raw,
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                }
                failures.append(row)
            results.append(row)
            results.sort(key=lambda item: str(item.get("source_ref", "")))
            atomic_write_json(output_path, result)
    except BaseException:
        for future in futures:
            future.cancel()
        if command_runner is run_command:
            terminate_active_commands()
        executor.shutdown(wait=True, cancel_futures=True)
        atomic_write_json(output_path, result)
        raise
    else:
        executor.shutdown(wait=True)

    result["completed_at"] = datetime.now(timezone.utc).isoformat()
    atomic_write_json(output_path, result)
    if failures:
        failed_refs = ", ".join(str(row.get("source_ref")) for row in failures)
        raise MirrorError(
            f"{len(failures)} image mirror operation(s) failed; see {output_path}: "
            f"{failed_refs}"
        )
    return result

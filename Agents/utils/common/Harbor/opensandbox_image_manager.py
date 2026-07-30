#!/usr/bin/env python3
"""Build and publish content-addressed task images for YiCloud OpenSandbox.

Flow:

    Harbor selects one local task
                  |
                  v
    Hash environment files, source policy,
    target platform, and explicit build args
                  |
                  v
    tag = <prefix>-<task>-<content-hash>
                  |
                  v
    Acquire lock for <registry>/<repository>:<tag>
                  |
                  v
         +--------------------------+
         | Registry manifest exists?|
         +-------------+------------+
                       |
              +--------+--------+
              | yes             | no
              v                 v
       Reuse manifest    Rewrite source mirrors
                              |
                              v
                         Build OCI archive
                              |
                              v
                     Upload blobs + schema2
                            manifest
                              |
              +---------------+
              v
    Return <sandbox-image-prefix>:<tag>
                  |
                  v
    CreateSandboxReq.Image.Ref selects this image

The Registry repository is shared. The deterministic tag is the lookup key;
Registry manifest and layer digests remain the underlying content addresses.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 is still used by some H-side tools.
    tomllib = None  # type: ignore[assignment]


DOCKER_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
DOCKER_CONFIG = "application/vnd.docker.container.image.v1+json"
DOCKER_LAYER_GZIP = "application/vnd.docker.image.rootfs.diff.tar.gzip"
OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
OCI_LAYER_GZIP = "application/vnd.oci.image.layer.v1.tar+gzip"
MANAGER_FORMAT_VERSION = "opensandbox-image-v1"
CONTENT_HASH_IGNORE_NAMES = {"__pycache__", ".DS_Store", ".git"}
BUILD_ARG_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
FROM_LINE = re.compile(
    r"^(?P<prefix>\s*FROM(?:\s+--platform=\S+)?\s+)"
    r"(?P<image>\S+)(?P<suffix>.*)$",
    re.IGNORECASE,
)
AS_ALIAS = re.compile(r"\s+AS\s+(?P<alias>[A-Za-z0-9_.-]+)\s*$", re.IGNORECASE)


def log(message: str) -> None:
    print(f"[opensandbox-image] {message}", file=sys.stderr, flush=True)


def digest_bytes(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def environment_content_hash(environment_dir: Path, truncate: int = 32) -> str:
    """Mirror Harbor 0.18's stable environment content identity."""
    candidates: list[tuple[str, Path]] = []
    for path in environment_dir.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(environment_dir)
        if CONTENT_HASH_IGNORE_NAMES & set(relative.parts):
            continue
        candidates.append((relative.as_posix(), path))

    if not candidates:
        return hashlib.sha256(environment_dir.name.encode("utf-8")).hexdigest()[
            :truncate
        ]

    digest = hashlib.sha256()
    for relative, path in sorted(candidates, key=lambda item: item[0]):
        relative_bytes = relative.encode("utf-8")
        data = path.read_bytes()
        digest.update(len(relative_bytes).to_bytes(4, "big"))
        digest.update(relative_bytes)
        digest.update(len(data).to_bytes(4, "big"))
        digest.update(data)
    return digest.hexdigest()[:truncate]


def safe_tag_component(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-").lower()
    return normalized or "task"


def resolve_task_dir(
    task_dir: Path | None, dataset_root: Path | None, include: str
) -> Path:
    if task_dir is not None:
        resolved = task_dir.resolve()
    elif dataset_root is not None:
        root = dataset_root.resolve()
        if (root / "task.toml").is_file():
            resolved = root
        else:
            task_names = [item.strip() for item in include.split(",") if item.strip()]
            if len(task_names) != 1:
                raise ValueError(
                    "automatic OpenSandbox image preparation requires exactly one "
                    "included task"
                )
            resolved = (root / task_names[0]).resolve()
    else:
        raise ValueError("provide --task-dir or --dataset-root")

    if not (resolved / "task.toml").is_file():
        raise ValueError(f"task.toml not found under {resolved}")
    return resolved


def load_build_timeout(task_dir: Path) -> float:
    task_config_path = task_dir / "task.toml"
    if tomllib is not None:
        with task_config_path.open("rb") as handle:
            task_config = tomllib.load(handle)
        value = (task_config.get("environment") or {}).get("build_timeout_sec", 600)
    else:
        value = 600
        section = ""
        for raw_line in task_config_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].strip()
            elif section == "environment" and line.startswith("build_timeout_sec"):
                _, raw_value = line.split("=", 1)
                value = raw_value.strip()
                break
    timeout = float(value)
    if timeout <= 0:
        raise ValueError(f"invalid environment.build_timeout_sec: {value!r}")
    return timeout


def validate_single_container_task(task_dir: Path) -> Path:
    environment_dir = task_dir / "environment"
    dockerfile = environment_dir / "Dockerfile"
    compose_paths = (
        environment_dir / "docker-compose.yaml",
        environment_dir / "docker-compose.yml",
    )
    if any(path.exists() for path in compose_paths):
        raise ValueError(
            f"OpenSandbox image preparation does not support compose task {task_dir.name}"
        )
    if not dockerfile.is_file():
        raise ValueError(f"Dockerfile not found under {environment_dir}")
    return environment_dir


@dataclass(frozen=True)
class SourcePolicy:
    dockerhub_mirror_prefix: str
    apt_mirror: str

    @property
    def identity(self) -> str:
        payload = json.dumps(
            {
                "apt_mirror": self.apt_mirror,
                "dockerhub_mirror_prefix": self.dockerhub_mirror_prefix,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def mirror_image_ref(image: str, mirror_prefix: str, aliases: set[str]) -> str:
    if not mirror_prefix or image in aliases or image.startswith("$"):
        return image
    if image.startswith("docker.io/"):
        return f"{mirror_prefix.rstrip('/')}/{image.removeprefix('docker.io/')}"
    first = image.split("/", 1)[0]
    if "/" not in image or ("." not in first and ":" not in first and first != "localhost"):
        relative = image if "/" in image else f"library/{image}"
        return f"{mirror_prefix.rstrip('/')}/{relative}"
    return image


def apt_mirror_command(source_image: str, apt_mirror: str) -> str | None:
    if not apt_mirror:
        return None
    image = source_image.lower()
    mirror = apt_mirror.rstrip("/")
    if "ubuntu" in image:
        replacements = (
            f"s#https?://(archive|security|ports)\\.ubuntu\\.com/ubuntu/?#"
            f"{mirror}/ubuntu/#g"
        )
    elif any(name in image for name in ("bookworm", "debian", "python:", "golang:")):
        replacements = (
            f"s#https?://deb\\.debian\\.org/debian/?#{mirror}/debian/#g;"
            f"s#https?://security\\.debian\\.org/debian-security/?#"
            f"{mirror}/debian-security/#g"
        )
    else:
        return None
    return (
        "RUN set -eu; "
        "for file in /etc/apt/sources.list /etc/apt/sources.list.d/*.list "
        "/etc/apt/sources.list.d/*.sources; do "
        "[ -f \"$file\" ] || continue; "
        f"sed -E -i '{replacements}' \"$file\"; "
        "done"
    )


def render_build_dockerfile(source: str, policy: SourcePolicy) -> str:
    output: list[str] = []
    aliases: set[str] = set()
    for line in source.splitlines():
        match = FROM_LINE.match(line)
        if not match:
            output.append(line)
            continue

        source_image = match.group("image")
        mirrored_image = mirror_image_ref(
            source_image, policy.dockerhub_mirror_prefix, aliases
        )
        output.append(
            f"{match.group('prefix')}{mirrored_image}{match.group('suffix')}"
        )
        alias_match = AS_ALIAS.search(match.group("suffix"))
        if alias_match:
            aliases.add(alias_match.group("alias"))
        if source_image not in aliases:
            command = apt_mirror_command(source_image, policy.apt_mirror)
            if command:
                output.append(command)
    return "\n".join(output) + "\n"


def parse_build_args(raw: str) -> dict[str, str]:
    if not raw.strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise TypeError("build args must be a JSON object")
    result: dict[str, str] = {}
    for key, value in data.items():
        if not isinstance(key, str) or not BUILD_ARG_NAME.fullmatch(key):
            raise ValueError(f"invalid build arg name: {key!r}")
        if not isinstance(value, (str, int, float, bool)):
            raise TypeError(f"invalid build arg value for {key!r}")
        result[key] = str(value).lower() if isinstance(value, bool) else str(value)
    return result


def proxy_build_args(enabled: bool) -> dict[str, str]:
    if not enabled:
        return {}
    proxy_names = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
    result = {name: os.environ[name] for name in proxy_names if os.environ.get(name)}
    if not result:
        raise ValueError("--use-proxy requires an HTTP_PROXY/HTTPS_PROXY environment variable")
    for name, value in result.items():
        if urlparse(value).hostname in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError(
                f"--use-proxy cannot pass loopback proxy {name} into BuildKit; "
                "use a proxy address reachable from build containers or disable "
                "proxy forwarding"
            )
    for name in ("NO_PROXY", "no_proxy"):
        if os.environ.get(name):
            result[name] = os.environ[name]
    return result


def run_build(
    *,
    environment_dir: Path,
    dockerfile: Path,
    archive_path: Path,
    log_path: Path,
    platform: str,
    timeout_sec: float,
    build_args: dict[str, str],
) -> None:
    child_env = os.environ.copy()
    child_env.update(build_args)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def execute(command: list[str], log_handle, operation: str, timeout: float) -> None:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            env=child_env,
            start_new_session=True,
        )
        try:
            return_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise RuntimeError(
                f"timed out {operation} after {timeout:g}s; see {log_path}"
            ) from exc
        if return_code != 0:
            raise RuntimeError(
                f"{operation} failed with exit code {return_code}; see {log_path}"
            )

    buildx_available = subprocess.run(
        ["docker", "buildx", "version"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0
    if not buildx_available:
        raise RuntimeError(
            "docker buildx is required to build benchmark task images, but the "
            "plugin is unavailable or broken; install Docker Buildx and verify "
            "`docker buildx version` before retrying"
        )

    with log_path.open("w", encoding="utf-8") as log_handle:
        command = [
            "docker",
            "buildx",
            "build",
            f"--file={dockerfile}",
            f"--platform={platform}",
            f"--output=type=oci,dest={archive_path},compression=gzip,force-compression=true",
            "--provenance=false",
            "--progress=plain",
        ]
        for name in sorted(build_args):
            command.extend(("--build-arg", name))
        command.append(str(environment_dir))
        execute(command, log_handle, "building task image", timeout_sec)


def docker_credentials(config_path: Path, registry: str) -> tuple[str, str]:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    auths = config.get("auths") or {}
    candidates = (registry, f"https://{registry}", f"http://{registry}")
    entry = next((auths[key] for key in candidates if key in auths), None)
    if not entry or not entry.get("auth"):
        raise RuntimeError(
            f"no inline Docker login found for {registry!r} in {config_path}; "
            "run docker login or configure an explicit supported credential source"
        )
    decoded = base64.b64decode(entry["auth"]).decode("utf-8")
    if ":" not in decoded:
        raise RuntimeError("Docker auth entry has an invalid format")
    username, password = decoded.split(":", 1)
    return username, password


def request_ok(response: requests.Response, expected: set[int], operation: str) -> None:
    if response.status_code not in expected:
        body = response.text[:500].replace("\n", " ")
        raise RuntimeError(
            f"{operation} failed with HTTP {response.status_code}: {body}"
        )


class RegistryClient:
    def __init__(
        self,
        registry: str,
        repository: str,
        username: str,
        password: str,
    ) -> None:
        self.registry = registry
        self.repository = repository
        self.base_url = f"https://{registry}"
        self.session = requests.Session()
        self.headers = {"Authorization": f"Bearer {self._token(username, password)}"}

    def _token(self, username: str, password: str) -> str:
        probe = self.session.head(
            f"{self.base_url}/v2/{self.repository}/manifests/__auth_probe__",
            timeout=30,
            allow_redirects=False,
        )
        challenge = probe.headers.get("WWW-Authenticate", "")
        if not challenge.lower().startswith("bearer "):
            raise RuntimeError(
                "registry did not return a Bearer challenge "
                f"(HTTP {probe.status_code})"
            )
        params = requests.utils.parse_dict_header(challenge[len("Bearer ") :])
        realm = params.get("realm")
        if not realm:
            raise RuntimeError("registry Bearer challenge has no realm")
        token_params = {
            "scope": f"repository:{self.repository}:pull,push",
            "account": username,
        }
        if params.get("service"):
            token_params["service"] = params["service"]
        response = self.session.get(
            realm,
            params=token_params,
            auth=(username, password),
            timeout=30,
        )
        response.raise_for_status()
        data = response.json()
        token = data.get("token") or data.get("access_token")
        if not token:
            raise RuntimeError("registry token response contained no token")
        return str(token)

    def manifest_exists(self, tag: str) -> bool:
        response = self.session.head(
            f"{self.base_url}/v2/{self.repository}/manifests/{tag}",
            headers={**self.headers, "Accept": DOCKER_MANIFEST},
            timeout=30,
            allow_redirects=False,
        )
        if response.status_code in {200, 307}:
            return True
        if response.status_code == 404:
            return False
        request_ok(response, {200, 404}, f"checking manifest {tag}")
        return False

    def _blob_exists(self, digest: str) -> bool:
        response = self.session.head(
            f"{self.base_url}/v2/{self.repository}/blobs/{digest}",
            headers=self.headers,
            timeout=30,
            allow_redirects=False,
        )
        if response.status_code in {200, 307}:
            return True
        if response.status_code == 404:
            return False
        request_ok(response, {200, 404}, f"checking blob {digest}")
        return False

    def ensure_blob(
        self,
        archive: tarfile.TarFile,
        descriptor: dict[str, object],
        spool_dir: Path,
    ) -> str:
        digest = str(descriptor["digest"])
        if self._blob_exists(digest):
            return "present"
        member = archive.extractfile(blob_member_name(digest))
        if member is None:
            raise RuntimeError(f"OCI archive is missing blob {digest}")
        spool_path = spool_dir / digest.replace(":", "-")
        hasher = hashlib.sha256()
        size = 0
        with spool_path.open("wb") as output:
            while chunk := member.read(1024 * 1024):
                hasher.update(chunk)
                size += len(chunk)
                output.write(chunk)
        actual = f"sha256:{hasher.hexdigest()}"
        if actual != digest:
            raise RuntimeError(f"blob digest mismatch: expected {digest}, got {actual}")
        expected_size = int(descriptor.get("size") or 0)
        if expected_size and expected_size != size:
            raise RuntimeError(
                f"blob size mismatch for {digest}: expected {expected_size}, got {size}"
            )

        start = self.session.post(
            f"{self.base_url}/v2/{self.repository}/blobs/uploads/",
            headers=self.headers,
            timeout=30,
        )
        request_ok(start, {202}, f"starting upload for {digest}")
        location = start.headers.get("Location")
        if not location:
            raise RuntimeError(f"registry returned no upload location for {digest}")
        try:
            with spool_path.open("rb") as data:
                response = self.session.put(
                    urljoin(self.base_url, location),
                    params={"digest": digest},
                    data=data,
                    headers={
                        **self.headers,
                        "Content-Type": "application/octet-stream",
                        "Content-Length": str(size),
                    },
                    timeout=(30, max(120, min(1800, size // (1024 * 1024) * 10))),
                )
            request_ok(response, {201}, f"uploading blob {digest}")
        finally:
            spool_path.unlink(missing_ok=True)
        return "uploaded"

    def publish_archive(self, archive_path: Path, tag: str, spool_dir: Path) -> dict:
        with tarfile.open(archive_path, "r") as archive:
            manifest, descriptors = schema2_manifest(archive)
            statuses = {
                str(item["digest"]): self.ensure_blob(archive, item, spool_dir)
                for item in descriptors
            }
        manifest_bytes = json.dumps(
            manifest, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        response = self.session.put(
            f"{self.base_url}/v2/{self.repository}/manifests/{tag}",
            data=manifest_bytes,
            headers={**self.headers, "Content-Type": DOCKER_MANIFEST},
            timeout=60,
        )
        request_ok(response, {201}, f"uploading manifest {tag}")
        return {
            "blob_status": statuses,
            "manifest_digest": response.headers.get("Docker-Content-Digest")
            or digest_bytes(manifest_bytes),
            "manifest_media_type": DOCKER_MANIFEST,
        }


def blob_member_name(digest: str) -> str:
    algorithm, value = digest.split(":", 1)
    if algorithm != "sha256":
        raise RuntimeError(f"unsupported digest algorithm: {algorithm}")
    return f"blobs/sha256/{value}"


def read_member_bytes(
    archive: tarfile.TarFile, name: str, *, max_bytes: int = 16 * 1024 * 1024
) -> bytes:
    member_info = archive.getmember(name)
    if member_info.size > max_bytes:
        raise RuntimeError(f"OCI metadata member is unexpectedly large: {name}")
    member = archive.extractfile(member_info)
    if member is None:
        raise RuntimeError(f"OCI archive is missing {name}")
    return member.read()


def schema2_manifest(
    archive: tarfile.TarFile,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    index = json.loads(read_member_bytes(archive, "index.json"))
    source_descriptors = index.get("manifests") or []
    if len(source_descriptors) != 1:
        raise RuntimeError(
            "expected exactly one platform manifest, "
            f"found {len(source_descriptors)}"
        )
    source_descriptor = source_descriptors[0]
    source_bytes = read_member_bytes(
        archive, blob_member_name(str(source_descriptor["digest"]))
    )
    if digest_bytes(source_bytes) != source_descriptor["digest"]:
        raise RuntimeError("OCI source manifest digest mismatch")
    source_manifest = json.loads(source_bytes)

    config = dict(source_manifest["config"])
    if config.get("mediaType") not in {OCI_CONFIG, DOCKER_CONFIG}:
        raise RuntimeError(f"unsupported config media type: {config.get('mediaType')}")
    config["mediaType"] = DOCKER_CONFIG
    layers: list[dict[str, object]] = []
    for source_layer in source_manifest.get("layers") or []:
        layer = dict(source_layer)
        if layer.get("mediaType") not in {OCI_LAYER_GZIP, DOCKER_LAYER_GZIP}:
            raise RuntimeError(
                f"cannot map layer media type to Docker schema2: {layer.get('mediaType')}"
            )
        layer["mediaType"] = DOCKER_LAYER_GZIP
        layers.append(layer)
    return (
        {
            "schemaVersion": 2,
            "mediaType": DOCKER_MANIFEST,
            "config": config,
            "layers": layers,
        },
        [config, *layers],
    )


def atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def image_identity(
    environment_hash: str,
    policy: SourcePolicy,
    platform: str,
    build_args: dict[str, str],
) -> str:
    payload = "\0".join(
        (
            MANAGER_FORMAT_VERSION,
            environment_hash,
            policy.identity,
            platform,
            json.dumps(build_args, sort_keys=True, separators=(",", ":")),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def build_tag(prefix: str, task_name: str, identity: str, force: bool) -> str:
    base = f"{safe_tag_component(prefix)}-{safe_tag_component(task_name)}-{identity[:20]}"
    if force:
        suffix = datetime.now(timezone.utc).strftime("r%Y%m%d%H%M%S") + f"-{os.getpid()}"
        base = f"{base}-{suffix}"
    return base[:128].rstrip(".-")


def prepare(args: argparse.Namespace) -> str:
    task_dir = resolve_task_dir(args.task_dir, args.dataset_root, args.include)
    environment_dir = validate_single_container_task(task_dir)
    policy = SourcePolicy(args.dockerhub_mirror_prefix, args.apt_mirror)
    explicit_build_args = parse_build_args(args.build_args_json)
    environment_hash = environment_content_hash(environment_dir)
    identity = image_identity(
        environment_hash, policy, args.platform, explicit_build_args
    )
    tag = build_tag(args.tag_prefix, task_dir.name, identity, args.force)
    image_ref = f"{args.sandbox_image_prefix}:{tag}"
    registry_ref = f"{args.registry}/{args.repository}:{tag}"

    if args.dry_run:
        return image_ref

    cache_root = args.cache_root.resolve()
    target_key = hashlib.sha256(
        f"{args.registry}/{args.repository}".encode()
    ).hexdigest()[:16]
    lock_path = cache_root / "locks" / f"{target_key}-{tag}.lock"
    record_path = cache_root / "records" / target_key / f"{tag}.json"
    log_path = cache_root / "logs" / f"{tag}.log"
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    username, password = docker_credentials(args.docker_config, args.registry)
    with lock_path.open("a+") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        registry = RegistryClient(
            args.registry, args.repository, username, password
        )
        if not args.force and registry.manifest_exists(tag):
            log(f"registry cache hit: {image_ref}")
            existing_record: dict = {}
            if record_path.is_file():
                try:
                    existing_record = json.loads(record_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    existing_record = {}
            atomic_write_json(
                record_path,
                {
                    **existing_record,
                    "environment_hash": environment_hash,
                    "image_identity": identity,
                    "image_ref": image_ref,
                    "manager_format": MANAGER_FORMAT_VERSION,
                    "platform": args.platform,
                    "registry_ref": registry_ref,
                    "source": existing_record.get("source", "registry-cache"),
                    "last_resolution": "registry-cache",
                    "source_policy": {
                        "apt_mirror": policy.apt_mirror,
                        "dockerhub_mirror_prefix": policy.dockerhub_mirror_prefix,
                    },
                    "build_arg_names": sorted(explicit_build_args),
                    "task_dir": str(task_dir),
                    "last_resolved_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            return image_ref

        cache_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=f"{tag}-", dir=cache_root
        ) as temporary_dir:
            temporary = Path(temporary_dir)
            rendered_dockerfile = temporary / "Dockerfile"
            rendered_dockerfile.write_text(
                render_build_dockerfile(
                    (environment_dir / "Dockerfile").read_text(encoding="utf-8"),
                    policy,
                ),
                encoding="utf-8",
            )
            archive_path = temporary / "image.oci.tar"
            proxy_args = proxy_build_args(args.use_proxy)
            build_args = {**proxy_args, **explicit_build_args}
            log(f"building {task_dir.name} for {args.platform}; log={log_path}")
            run_build(
                environment_dir=environment_dir,
                dockerfile=rendered_dockerfile,
                archive_path=archive_path,
                log_path=log_path,
                platform=args.platform,
                timeout_sec=load_build_timeout(task_dir),
                build_args=build_args,
            )
            log(f"publishing {registry_ref}")
            published = registry.publish_archive(archive_path, tag, temporary)

        atomic_write_json(
            record_path,
            {
                "build_log": str(log_path),
                "environment_hash": environment_hash,
                "image_identity": identity,
                "image_ref": image_ref,
                "manager_format": MANAGER_FORMAT_VERSION,
                "manifest_digest": published["manifest_digest"],
                "manifest_media_type": published["manifest_media_type"],
                "platform": args.platform,
                "proxy_configured": bool(proxy_args),
                "registry_ref": registry_ref,
                "source": "built-and-pushed",
                "last_resolution": "built-and-pushed",
                "source_policy": {
                    "apt_mirror": policy.apt_mirror,
                    "dockerhub_mirror_prefix": policy.dockerhub_mirror_prefix,
                },
                "build_arg_names": sorted(explicit_build_args),
                "task_dir": str(task_dir),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        log(f"ready: {image_ref}")
        return image_ref


def default_path(env_name: str, fallback: Path) -> Path:
    value = os.environ.get(env_name, "").strip()
    return Path(value).expanduser() if value else fallback


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prebuild one content-addressed YiCloud OpenSandbox task image"
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--task-dir", type=Path)
    source.add_argument("--dataset-root", type=Path)
    parser.add_argument("--include", default=os.environ.get("INCLUDE_TASKS", ""))
    parser.add_argument(
        "--registry",
        default=os.environ.get(
            "HARBOR_OPENSANDBOX_REGISTRY", "registry.gate.yicloud.com.cn"
        ),
    )
    parser.add_argument(
        "--repository",
        default=os.environ.get("HARBOR_OPENSANDBOX_IMAGE_REPOSITORY", ""),
    )
    parser.add_argument(
        "--sandbox-image-prefix",
        default=os.environ.get("HARBOR_OPENSANDBOX_SANDBOX_IMAGE_PREFIX", ""),
    )
    parser.add_argument(
        "--docker-config",
        type=Path,
        default=default_path(
            "HARBOR_OPENSANDBOX_DOCKER_CONFIG", Path.home() / ".docker" / "config.json"
        ),
    )
    parser.add_argument(
        "--cache-root",
        type=Path,
        default=default_path(
            "HARBOR_OPENSANDBOX_IMAGE_CACHE_ROOT",
            Path("/data/harbor-runs/opensandbox-images"),
        ),
    )
    parser.add_argument(
        "--platform",
        default=os.environ.get("HARBOR_OPENSANDBOX_IMAGE_PLATFORM", "linux/amd64"),
    )
    parser.add_argument(
        "--tag-prefix",
        default=os.environ.get("HARBOR_OPENSANDBOX_IMAGE_TAG_PREFIX", "harbor"),
    )
    parser.add_argument(
        "--dockerhub-mirror-prefix",
        default=os.environ.get(
            "HARBOR_OPENSANDBOX_DOCKERHUB_MIRROR_PREFIX", "m.daocloud.io/docker.io"
        ),
    )
    parser.add_argument(
        "--apt-mirror",
        default=os.environ.get(
            "HARBOR_OPENSANDBOX_APT_MIRROR", "http://mirrors.tuna.tsinghua.edu.cn"
        ),
    )
    parser.add_argument(
        "--build-args-json",
        default=os.environ.get("HARBOR_OPENSANDBOX_BUILD_ARGS_JSON", "{}"),
    )
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--use-proxy", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    args.registry = args.registry.strip().removeprefix("https://").rstrip("/")
    args.repository = args.repository.strip().strip("/")
    if not args.repository:
        parser.error("--repository or HARBOR_OPENSANDBOX_IMAGE_REPOSITORY is required")
    if not args.sandbox_image_prefix:
        args.sandbox_image_prefix = args.repository
    args.sandbox_image_prefix = args.sandbox_image_prefix.strip().rstrip(":")
    return args


def main() -> int:
    args = parse_args()
    print(prepare(args))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    # This is the CLI boundary: report any operational failure without a
    # traceback while preserving KeyboardInterrupt and other BaseExceptions.
    except Exception as exc:  # noqa: BLE001
        log(f"failed: {type(exc).__name__}: {exc}")
        raise SystemExit(1)

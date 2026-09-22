"""Skopeo publication, registry inspection, and digest checks."""

from __future__ import annotations

import base64
import json
import os
import re
import ssl
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

from .oci import DOCKER_MANIFEST, normalize_oci_image_config

SKOPEO_COPY_ATTEMPTS = 3
SKOPEO_COPY_RETRY_DELAY_SECONDS = 3


def log(message: str) -> None:
    print(f"[harbor-task-image] {message}", file=sys.stderr, flush=True)


def safe_tag_component(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-").lower()
    return normalized or "task"


def validate_registry_host(registry: str) -> str:
    host = registry.strip()
    if not host:
        raise ValueError("--registry or YICLOUD_HARBOR_HOST is required")
    if (
        "://" in host
        or "/" in host
        or "@" in host
        or any(character.isspace() for character in host)
    ):
        raise ValueError(
            f"registry must be a bare OCI registry host, got: {registry!r}"
        )
    return host


@dataclass(frozen=True)
class RegistryTarget:
    """The immutable address scope for one task's Harbor repository."""

    registry: str
    project: str
    task_repository: str

    @property
    def repository(self) -> str:
        return f"{self.project}/{self.task_repository}"

    def tag(self, service: str, input_hash: str) -> str:
        return (
            f"{safe_tag_component(service)}-{input_hash.removeprefix('sha256:')[:20]}"
        )

    def tag_ref(self, service: str, input_hash: str) -> str:
        return f"{self.registry}/{self.repository}:{self.tag(service, input_hash)}"

    def digest_ref(self, artifact_digest: str) -> str:
        return f"{self.registry}/{self.repository}@{artifact_digest}"


def check_task_repository(task_identity: str, *, maximum_length: int = 255) -> str:
    """Validate that a task identity can be used verbatim as its repository."""
    if not task_identity:
        raise ValueError("task identity must not be empty")
    if len(task_identity) > maximum_length:
        raise ValueError(
            f"task identity exceeds the {maximum_length}-character repository limit: "
            f"{task_identity!r}; fix the dataset adapter instead of renaming it during upload"
        )
    # OCI/Docker repository path components permit one dot or underscore,
    # two underscores, or one-or-more dashes between lowercase alphanumeric
    # runs. In particular, SWE-Rebench's ``owner__repository-issue`` identity
    # is already valid and must remain unchanged.
    if not re.fullmatch(r"[a-z0-9]+(?:(?:[._]|__|[-]+)[a-z0-9]+)*", task_identity):
        raise ValueError(
            f"task identity is not a valid OCI repository component: {task_identity!r}; "
            "fix the dataset adapter instead of renaming it during upload"
        )
    return task_identity


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


def registry_credentials(config_path: Path, registry: str) -> tuple[str, str]:
    """Prefer the ignored Harbor credential environment, then Docker config."""
    username = os.environ.get("YICLOUD_HARBOR_USERNAME", "").strip()
    password = os.environ.get("YICLOUD_HARBOR_PASSWORD", "")
    if username or password:
        if not username or not password:
            raise RuntimeError(
                "YICLOUD_HARBOR_USERNAME and YICLOUD_HARBOR_PASSWORD must be set together"
            )
        return username, password
    return docker_credentials(config_path, registry)


class SkopeoPublisher:
    """Thin, task-scoped `skopeo` Registry publisher.

    It intentionally delegates blob probing, mounting and upload mechanics to
    skopeo; this class only performs login, copy and independent inspection.
    """

    def __init__(
        self, target: RegistryTarget, username: str, password: str, *, tls_verify: bool
    ) -> None:
        self.target = target
        self.username = username
        self.password = password
        self.tls_verify = tls_verify
        self._logged_in = False
        # `skopeo login` otherwise writes to the process-wide XDG runtime
        # auth.json. Batch prebuild has several independent publisher
        # processes, which can truncate that shared file while logging in.
        self._auth_dir = tempfile.TemporaryDirectory(prefix="opensandbox-skopeo-auth-")
        self._authfile = str(Path(self._auth_dir.name) / "auth.json")

    def close(self) -> None:
        self._auth_dir.cleanup()

    def __del__(self) -> None:
        # Best effort for callers that abort before `prepare_task_images` returns.
        self.close()

    def _environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            environment.pop(name, None)
        return environment

    def _run(self, command: list[str], *, input_text: str | None = None) -> str:
        completed = subprocess.run(
            command,
            input=input_text,
            text=True,
            stdin=subprocess.DEVNULL if input_text is None else None,
            capture_output=True,
            env=self._environment(),
            check=False,
            timeout=1800,
        )
        if completed.returncode:
            error = completed.stderr.strip()[-1000:]
            raise RuntimeError(
                f"skopeo command failed (exit={completed.returncode}): "
                f"{' '.join(command)}; {error or '<no stderr>'}"
            )
        return completed.stdout

    def login(self) -> None:
        if self._logged_in:
            return
        if not self.username and not self.password:
            # Keep anonymous reads independent of ambient Docker credentials.
            Path(self._authfile).write_text('{"auths": {}}\n', encoding="utf-8")
            return
        command = [
            "skopeo",
            "login",
            "--authfile",
            self._authfile,
            self.target.registry,
            "--username",
            self.username,
            "--password-stdin",
        ]
        command.append("--tls-verify=true" if self.tls_verify else "--tls-verify=false")
        self._run(command, input_text=self.password)
        self._logged_in = True

    def _image_url(self, ref: str) -> str:
        return f"docker://{ref}"

    def inspect(self, ref: str) -> dict[str, str] | None:
        self.login()
        # Skopeo 1.4 (the current YiCloud runner package) exposes Digest but
        # not MediaType in inspect templates. Digest is the required cache and
        # runtime identity; the v2s2 copy format determines the media type.
        command = [
            "skopeo",
            "inspect",
            "--authfile",
            self._authfile,
            "--format",
            "{{.Digest}}",
        ]
        command.append("--tls-verify=true" if self.tls_verify else "--tls-verify=false")
        command.append(self._image_url(ref))
        try:
            output = self._run(command).strip().split(maxsplit=1)
        except RuntimeError as exc:
            # Skopeo emits both a 404 and a clear manifest-not-known error for
            # cache misses. Keep real registry/auth errors fatal.
            message = str(exc).lower()
            if (
                ("repository " in message or "artifact " in message)
                and " not found" in message
            ) or any(
                marker in message
                for marker in (
                    "manifest unknown",
                    "repository not found",
                    "name unknown",
                    "status code: 404",
                )
            ):
                return None
            raise
        if not output or not output[0].startswith("sha256:"):
            raise RuntimeError(f"skopeo inspect returned no manifest digest for {ref}")
        return {"artifact_digest": output[0], "media_type": DOCKER_MANIFEST}

    def inspect_config(self, ref: str) -> dict[str, object]:
        """Return the final image config for a published or external image."""
        self.login()
        command = ["skopeo", "inspect", "--authfile", self._authfile, "--config"]
        command.append("--tls-verify=true" if self.tls_verify else "--tls-verify=false")
        command.append(self._image_url(ref))
        try:
            payload = json.loads(self._run(command))
        except ValueError as exc:
            raise RuntimeError(
                f"skopeo inspect --config returned invalid JSON for {ref}"
            ) from exc
        return normalize_oci_image_config(payload)

    def copy(
        self, source: str, destination: str, *, source_is_archive: bool = False
    ) -> dict[str, str]:
        self.login()
        command = [
            "skopeo",
            "copy",
            "--format",
            "v2s2",
            "--src-authfile",
            self._authfile,
            "--dest-authfile",
            self._authfile,
        ]
        command.append(
            "--dest-tls-verify=true" if self.tls_verify else "--dest-tls-verify=false"
        )
        if not source_is_archive:
            command.append(
                "--src-tls-verify=true" if self.tls_verify else "--src-tls-verify=false"
            )
        source_ref = (
            f"oci-archive:{source}" if source_is_archive else self._image_url(source)
        )
        # `--digestfile` captures the manifest digest produced by skopeo's
        # v2s2 conversion. Read it independently from a subsequent inspect so
        # a successful copy cannot silently record a different target tag.
        with tempfile.NamedTemporaryFile(
            prefix="skopeo-digest-", delete=False
        ) as handle:
            digest_path = Path(handle.name)
        try:
            copy_command = [
                *command,
                "--digestfile",
                str(digest_path),
                source_ref,
                self._image_url(destination),
            ]
            for attempt in range(1, SKOPEO_COPY_ATTEMPTS + 1):
                try:
                    self._run(copy_command)
                    break
                except RuntimeError as exc:
                    if attempt == SKOPEO_COPY_ATTEMPTS:
                        raise
                    log(
                        "warning: skopeo copy failed "
                        f"(attempt {attempt}/{SKOPEO_COPY_ATTEMPTS}); "
                        f"retrying in {SKOPEO_COPY_RETRY_DELAY_SECONDS}s: {exc}"
                    )
                    time.sleep(SKOPEO_COPY_RETRY_DELAY_SECONDS)
            copied_digest = digest_path.read_text(encoding="utf-8").strip()
        finally:
            digest_path.unlink(missing_ok=True)
        inspected = self.inspect(destination)
        if inspected is None:
            raise RuntimeError(
                f"skopeo copy succeeded but target tag cannot be inspected: {destination}"
            )
        if copied_digest != inspected["artifact_digest"]:
            raise RuntimeError(
                "skopeo copy/inspect digest mismatch for "
                f"{destination}: copy={copied_digest!r} inspect={inspected['artifact_digest']!r}"
            )
        return inspected


class RegistryClient:
    """Task-scoped read client; publishing is intentionally delegated to skopeo."""

    def __init__(self, target: RegistryTarget, publisher: SkopeoPublisher) -> None:
        self.target = target
        self.publisher = publisher
        self._artifacts: list[dict[str, object]] | None = None

    def list_artifacts(self) -> list[dict[str, object]]:
        """List task artifacts using available credentials; only a missing repo is a miss."""
        context = ssl.create_default_context()
        if not self.publisher.tls_verify:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        opener = build_opener(ProxyHandler({}), HTTPSHandler(context=context))
        url = (
            f"https://{self.target.registry}/api/v2.0/projects/"
            f"{quote(self.target.project, safe='')}/repositories/"
            f"{quote(self.target.task_repository, safe='')}/artifacts"
        )
        artifacts: list[dict[str, object]] = []
        page = 1
        while True:
            request = f"{url}?page_size=100&with_tag=true&page={page}"
            if self.publisher.username and self.publisher.password:
                request = Request(request)
                token = base64.b64encode(
                    f"{self.publisher.username}:{self.publisher.password}".encode()
                ).decode("ascii")
                # Do not forward registry credentials on HTTP redirects.
                request.add_unredirected_header("Authorization", f"Basic {token}")
            try:
                with opener.open(request, timeout=30) as response:
                    batch = json.load(response)
            except HTTPError as exc:
                if exc.code == 404 and page == 1:
                    return []
                raise
            if not isinstance(batch, list) or any(
                not isinstance(item, dict) for item in batch
            ):
                raise ValueError(
                    "Harbor artifact listing must return a list of objects"
                )
            artifacts.extend(batch)
            if len(batch) < 100:
                return artifacts
            page += 1

    def latest_image(
        self, service: str, *, single_service: bool
    ) -> dict[str, str] | None:
        candidates = []
        # Resolve all services against the same initial repository listing.
        # A build of the first service must not hide an initially empty repo.
        if self._artifacts is None:
            self._artifacts = self.list_artifacts()
        artifacts = self._artifacts
        for artifact in artifacts:
            for tag in artifact.get("tags") or []:
                name = tag["name"]
                if not single_service and not re.fullmatch(
                    rf"{re.escape(safe_tag_component(service))}-[0-9a-f]{{20}}", name
                ):
                    continue
                # Tag time distinguishes tags added to the same artifact.
                push_time = tag.get("push_time") or artifact.get("push_time")
                candidates.append((push_time, name, artifact))
        if not candidates:
            if artifacts:
                raise RuntimeError(
                    f"task repository {self.target.repository!r} has artifacts "
                    f"but no usable tag for service {service!r}"
                )
            return None
        if len(candidates) > 1:

            def order(candidate):
                pushed, name, artifact = candidate
                timestamp = datetime.fromisoformat(pushed.replace("Z", "+00:00"))
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
                return timestamp, name, artifact["digest"]

            selected = max(candidates, key=order)
        else:
            selected = candidates[0]
        _, tag, artifact = selected
        digest = artifact["digest"]
        if not isinstance(digest, str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", digest
        ):
            raise ValueError("Harbor artifact listing returned an invalid digest")
        return {
            "tag": tag,
            "tag_ref": f"{self.target.registry}/{self.target.repository}:{tag}",
            "artifact_digest": digest,
            "digest_ref": self.target.digest_ref(digest),
            "media_type": artifact.get("manifest_media_type") or DOCKER_MANIFEST,
        }

    def manifest(self, tag: str) -> dict[str, str] | None:
        return self.publisher.inspect(
            f"{self.target.registry}/{self.target.repository}:{tag}"
        )

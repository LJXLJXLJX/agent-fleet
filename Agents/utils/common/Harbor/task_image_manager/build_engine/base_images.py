"""Resolve and pin external base-image references."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from urllib.parse import urlparse

from ..oci import digest_bytes

FROM_LINE = re.compile(
    r"^(?P<prefix>\s*FROM(?:\s+--platform=\S+)?\s+)"
    r"(?P<image>\S+)(?P<suffix>.*)$",
    re.IGNORECASE,
)
AS_ALIAS = re.compile(r"\s+AS\s+(?P<alias>[A-Za-z0-9_.-]+)\s*$", re.IGNORECASE)
DOCKERFILE_INSTRUCTION = re.compile(r"^\s*(?P<name>[A-Za-z]+)\b")
HEREDOC_MARKER = re.compile(
    r"<<(?P<strip>-)?\s*(?P<quote>['\"]?)"
    r"(?P<delimiter>[A-Za-z0-9_.-]+)(?P=quote)"
)


def mirror_image_ref(image: str, mirror_prefix: str, aliases: set[str]) -> str:
    if not mirror_prefix or image in aliases or image.startswith("$"):
        return image
    first, separator, remainder = image.partition("/")
    if first == "docker.io" and separator:
        return f"{mirror_prefix.rstrip('/')}/{remainder}"
    if not separator or (
        "." not in first and ":" not in first and first != "localhost"
    ):
        relative = image if "/" in image else f"library/{image}"
        return f"{mirror_prefix.rstrip('/')}/{relative}"
    return image


def normalize_base_image_registry(value: str) -> str:
    registry = value.strip().rstrip("/")
    if not registry:
        return ""
    if (
        "://" in registry
        or registry.startswith("/")
        or any(character.isspace() for character in registry)
        or any(character in registry for character in "@?#")
    ):
        raise ValueError(
            "base image registry must be an OCI registry/repository prefix "
            "without a scheme, digest, query, fragment, or credentials"
        )
    return registry


def base_image_registry_host(registry: str) -> str:
    if not registry:
        return ""
    return urlparse(f"//{registry}").hostname or ""


def direct_host_environment(host: str) -> dict[str, str]:
    environment = os.environ.copy()
    if not host:
        return environment
    existing = environment.get("NO_PROXY", environment.get("no_proxy", ""))
    entries = [entry.strip() for entry in existing.split(",") if entry.strip()]
    if host not in entries:
        entries.append(host)
    environment["NO_PROXY"] = ",".join(entries)
    environment["no_proxy"] = environment["NO_PROXY"]
    return environment


def _is_unqualified_image_ref(image: str) -> bool:
    first, separator, _remainder = image.partition("/")
    return not separator or (
        "." not in first and ":" not in first and first != "localhost"
    )


def _is_docker_io_image_ref(image: str) -> bool:
    first, separator, _remainder = image.partition("/")
    return bool(separator) and first in {"docker.io", "index.docker.io"}


def dockerfile_external_base_images(
    source: str,
    *,
    include_docker_io: bool = False,
) -> tuple[str, ...]:
    """Return external FROM references without inspecting heredoc payloads."""
    images: list[str] = []
    aliases: set[str] = set()
    heredocs: list[tuple[str, bool]] = []
    active_instruction: str | None = None
    for source_line in source.splitlines():
        if heredocs:
            delimiter, strip_tabs = heredocs[0]
            candidate = source_line.lstrip("\t") if strip_tabs else source_line
            if candidate == delimiter:
                heredocs.pop(0)
                if not heredocs:
                    active_instruction = None
            continue

        if active_instruction is None:
            instruction = DOCKERFILE_INSTRUCTION.match(source_line)
            if instruction:
                active_instruction = instruction.group("name").upper()

        match = FROM_LINE.match(source_line)
        if match:
            image = match.group("image")
            resolvable = _is_unqualified_image_ref(image) or (
                include_docker_io and _is_docker_io_image_ref(image)
            )
            if (
                image not in aliases
                and image != "scratch"
                and not image.startswith("$")
                and resolvable
                and image not in images
            ):
                images.append(image)
            alias_match = AS_ALIAS.search(match.group("suffix"))
            if alias_match:
                aliases.add(alias_match.group("alias"))

        if active_instruction in {"RUN", "COPY", "ADD"}:
            heredocs.extend(
                (item.group("delimiter"), bool(item.group("strip")))
                for item in HEREDOC_MARKER.finditer(source_line)
            )
        if not heredocs and not source_line.rstrip().endswith("\\"):
            active_instruction = None
    return tuple(images)


def base_image_lookup_ref(image: str, registry: str) -> str:
    """Join a FROM reference with the base registry's repository prefix."""
    path = image
    for prefix in ("index.docker.io/", "docker.io/"):
        if path.startswith(prefix):
            path = path[len(prefix) :]
            break
    path = path.removeprefix("library/")
    return f"{registry}/{path}"


def resolve_base_image_contexts(
    source: str,
    registry: str,
    platform: str,
) -> tuple[dict[str, str], dict[str, str]]:
    """Resolve logical FROM names into immutable BuildKit named contexts."""
    registry = normalize_base_image_registry(registry)
    if not registry:
        return {}, {}
    replacements: dict[str, str] = {}
    contexts: dict[str, str] = {}
    for image in dockerfile_external_base_images(source, include_docker_io=True):
        source_ref = base_image_lookup_ref(image, registry)
        resolved_ref, digest = inspect_external_image(
            source_ref,
            dockerhub_mirror_prefix="",
            platform=platform,
            dry_run=False,
            direct_host=base_image_registry_host(registry),
        )
        context_name = (
            "opensandbox-base-" + hashlib.sha256(image.encode("utf-8")).hexdigest()[:20]
        )
        immutable_ref = f"{resolved_ref.split('@', 1)[0]}@{digest}"
        replacements[image] = context_name
        contexts[context_name] = f"docker-image://{immutable_ref}"
    return replacements, contexts


def inspect_external_image(
    image_ref: str,
    dockerhub_mirror_prefix: str,
    platform: str,
    *,
    dry_run: bool,
    direct_host: str = "",
) -> tuple[str, str]:
    resolved_ref = mirror_image_ref(image_ref, dockerhub_mirror_prefix, aliases=set())
    if dry_run:
        payload = f"dry-run\0{resolved_ref}\0{platform}".encode()
        return resolved_ref, digest_bytes(payload)
    completed = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", "--raw", resolved_ref],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
        timeout=180,
        env=direct_host_environment(direct_host),
    )
    if completed.returncode != 0 or not completed.stdout:
        error = completed.stderr.decode("utf-8", errors="replace")[-500:].strip()
        raise RuntimeError(
            f"failed to inspect external image {resolved_ref!r}: "
            f"exit={completed.returncode} error={error or '<none>'}"
        )
    return resolved_ref, digest_bytes(completed.stdout)

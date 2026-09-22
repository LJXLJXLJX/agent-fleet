"""OCI archive inspection, config normalization, and manifest media types."""

from __future__ import annotations

import hashlib
import json
import tarfile
from pathlib import Path

DOCKER_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
DOCKER_CONFIG = "application/vnd.docker.container.image.v1+json"
DOCKER_LAYER_GZIP = "application/vnd.docker.image.rootfs.diff.tar.gzip"
OCI_CONFIG = "application/vnd.oci.image.config.v1+json"
OCI_LAYER_GZIP = "application/vnd.oci.image.layer.v1.tar+gzip"

def digest_bytes(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


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
            f"expected exactly one platform manifest, found {len(source_descriptors)}"
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


def _string_argv(value: object, *, label: str) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RuntimeError(f"OCI image config {label} must be a string list or null")
    return list(value)


def _oci_port_entries(raw: object) -> list[dict[str, object]]:
    if raw is None:
        return []
    if not isinstance(raw, dict):
        raise TypeError("OCI image config ExposedPorts must be an object or null")
    ports: list[dict[str, object]] = []
    for token in sorted(raw):
        if not isinstance(token, str):
            raise TypeError("OCI image config ExposedPorts keys must be strings")
        raw_port, separator, raw_protocol = token.partition("/")
        protocol = raw_protocol.lower() if separator else "tcp"
        if not raw_port.isdigit() or protocol not in {"tcp", "udp"}:
            raise RuntimeError(f"invalid OCI image config exposed port: {token!r}")
        port = int(raw_port)
        if not 1 <= port <= 65535:
            raise RuntimeError(f"invalid OCI image config exposed port: {token!r}")
        ports.append({"port": port, "protocol": protocol})
    return ports


def normalize_oci_image_config(raw: object) -> dict[str, object]:
    """Extract only OCI fields needed by the Bundle runtime contract."""
    if not isinstance(raw, dict):
        raise TypeError("OCI image config must be a JSON object")
    config = raw.get("config", raw)
    if not isinstance(config, dict):
        raise TypeError("OCI image config .config must be a JSON object")
    healthcheck = config.get("Healthcheck")
    if healthcheck is not None and not isinstance(healthcheck, dict):
        raise RuntimeError("OCI image config Healthcheck must be an object or null")
    normalized_healthcheck = dict(healthcheck) if healthcheck is not None else None
    if normalized_healthcheck is not None and "Test" in normalized_healthcheck:
        normalized_healthcheck["test"] = normalized_healthcheck.pop("Test")
    working_dir = config.get("WorkingDir")
    if working_dir is not None and not isinstance(working_dir, str):
        raise RuntimeError("OCI image config WorkingDir must be a string or null")
    working_dir = working_dir or None
    if working_dir is not None and not working_dir.startswith("/"):
        raise RuntimeError("OCI image config WorkingDir must be an absolute path")
    return {
        "entrypoint": _string_argv(config.get("Entrypoint"), label="Entrypoint"),
        "cmd": _string_argv(config.get("Cmd"), label="Cmd"),
        "working_dir": working_dir,
        "exposed_ports": _oci_port_entries(config.get("ExposedPorts")),
        "healthcheck": normalized_healthcheck,
    }


def oci_archive_image_config(archive_path: Path) -> dict[str, object]:
    """Read the final image config from a local OCI archive before publishing."""
    with tarfile.open(archive_path, "r") as archive:
        index = json.loads(read_member_bytes(archive, "index.json"))
        descriptors = index.get("manifests") or []
        if len(descriptors) != 1:
            raise RuntimeError(
                "expected exactly one platform manifest while reading OCI image config, "
                f"found {len(descriptors)}"
            )
        descriptor = descriptors[0]
        manifest_bytes = read_member_bytes(
            archive, blob_member_name(str(descriptor["digest"]))
        )
        if digest_bytes(manifest_bytes) != descriptor["digest"]:
            raise RuntimeError("OCI image manifest digest mismatch")
        manifest = json.loads(manifest_bytes)
        config = manifest.get("config")
        if not isinstance(config, dict) or not isinstance(config.get("digest"), str):
            raise TypeError("OCI image manifest has no config descriptor")
        config_bytes = read_member_bytes(archive, blob_member_name(config["digest"]))
        if digest_bytes(config_bytes) != config["digest"]:
            raise RuntimeError("OCI image config digest mismatch")
    return normalize_oci_image_config(json.loads(config_bytes))

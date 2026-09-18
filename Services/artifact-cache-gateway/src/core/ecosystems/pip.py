"""pip ecosystem hooks: pytorch-wheels path validation, pytorch HTML preprocessing and inventory fields. pypi (generic) has no protocol-level path validation (admission is carried by the trunk general allowlist in build_url), so validate_path always returns None for generic."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .base import EcosystemHandler

if TYPE_CHECKING:
    from ..config.source import SourceConfig


_PYTORCH_CHANNEL = r"(?:cpu|xpu|cu[0-9]+|rocm[0-9]+(?:\.[0-9]+)*)"


_PYTORCH_PACKAGE_INDEX = re.compile(
    rf"^(?P<channel>{_PYTORCH_CHANNEL})/"
    r"(?P<package>[A-Za-z0-9._-]+)/$"
)


_PYTORCH_ARTIFACT = re.compile(
    rf"^(?P<channel>{_PYTORCH_CHANNEL})/"
    r"[^/]+(?:\.whl(?:\.metadata)?|\.tar\.(?:gz|bz2|xz)|\.zip)$",
    re.IGNORECASE,
)


_PYTORCH_ROOT_ARTIFACT = re.compile(
    r"^[^/]+(?:\.whl(?:\.metadata)?|\.tar\.(?:gz|bz2|xz)|\.zip)$",
    re.IGNORECASE,
)


def _validate_pytorch_path(source: SourceConfig, decoded: str) -> str | None:
    pytorch_package_index = _PYTORCH_PACKAGE_INDEX.fullmatch(decoded)
    pytorch_artifact = _PYTORCH_ARTIFACT.fullmatch(decoded)
    pytorch_channel_path = pytorch_package_index or pytorch_artifact
    if not (
        pytorch_channel_path
        or (
            _PYTORCH_ROOT_ARTIFACT.fullmatch(decoded)
            and any(
                decoded.lower().startswith(
                    f"{package.lower().replace('-', '_')}-"
                )
                or decoded.lower().startswith(f"{package.lower()}-")
                for package in source.root_artifact_packages
            )
        )
    ):
        return "upstream path does not match the PyTorch wheel allowlist"
    return None


def _validate_path(source: SourceConfig, decoded: str) -> str | None:
    if source.kind != "pytorch-wheels":
        return None
    return _validate_pytorch_path(source, decoded)


def _prepare_pytorch_html(source: SourceConfig, rewritten: bytes) -> bytes:
    # Aliyun channel pages are flat. Package-index requests are mapped to
    # that page, so bare artifact links must resolve one level above the
    # synthetic /<channel>/<package>/ route.
    if source.kind != "pytorch-wheels":
        return rewritten
    rewritten = re.sub(
        (
            rb"(?i)((?:href|src)\s*=\s*['\"])([^/'\"]+"
            rb"(?:\.whl(?:\.metadata)?|\.tar\.(?:gz|bz2|xz)|\.zip)"
            rb"(?:#[^'\"]*)?)"
        ),
        lambda match: match.group(1) + b"../" + match.group(2),
        rewritten,
    )
    return rewritten


def _inventory_fields(
    source: SourceConfig, decoded_path: str, filename: str, content_type: str
) -> dict[str, str | None]:
    return _pip_fields(source, decoded_path, filename, content_type)


def _wheel_fields(filename: str) -> tuple[str, str, str, str, str] | None:
    if not filename.lower().endswith(".whl"):
        return None
    parts = filename[:-4].split("-")
    if len(parts) not in {5, 6}:
        return None
    package, version = parts[0], parts[1]
    python_tag, abi_tag, platform_tag = parts[-3:]
    if not all((package, version, python_tag, abi_tag, platform_tag)):
        return None
    return package, version, python_tag, abi_tag, platform_tag


def _sdist_fields(filename: str) -> tuple[str, str] | None:
    lowered = filename.lower()
    suffix = next(
        (
            candidate
            for candidate in (".tar.gz", ".tar.bz2", ".tar.xz", ".zip")
            if lowered.endswith(candidate)
        ),
        None,
    )
    if suffix is None:
        return None
    stem = filename[: -len(suffix)]
    package, separator, version = stem.rpartition("-")
    if not separator or not package or not version:
        return None
    return package, version


def _platform_architecture(platform_tag: str) -> str | None:
    for architecture in (
        "x86_64",
        "aarch64",
        "amd64",
        "arm64",
        "ppc64le",
        "s390x",
        "i686",
        "universal2",
    ):
        if platform_tag == architecture or platform_tag.endswith(f"_{architecture}"):
            return architecture
    return None


def _pip_fields(
    source: SourceConfig, relative_path: str, filename: str, content_type: str
) -> dict[str, str | None]:
    wheel = _wheel_fields(filename)
    if wheel:
        package, version, python_tag, abi_tag, platform_tag = wheel
        return {
            "package": package,
            "version": version,
            "object_type": "artifact",
            "python_tag": python_tag,
            "abi_tag": abi_tag,
            "platform_tag": platform_tag,
            "architecture": _platform_architecture(platform_tag),
        }
    sdist = _sdist_fields(filename)
    if sdist:
        return {
            "package": sdist[0],
            "version": sdist[1],
            "object_type": "artifact",
        }

    segments = [segment for segment in relative_path.strip("/").split("/") if segment]
    if content_type.lower().startswith("text/html"):
        package = segments[-1] if segments else "@root"
        if source.name == "pypi-files" and len(segments) > 1:
            package = segments[-2]
        return {"package": package, "version": "@index", "object_type": "index"}
    return {"package": filename, "version": "@unversioned", "object_type": "object"}

HANDLER = EcosystemHandler(
    name="pip",
    kinds=("generic", "pytorch-wheels"),
    ecosystems=("pip",),
    validate_path=_validate_path,
    prepare_html=_prepare_pytorch_html,
    inventory_fields=_inventory_fields,
)

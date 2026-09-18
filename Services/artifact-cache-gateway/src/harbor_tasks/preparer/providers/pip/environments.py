"""pip resolution environments (image/distribution Python -> PipEnvironment mapping)."""

from __future__ import annotations

import re

from ...models import PipEnvironment

_PYTHON_IMAGE = re.compile(
    r"(?:^|/)(?:library/)?python:(?P<tag>3\.(?P<minor>\d+)(?:[-.][a-z0-9_.-]+)?)$",
    re.I,
)


_DISTRO_PYTHONS: tuple[tuple[re.Pattern[str], str, str], ...] = (
    (re.compile(r"(?:^|[:_-])(?:24\.04|noble)(?:$|[-_])", re.I), "3.12", "manylinux_2_39_x86_64"),
    (re.compile(r"(?:^|[:_-])(?:22\.04|jammy)(?:$|[-_])", re.I), "3.10", "manylinux_2_35_x86_64"),
    (re.compile(r"(?:^|[:_-])(?:20\.04|focal)(?:$|[-_])", re.I), "3.8", "manylinux_2_31_x86_64"),
    (re.compile(r"(?:^|[:_-])(?:bookworm|debian-?12)(?:$|[-_])", re.I), "3.11", "manylinux_2_36_x86_64"),
    (re.compile(r"(?:^|[:_-])(?:bullseye|debian-?11)(?:$|[-_])", re.I), "3.9", "manylinux_2_31_x86_64"),
)


def _environment(
    python_version: str, platform: str, resolver_image: str
) -> PipEnvironment:
    compact = python_version.replace(".", "")
    return PipEnvironment(
        implementation="cpython",
        python_version=python_version,
        abi=f"cp{compact}",
        platform=platform,
        architecture="amd64",
        resolver_image=resolver_image,
    )


def _normalized_python_version(value: object) -> str | None:
    text = str(value or "").strip()
    match = re.fullmatch(r"3(?:\.)?(\d+)", text)
    return f"3.{match.group(1)}" if match else None


def environment_for_image(image: str) -> PipEnvironment | None:
    normalized = image.lower()
    python_match = _PYTHON_IMAGE.search(normalized)
    if python_match:
        tag = python_match.group("tag")
        if "alpine" in tag:
            return None
        version = f"3.{python_match.group('minor')}"
        resolver = f"m.daocloud.io/docker.io/library/python:{tag}"
        return _environment(version, "native-linux-x86_64", resolver)
    for pattern, version, platform in _DISTRO_PYTHONS:
        if pattern.search(normalized):
            resolver = (
                f"m.daocloud.io/docker.io/library/python:{version}-slim"
            )
            return _environment(version, platform, resolver)
    return None


def environment_from_identity(identity: str) -> PipEnvironment | None:
    fields = identity.split(":")
    if len(fields) != 5 or fields[0] != "cpython" or fields[4] != "amd64":
        return None
    version, abi, platform = fields[1], fields[2], fields[3]
    if abi != f"cp{version.replace('.', '')}":
        return None
    return _environment(
        version,
        platform,
        f"m.daocloud.io/docker.io/library/python:{version}-slim",
    )

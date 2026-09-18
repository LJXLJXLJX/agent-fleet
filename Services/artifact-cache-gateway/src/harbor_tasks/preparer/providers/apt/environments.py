"""APT resolution environments (image -> AptEnvironment mapping)."""

from __future__ import annotations

import re

from ...models import AptEnvironment

_IMAGE_ENVIRONMENTS: tuple[tuple[re.Pattern[str], AptEnvironment], ...] = (
    (
        re.compile(r"(?:^|[:_-])(?:22\.04|jammy)(?:$|[-_])", re.I),
        AptEnvironment(
            "ubuntu",
            "jammy",
            "amd64",
            "m.daocloud.io/docker.io/library/ubuntu:22.04",
        ),
    ),
    (
        re.compile(r"(?:^|[:_-])(?:24\.04|noble)(?:$|[-_])", re.I),
        AptEnvironment(
            "ubuntu",
            "noble",
            "amd64",
            "m.daocloud.io/docker.io/library/ubuntu:24.04",
        ),
    ),
    (
        re.compile(r"(?:^|[:_-])(?:20\.04|focal)(?:$|[-_])", re.I),
        AptEnvironment(
            "ubuntu",
            "focal",
            "amd64",
            "m.daocloud.io/docker.io/library/ubuntu:20.04",
        ),
    ),
    (
        re.compile(r"(?:^|[:_-])(?:bookworm|debian-?12)(?:$|[-_])", re.I),
        AptEnvironment(
            "debian",
            "bookworm",
            "amd64",
            "m.daocloud.io/docker.io/library/debian:bookworm",
        ),
    ),
    (
        re.compile(r"(?:^|[:_-])(?:bullseye|debian-?11)(?:$|[-_])", re.I),
        AptEnvironment(
            "debian",
            "bullseye",
            "amd64",
            "m.daocloud.io/docker.io/library/debian:bullseye",
        ),
    ),
)


def environment_for_image(image: str) -> AptEnvironment | None:
    normalized = image.lower()
    if normalized in {"ubuntu", "ubuntu:latest", "docker.io/library/ubuntu:latest"}:
        return None
    for pattern, environment in _IMAGE_ENVIRONMENTS:
        if pattern.search(normalized):
            return environment
    return None


def environment_from_identity(identity: str) -> AptEnvironment | None:
    for _pattern, environment in _IMAGE_ENVIRONMENTS:
        if environment.identity == identity:
            return environment
    return None

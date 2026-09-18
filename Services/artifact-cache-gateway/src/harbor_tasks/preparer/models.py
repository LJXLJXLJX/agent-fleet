from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal

ProbeStatus = Literal[
    "fast", "slow", "unavailable", "unsupported", "not-applicable"
]
WarmStatus = Literal["cached", "already-cached", "failed", "not-actionable"]


@dataclass(frozen=True)
class ProbeSettings:
    timeout_seconds: float = 15.0
    slow_seconds: float = 3.0
    sample_bytes: int = 128 * 1024
    concurrency: int = 16

    def validate(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError("package probe timeout must be positive")
        if self.slow_seconds <= 0 or self.slow_seconds >= self.timeout_seconds:
            raise ValueError(
                "package probe slow threshold must be positive and below timeout"
            )
        if self.sample_bytes < 1 or self.sample_bytes > 8 * 1024 * 1024:
            raise ValueError("package probe sample bytes must be between 1 and 8 MiB")
        if self.concurrency < 1 or self.concurrency > 64:
            raise ValueError("package probe concurrency must be between 1 and 64")


@dataclass(frozen=True)
class AptEnvironment:
    distro: str
    codename: str
    architecture: str
    resolver_image: str

    @property
    def identity(self) -> str:
        return f"{self.distro}:{self.codename}:{self.architecture}"

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class PipEnvironment:
    implementation: str
    python_version: str
    abi: str
    platform: str
    architecture: str
    resolver_image: str

    @property
    def identity(self) -> str:
        return (
            f"{self.implementation}:{self.python_version}:"
            f"{self.abi}:{self.platform}:{self.architecture}"
        )

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class ProbeResult:
    manager: str
    package: str
    environment: str | None
    status: ProbeStatus
    reason: str
    requirement: str | None = None
    python_version: str | None = None
    implementation: str | None = None
    abi: str | None = None
    platform: str | None = None
    domestic_source: str | None = None
    domestic_url: str | None = None
    filename: str | None = None
    version: str | None = None
    sha256: str | None = None
    integrity: str | None = None
    shasum: str | None = None
    size: int | None = None
    elapsed_seconds: float | None = None
    bytes_sampled: int | None = None
    bytes_per_second: float | None = None
    repository_contexts: tuple[dict[str, object], ...] = ()
    environment_id: str | None = None
    consumer_environment_ids: tuple[str, ...] = ()
    build_contexts: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class WarmResult:
    manager: str
    package: str
    environment: str | None
    status: WarmStatus
    reason: str
    requirement: str | None = None
    gateway_url: str | None = None
    first_cache_state: str | None = None
    verification_cache_state: str | None = None
    size: int | None = None
    environment_id: str | None = None
    consumer_environment_ids: tuple[str, ...] = ()
    build_contexts: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

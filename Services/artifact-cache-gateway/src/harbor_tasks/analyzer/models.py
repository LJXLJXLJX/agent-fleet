"""Analyzer data models: aggregate/install/dependency/external-input/build-context dataclasses and stable-id helpers."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

NODE_REGISTRY_MANAGERS = {"npm", "pnpm", "yarn", "bun"}


@dataclass
class Aggregate:
    occurrences: Counter = field(default_factory=Counter)
    tasks: dict = field(default_factory=lambda: defaultdict(set))
    specs: dict = field(default_factory=lambda: defaultdict(Counter))

    def add(self, key: object, task: str, spec: str) -> None:
        self.occurrences[key] += 1
        self.tasks[key].add(task)
        self.specs[key][spec] += 1


@dataclass(frozen=True)
class Install:
    manager: str
    arguments: tuple[str, ...]
    command: str
    python_version: str | None = None


@dataclass(frozen=True)
class AptDependency:
    url: str
    host: str
    kind: str
    coverage: str
    action: str
    reason: str
    query_present: bool


@dataclass(frozen=True)
class ExternalBuildInput:
    url: str
    host: str
    kind: str
    tool: str
    action: str
    cache_mode: str | None
    refresh_policy: str
    reason: str
    query_present: bool
    credentials_present: bool
    reference: str | None = None


@dataclass(frozen=True)
class ExternalBuildInputIssue:
    kind: str
    tool: str
    reason: str


@dataclass(frozen=True)
class BuildContext:
    """One concrete Dockerfile stage that can execute package commands."""

    id: str
    task: str
    dockerfile: str
    stage_index: int
    stage_name: str | None
    base_image: str
    platform: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "task": self.task,
            "dockerfile": self.dockerfile,
            "stage_index": self.stage_index,
            "stage_name": self.stage_name,
            "base_image": self.base_image,
            "platform": self.platform,
        }


@dataclass(frozen=True)
class ScanSource:
    path: Path
    text: str
    contexts: tuple[BuildContext, ...]


def _stable_id(prefix: str, value: object) -> str:
    encoded = json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(encoded).hexdigest()[:16]}"


def _resolution_environment_id(
    manager: str,
    context: BuildContext,
    *,
    python_version: str | None = None,
    repositories: Sequence[dict[str, object]] = (),
    resolution_options: dict[str, object] | None = None,
) -> str:
    repository_rows = []
    for repository in repositories:
        components = repository.get("components", [])
        repository_rows.append(
            {
                "upstream_url": repository.get("upstream_url"),
                "suite": repository.get("suite"),
                "components": sorted(
                    str(item) for item in components
                )
                if isinstance(components, list)
                else [],
            }
        )
    identity: dict[str, object] = {
        "manager": manager,
        "resolution_options": resolution_options or {},
    }
    if manager not in NODE_REGISTRY_MANAGERS:
        identity.update(
            {
                "base_image": context.base_image,
                "platform": context.platform,
                "python_version": python_version,
                "repositories": sorted(
                    repository_rows,
                    key=lambda row: json.dumps(row, sort_keys=True),
                ),
            }
        )
    return _stable_id("env", identity)

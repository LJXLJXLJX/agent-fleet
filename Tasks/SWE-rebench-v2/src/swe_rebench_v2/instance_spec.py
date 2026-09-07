from __future__ import annotations

from dataclasses import dataclass
from typing import Any

type Record = dict[str, Any]


def _difficulty(record: Record) -> str:
    meta = record.get("meta") or {}
    llm_metadata = meta.get("llm_metadata") or {}
    if isinstance(llm_metadata, list):
        llm_metadata = llm_metadata[0] if llm_metadata else {}
    value = llm_metadata.get("difficulty") if isinstance(llm_metadata, dict) else None
    normalized = str(value).lower() if isinstance(value, str) else ""
    if normalized in {"easy", "medium", "hard"}:
        return normalized
    return "medium"


def _command_list(
    value: object,
    field: str,
    instance_id: str,
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        raise TypeError(f"Task {instance_id} has invalid {field}: expected string/list")
    commands = tuple(item for item in value if isinstance(item, str) and item.strip())
    if not commands and not allow_empty:
        raise ValueError(f"Task {instance_id} is missing {field}")
    return commands


@dataclass(frozen=True)
class InstanceSpec:
    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str
    difficulty: str
    language: str | None
    patch: str | None
    test_patch: str | None
    install_commands: tuple[str, ...]
    test_commands: tuple[str, ...]
    log_parser: str
    base_image_name: str
    raw: Record

    @property
    def project_dir(self) -> str:
        return f"/{self.repo.split('/', 1)[1]}"

    @classmethod
    def from_record(cls, record: Record) -> InstanceSpec:
        instance_id = record.get("instance_id")
        if not isinstance(instance_id, str) or not instance_id:
            raise ValueError("Record is missing instance_id")

        repo = record.get("repo")
        if not isinstance(repo, str) or repo.count("/") != 1:
            raise ValueError(f"Task {instance_id} has invalid repo: {repo!r}")

        base_commit = record.get("base_commit")
        if not isinstance(base_commit, str) or not base_commit:
            raise ValueError(f"Task {instance_id} is missing base_commit")

        problem_statement = record.get("problem_statement")
        if not isinstance(problem_statement, str) or not problem_statement.strip():
            # The published dataset contains a small OCaml subset with a null
            # problem_statement but a populated PR description. Both fields
            # describe the requested change and neither contains the gold patch.
            problem_statement = record.get("pr_description")
        if not isinstance(problem_statement, str) or not problem_statement.strip():
            raise ValueError(
                f"Task {instance_id} is missing problem_statement and pr_description"
            )

        install_config = record.get("install_config") or {}
        if not isinstance(install_config, dict):
            raise TypeError(f"Task {instance_id} has invalid install_config")

        # The published V2 Parquet schema uses base_image_name. The upstream
        # combine.Dockerfile.j2 historically reads image_name, so accept both
        # here and normalize only in the renderer.
        base_image_name = install_config.get("base_image_name") or install_config.get(
            "image_name"
        )
        if not isinstance(base_image_name, str) or not base_image_name.strip():
            raise ValueError(f"Task {instance_id} is missing its builder base image")

        log_parser = install_config.get("log_parser")
        if not isinstance(log_parser, str) or not log_parser:
            raise ValueError(f"Task {instance_id} is missing install_config.log_parser")

        language = record.get("language")
        normalized_language = (
            language.strip().lower()
            if isinstance(language, str) and language.strip()
            else None
        )

        return cls(
            instance_id=instance_id,
            repo=repo,
            base_commit=base_commit,
            problem_statement=problem_statement,
            difficulty=_difficulty(record),
            language=normalized_language,
            patch=record.get("patch"),
            test_patch=record.get("test_patch"),
            install_commands=_command_list(
                install_config.get("install"),
                "install_config.install",
                instance_id,
                allow_empty=True,
            ),
            test_commands=_command_list(
                install_config.get("test_cmd"),
                "install_config.test_cmd",
                instance_id,
            ),
            log_parser=log_parser,
            base_image_name=base_image_name,
            raw=record,
        )

"""Dockerfile parsing, build-context mapping, and shell script ownership."""

from __future__ import annotations

import os
import re
import shlex
import sys
from collections import defaultdict
from pathlib import Path
from typing import Iterator, Sequence

from .models import (
    BuildContext,
    ScanSource,
    _stable_id,
)
from .shell import unquoted_heredoc_matches

SHELL_INTERPRETERS = {"sh", "bash", "dash", "zsh", "ksh"}


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"warning: cannot read {path}: {exc}", file=sys.stderr)
        return ""


def discover_tasks(dataset: Path) -> Iterator[Path]:
    for current, directories, files in os.walk(dataset, followlinks=False):
        if "task.toml" in files and "environment" in directories:
            yield Path(current)
            directories[:] = []


def docker_instructions(text: str) -> Iterator[tuple[str, str]]:
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        raw = lines[index]
        index += 1
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue

        pieces: list[str] = []
        while True:
            continued = raw.rstrip().endswith("\\")
            piece = raw.rstrip()[:-1] if continued else raw.rstrip()
            pieces.append(piece.strip())
            if not continued or index >= len(lines):
                break
            raw = lines[index]
            index += 1

        pending = " ".join(pieces).strip()
        match = re.match(r"^([A-Za-z]+)\s+(.*)$", pending, re.DOTALL)
        if not match:
            continue
        instruction = match.group(1).upper()
        body = match.group(2).strip()
        heredocs = unquoted_heredoc_matches(body)
        if not heredocs:
            yield instruction, body
            continue

        sections: list[tuple[str, list[str]]] = []
        for heredoc in heredocs:
            delimiter = heredoc.group(2)
            content: list[str] = []
            while index < len(lines):
                line = lines[index]
                index += 1
                if line.strip() == delimiter:
                    break
                content.append(line)
            sections.append((delimiter, content))

        first = heredocs[0]
        before = body[: first.start()].strip()
        after = body[first.end() :].strip()
        try:
            after_tokens = shlex.split(after, comments=True, posix=True)
        except ValueError:
            after_tokens = after.split()
        interpreter = after_tokens[0].rsplit("/", 1)[-1] if after_tokens else ""
        executable_run_heredoc = (
            instruction == "RUN"
            and len(heredocs) == 1
            and not before
            and (not after_tokens or interpreter in SHELL_INTERPRETERS)
        )
        if executable_run_heredoc:
            yield instruction, "\n".join(sections[0][1])
            continue

        serialized = [body]
        for delimiter, content in sections:
            serialized.extend(content)
            serialized.append(delimiter)
        yield instruction, "\n".join(serialized)


def from_image(body: str) -> str | None:
    try:
        tokens = shlex.split(body, comments=True, posix=True)
    except ValueError:
        tokens = body.split()
    index = 0
    while index < len(tokens) and tokens[index].startswith("--"):
        index += 1
    return tokens[index] if index < len(tokens) else None


def from_stage(body: str) -> tuple[str | None, str | None, str | None]:
    """Return image, optional stage alias, and optional --platform value."""
    try:
        tokens = shlex.split(body, comments=True, posix=True)
    except ValueError:
        tokens = body.split()
    platform: str | None = None
    index = 0
    while index < len(tokens) and tokens[index].startswith("--"):
        token = tokens[index]
        if token.startswith("--platform="):
            platform = token.split("=", 1)[1]
        elif token == "--platform" and index + 1 < len(tokens):
            index += 1
            platform = tokens[index]
        index += 1
    if index >= len(tokens):
        return None, None, platform
    image = tokens[index]
    alias = None
    if index + 2 < len(tokens) and tokens[index + 1].upper() == "AS":
        alias = tokens[index + 2]
    return image, alias, platform


def docker_scan_sources(
    task: str, task_root: Path, dockerfile: Path, text: str
) -> tuple[list[BuildContext], list[ScanSource], list[tuple[str, BuildContext]]]:
    """Bind each RUN and file-reference instruction to its exact stage."""
    contexts: list[BuildContext] = []
    runs: list[ScanSource] = []
    references: list[tuple[str, BuildContext]] = []
    aliases: dict[str, str] = {}
    current: BuildContext | None = None
    relative = dockerfile.relative_to(task_root).as_posix()
    for instruction, body in docker_instructions(text):
        if instruction == "FROM":
            image, alias, platform = from_stage(body)
            if not image:
                current = None
                continue
            base_image = aliases.get(image.lower(), image)
            stage_index = len(contexts)
            context_id = _stable_id(
                "ctx",
                {
                    "task": task,
                    "dockerfile": relative,
                    "stage_index": stage_index,
                    "stage_name": alias,
                    "base_image": base_image,
                    "platform": platform,
                },
            )
            current = BuildContext(
                context_id,
                task,
                relative,
                stage_index,
                alias,
                base_image,
                platform,
            )
            contexts.append(current)
            if alias:
                aliases[alias.lower()] = base_image
            continue
        if current is None:
            continue
        if instruction == "RUN":
            runs.append(ScanSource(dockerfile, body, (current,)))
        if instruction in {"RUN", "ADD", "COPY"}:
            references.append((body, current))
    return contexts, runs, references


def shell_contexts(
    environment: Path,
    scripts: Sequence[Path],
    references: Sequence[tuple[str, BuildContext]],
    all_contexts: Sequence[BuildContext],
) -> dict[Path, tuple[BuildContext, ...]]:
    """Propagate stage ownership through directly referenced shell scripts."""
    owners: dict[Path, set[BuildContext]] = defaultdict(set)
    for script in scripts:
        relative = script.relative_to(environment).as_posix()
        for body, context in references:
            if script.name in body or relative in body:
                owners[script].add(context)
    if len(all_contexts) == 1:
        for script in scripts:
            owners[script].add(all_contexts[0])
    changed = True
    while changed:
        changed = False
        for parent in scripts:
            parent_owners = owners[parent]
            if not parent_owners:
                continue
            text = read_text(parent)
            for child in scripts:
                if child == parent:
                    continue
                relative = child.relative_to(environment).as_posix()
                if child.name not in text and relative not in text:
                    continue
                before = len(owners[child])
                owners[child].update(parent_owners)
                changed = changed or len(owners[child]) != before
    return {
        script: tuple(sorted(owners[script], key=lambda item: item.id))
        for script in scripts
    }

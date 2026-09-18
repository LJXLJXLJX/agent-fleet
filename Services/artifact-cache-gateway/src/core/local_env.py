from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import MutableMapping


_ASSIGNMENT = re.compile(
    r"^\s*(?:export\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*)$"
)


class LocalEnvError(ValueError):
    """Raised when a project-local environment file is malformed."""


def parse_local_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _ASSIGNMENT.fullmatch(raw_line)
        if not match:
            raise LocalEnvError(f"invalid assignment at {path}:{line_number}")
        lexer = shlex.shlex(match.group("value"), posix=True)
        lexer.whitespace_split = True
        lexer.commenters = "#"
        try:
            tokens = list(lexer)
        except ValueError as exc:
            raise LocalEnvError(
                f"invalid quoted value at {path}:{line_number}"
            ) from exc
        if len(tokens) > 1:
            raise LocalEnvError(
                f"unquoted whitespace is not allowed at {path}:{line_number}"
            )
        values[match.group("name")] = tokens[0] if tokens else ""
    return values


def load_local_env(
    path: Path,
    *,
    environ: MutableMapping[str, str] | None = None,
    override: bool = False,
) -> dict[str, str]:
    target = os.environ if environ is None else environ
    values = parse_local_env(path)
    for name, value in values.items():
        if override or name not in target:
            target[name] = value
    return values

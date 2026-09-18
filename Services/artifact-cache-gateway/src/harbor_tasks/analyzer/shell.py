"""Core shell lexing: heredocs, logical lines, command splitting, and command_start (shared foundation)."""

from __future__ import annotations

import re
import shlex
from typing import Iterator, Sequence

SEPARATORS = {";", "&&", "||", "|", "&"}


CONTROL_WORDS = {"if", "then", "elif", "do", "while", "until", "!", "("}


ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=.*$", re.DOTALL)


DOCKER_HEREDOC_RE = re.compile(
    r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1"
)


def unquoted_heredoc_matches(text: str) -> list[re.Match[str]]:
    """Return heredoc operators that are not quoted or backslash-escaped."""

    matches: list[re.Match[str]] = []
    quote: str | None = None
    index = 0
    while index < len(text):
        character = text[index]
        if character == "\\" and quote != "'":
            index += 2
            continue
        if quote is not None:
            if character == quote:
                quote = None
            index += 1
            continue
        if character in {"'", '"'}:
            quote = character
            index += 1
            continue
        match = DOCKER_HEREDOC_RE.match(text, index)
        if match is not None:
            matches.append(match)
            index = match.end()
            continue
        index += 1
    return matches


def strip_heredocs(text: str) -> str:
    """Remove generated file bodies so their text is not counted as build shell."""
    output: list[str] = []
    delimiter: str | None = None
    for line in text.splitlines():
        if delimiter is not None:
            if line.strip() == delimiter:
                delimiter = None
            continue
        output.append(line)
        matches = unquoted_heredoc_matches(line)
        match = matches[0] if matches else None
        if match:
            delimiter = match.group(2)
    return "\n".join(output)


def logical_shell_lines(text: str) -> Iterator[str]:
    pending = ""
    for raw in strip_heredocs(text).splitlines():
        stripped = raw.strip()
        if not pending and (not stripped or stripped.startswith("#")):
            continue
        continued = raw.rstrip().endswith("\\")
        piece = raw.rstrip()[:-1] if continued else raw.rstrip()
        pending = f"{pending} {piece.strip()}".strip()
        if not continued and pending:
            yield pending
            pending = ""
    if pending:
        yield pending


def shell_commands(line: str) -> Iterator[list[str]]:
    try:
        lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|()")
        lexer.whitespace_split = True
        lexer.commenters = "#"
        tokens = list(lexer)
    except ValueError:
        return
    command: list[str] = []
    for token in tokens:
        if (
            token == ")"
            or token in SEPARATORS
            or (token and set(token) <= set(";&|"))
        ):
            if command:
                yield command
                command = []
        else:
            command.append(token)
    if command:
        yield command


def command_start(tokens: Sequence[str]) -> int:
    index = 0
    while index < len(tokens) and tokens[index] in CONTROL_WORDS:
        index += 1
    while index < len(tokens) and ASSIGNMENT_RE.match(tokens[index]):
        index += 1
    if index < len(tokens) and tokens[index] == "env":
        index += 1
        while index < len(tokens) and (tokens[index].startswith("-") or ASSIGNMENT_RE.match(tokens[index])):
            index += 1
    if index < len(tokens) and tokens[index] in {"sudo", "command"}:
        index += 1
        while index < len(tokens) and tokens[index].startswith("-"):
            index += 1
    return index

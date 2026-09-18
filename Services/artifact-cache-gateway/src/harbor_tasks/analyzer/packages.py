"""Recognition of package manager install commands and their resolution options (pip/npm/apt etc.)."""

from __future__ import annotations

import json
import posixpath
import re
from pathlib import Path
from typing import Iterator, Sequence

from .dockerfile import read_text
from .external import sanitize_public_url
from .models import (
    NODE_REGISTRY_MANAGERS,
    Install,
)
from .shell import (
    command_start,
    unquoted_heredoc_matches,
)

SHELL_SUFFIXES = {".sh", ".bash", ".zsh", ".ksh"}


SHELL_REDIRECTION_RE = re.compile(r"^\d*(?:>{1,2}|<{1,2}).*$", re.DOTALL)


def generated_manifests(text: str) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Extract requirements.txt and package.json content created by heredocs."""
    requirements: dict[str, list[str]] = {}
    package_json: dict[str, list[str]] = {}
    lines = text.splitlines()
    redirect_pattern = re.compile(r">\s*(['\"]?)([^\s'\"]+)\1")
    index = 0
    while index < len(lines):
        line = lines[index]
        heredoc_matches = unquoted_heredoc_matches(line)
        heredoc_match = heredoc_matches[0] if heredoc_matches else None
        redirect_match = redirect_pattern.search(line)
        if not heredoc_match or not redirect_match:
            index += 1
            continue
        delimiter = heredoc_match.group(2)
        target = redirect_match.group(2)
        body: list[str] = []
        index += 1
        while index < len(lines) and lines[index].strip() != delimiter:
            body.append(lines[index])
            index += 1
        basename = posixpath.basename(target)
        if "requirements" in basename.lower() and basename.lower().endswith((".txt", ".in")):
            specs = []
            for raw in body:
                spec = raw.split("#", 1)[0].strip()
                if spec and not spec.startswith("-"):
                    specs.append(spec)
            requirements[target] = specs
        elif basename == "package.json":
            try:
                document = json.loads("\n".join(body))
            except json.JSONDecodeError:
                document = None
            if isinstance(document, dict):
                specs = []
                for section in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
                    dependencies = document.get(section, {})
                    if isinstance(dependencies, dict):
                        specs.extend(f"{name}@{version}" for name, version in dependencies.items())
                package_json[target] = specs
        index += 1
    return requirements, package_json


def select_generated_manifest(
    manifests: dict[str, list[str]], requested: str | None = None, cwd: str | None = None
) -> list[str] | None:
    if requested:
        normalized = posixpath.normpath(requested)
        exact = [specs for path, specs in manifests.items() if posixpath.normpath(path) == normalized]
        if len(exact) == 1:
            return exact[0]
        basename = posixpath.basename(normalized)
        matching = [specs for path, specs in manifests.items() if posixpath.basename(path) == basename]
        if len(matching) == 1:
            return matching[0]
    if cwd:
        normalized_cwd = posixpath.normpath(cwd)
        matching = [
            specs for path, specs in manifests.items()
            if posixpath.normpath(posixpath.dirname(path)) == normalized_cwd
        ]
        if len(matching) == 1:
            return matching[0]
    if len(manifests) == 1:
        return next(iter(manifests.values()))
    return None


def identify_install(tokens: Sequence[str]) -> Install | None:
    index = command_start(tokens)
    if index >= len(tokens):
        return None
    executable = tokens[index].rsplit("/", 1)[-1].lower()
    rest = list(tokens[index + 1 :])
    python_match = re.fullmatch(r"(?:python|pypy)(?P<version>[23](?:\.\d+)?)?", executable)
    if python_match:
        if len(rest) >= 3 and rest[0] == "-m" and rest[1] in {"pip", "pip3"} and rest[2] == "install":
            version = python_match.group("version")
            return Install(
                "pip",
                tuple(rest[3:]),
                "python -m pip install",
                version if version and "." in version else None,
            )
        return None
    pip_match = re.fullmatch(r"pip(?P<version>[23](?:\.\d+)?)?", executable)
    if pip_match and rest[:1] == ["install"]:
        version = pip_match.group("version")
        return Install(
            "pip",
            tuple(rest[1:]),
            f"{executable} install",
            version if version and "." in version else None,
        )
    if executable == "pipx" and rest[:1] == ["install"]:
        return Install("pipx", tuple(rest[1:]), "pipx install")
    if executable == "uv":
        if rest[:2] == ["pip", "install"]:
            return Install("pip", tuple(rest[2:]), "uv pip install")
        if rest[:1] == ["add"]:
            return Install("uv", tuple(rest[1:]), "uv add")
    command_map = {
        "apt": ({"install"}, "apt"), "apt-get": ({"install"}, "apt"),
        "apk": ({"add"}, "apk"), "dnf": ({"install"}, "dnf"),
        "microdnf": ({"install"}, "dnf"), "yum": ({"install"}, "yum"),
        "zypper": ({"install", "in"}, "zypper"), "brew": ({"install"}, "brew"),
        "conda": ({"install"}, "conda"), "mamba": ({"install"}, "conda"),
        "micromamba": ({"install"}, "conda"),
        "npm": ({"install", "i", "add", "ci"}, "npm"),
        "pnpm": ({"install", "i", "add"}, "pnpm"),
        "yarn": ({"add", "install"}, "yarn"), "bun": ({"add", "install"}, "bun"),
        "cargo": ({"install", "add"}, "cargo"), "gem": ({"install"}, "gem"),
        "go": ({"install", "get"}, "go"), "composer": ({"require"}, "composer"),
        "luarocks": ({"install"}, "luarocks"), "pear": ({"install"}, "pear"),
    }
    if executable in command_map and rest:
        subcommands, manager = command_map[executable]
        if rest[0] in subcommands:
            return Install(manager, tuple(rest[1:]), f"{executable} {rest[0]}")
    if executable == "pacman" and any(re.fullmatch(r"-[A-Za-z]*S[A-Za-z]*", item) for item in rest):
        return Install("pacman", tuple(rest), "pacman -S")
    if executable == "dotnet" and rest[:2] == ["add", "package"]:
        return Install("dotnet", tuple(rest[2:]), "dotnet add package")
    if executable == "dotnet" and rest[:2] == ["tool", "install"]:
        return Install("dotnet-tool", tuple(rest[2:]), "dotnet tool install")
    return None


OPTION_VALUES = {
    "pip": {"-c", "--constraint", "-e", "--editable", "-f", "--find-links", "-i", "--index-url",
            "--extra-index-url", "--trusted-host", "-t", "--target", "--platform", "--python-version",
            "--implementation", "--abi", "--root", "--prefix", "--src", "--cache-dir", "--timeout",
            "--retries", "--proxy", "--config-settings"},
    "apt": {"-o", "--option", "-t", "--target-release"},
    "apk": {"-X", "--repository", "--arch", "--root", "--keys-dir"},
    "dnf": {"--releasever", "--installroot", "--setopt", "--exclude"},
    "yum": {"--releasever", "--installroot", "--setopt", "--exclude"},
    "conda": {"-n", "--name", "-p", "--prefix", "-c", "--channel", "--file"},
    "npm": {"--prefix", "--registry", "--cache", "--userconfig", "--workspace", "-w"},
    "pnpm": {"--dir", "-C", "--registry", "--filter"},
    "yarn": {"--cwd", "--registry", "--cache-folder", "--modules-folder"},
    "bun": {"--cwd", "--registry", "--cache-dir"},
    "cargo": {"-F", "--features", "--version", "--git", "--branch", "--tag", "--rev", "--path"},
    "gem": {"-v", "--version", "--source", "--platform", "--install-dir"},
    "composer": {"--working-dir", "-d", "--repository", "--stability"},
    "pacman": {"--root", "--dbpath", "--cachedir", "--config", "--arch"},
    "zypper": {"-r", "--repo", "-t", "--type", "--root"},
}


def argument_specs(install: Install) -> tuple[list[str], list[str]]:
    specs: list[str] = []
    requirements: list[str] = []
    args = list(install.arguments)
    value_options = OPTION_VALUES.get(install.manager, set())
    index = 0
    while index < len(args):
        token = args[index]
        if install.manager == "pip" and token in {"-r", "--requirement"}:
            if index + 1 < len(args):
                requirements.append(args[index + 1])
            index += 2
            continue
        if install.manager == "pip" and token.startswith("-r") and token != "-r":
            requirements.append(token[2:])
            index += 1
            continue
        if token in value_options:
            index += 2
            continue
        if SHELL_REDIRECTION_RE.match(token):
            index += 1
            continue
        if token.startswith("-") or token in {"true", "false"} or token.startswith((">", "<")):
            index += 1
            continue
        if "$" in token or token.startswith(("/", "./", "../", "http://", "https://", "git+")):
            index += 1
            continue
        specs.append(token.rstrip(","))
        index += 1
    return specs, requirements


def pip_python_version(install: Install) -> str | None:
    if install.manager != "pip":
        return None
    args = list(install.arguments)
    for index, token in enumerate(args):
        if token == "--python-version" and index + 1 < len(args):
            return args[index + 1]
        if token.startswith("--python-version="):
            return token.split("=", 1)[1]
    return install.python_version


def pip_resolution_options(install: Install) -> dict[str, object]:
    """Return resolver-affecting pip flags without leaking URL credentials."""
    if install.manager != "pip":
        return {}
    aliases = {"-i": "index_url"}
    options = {
        "--index-url": "index_url",
        "--extra-index-url": "extra_index_url",
        "--platform": "platform",
        "--python-version": "python_version",
        "--implementation": "implementation",
        "--abi": "abi",
    }
    values: dict[str, object] = {}
    args = list(install.arguments)
    index = 0
    while index < len(args):
        token = args[index]
        name: str | None = None
        value: str | None = None
        if token in aliases or token in options:
            name = aliases.get(token) or options[token]
            if index + 1 < len(args):
                value = args[index + 1]
            index += 2
        elif token.startswith("--") and "=" in token:
            option, value = token.split("=", 1)
            name = options.get(option)
            index += 1
        else:
            index += 1
        if not name or value is None:
            continue
        if name in {"index_url", "extra_index_url"}:
            sanitized = sanitize_public_url(value)
            if sanitized is None:
                # Variables and malformed URLs affect resolution, but their raw
                # value may contain credentials and must not enter reports.
                values[name] = "<dynamic-or-redacted>"
            else:
                values[name] = sanitized[0]
        else:
            values[name] = value
    return values


def npm_resolution_options(install: Install) -> dict[str, object]:
    """Return registry selection without making the base image part of identity."""
    if install.manager not in NODE_REGISTRY_MANAGERS:
        return {}
    args = list(install.arguments)
    registry: str | None = None
    for index, token in enumerate(args):
        if token == "--registry" and index + 1 < len(args):
            registry = args[index + 1]
        elif token.startswith("--registry="):
            registry = token.split("=", 1)[1]
    if registry is None:
        return {}
    sanitized = sanitize_public_url(registry)
    return {
        "registry": sanitized[0]
        if sanitized is not None
        else "<dynamic-or-redacted>"
    }


def package_resolution_options(install: Install) -> dict[str, object]:
    if install.manager == "pip":
        return pip_resolution_options(install)
    if install.manager in NODE_REGISTRY_MANAGERS:
        return npm_resolution_options(install)
    return {}


def normalize_package(manager: str, spec: str) -> str | None:
    spec = spec.strip().strip("'\"")
    if not spec or spec in {".", "..", "*"}:
        return None
    if manager in {"pip", "pipx", "uv"}:
        if spec.startswith("."):
            return None
        name = re.split(r"[<>=!~;\[]", spec, maxsplit=1)[0]
        name = re.sub(r"[-_.]+", "-", name).lower()
    elif manager in {"apt", "apk", "dnf", "yum", "zypper", "pacman", "conda"}:
        name = re.split(r"[<>=!~]", spec, maxsplit=1)[0].lower()
        name = re.sub(r":[A-Za-z0-9_-]+$", "", name)
    elif manager in {"npm", "pnpm", "yarn", "bun"}:
        if spec.startswith("@"):
            match = re.match(r"^(@[^/]+/[^@]+)", spec)
            name = match.group(1) if match else spec
        else:
            name = spec.split("@", 1)[0]
        name = name.lower()
    elif manager == "go":
        name = spec.rsplit("@", 1)[0]
    elif manager in {"cargo", "gem"}:
        name = re.split(r"[@<>=!~]", spec, maxsplit=1)[0].lower()
    elif manager == "composer":
        name = re.split(r"[:<>=!~^]", spec, maxsplit=1)[0].lower()
    else:
        name = re.split(r"[<>=!~@]", spec, maxsplit=1)[0].lower()
    return name or None


def requirement_candidates(environment: Path, source: Path, requested: str) -> list[Path]:
    requested_path = Path(requested)
    possible = [source.parent / requested_path, environment / requested_path]
    if requested_path.name:
        possible.extend(environment.rglob(requested_path.name))
    result: list[Path] = []
    seen: set[Path] = set()
    for candidate in possible:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved not in seen and resolved.is_file():
            seen.add(resolved)
            result.append(resolved)
    return result


def parse_requirements(path: Path, seen: set[Path] | None = None) -> Iterator[str]:
    seen = set() if seen is None else seen
    try:
        resolved = path.resolve()
    except OSError:
        return
    if resolved in seen:
        return
    seen.add(resolved)
    for raw in read_text(path).splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith(("-r ", "--requirement ")):
            yield from parse_requirements(path.parent / line.split(maxsplit=1)[1], seen)
        elif not line.startswith("-"):
            yield line


def shell_files(environment: Path, docker_texts: Sequence[str], scope: str) -> list[Path]:
    if scope == "none":
        return []
    candidates = [p for p in environment.rglob("*") if p.is_file() and p.suffix.lower() in SHELL_SUFFIXES]
    if scope == "all":
        return sorted(candidates)
    haystacks = list(docker_texts)
    selected: set[Path] = set()
    changed = True
    while changed:
        changed = False
        combined = "\n".join(haystacks)
        for path in candidates:
            if path in selected:
                continue
            relative = path.relative_to(environment).as_posix()
            if path.name in combined or relative in combined:
                selected.add(path)
                haystacks.append(read_text(path))
                changed = True
    return sorted(selected)

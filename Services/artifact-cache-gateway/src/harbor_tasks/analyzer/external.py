"""Static recognition of external build inputs (direct download URLs, git clone)."""

from __future__ import annotations

import ipaddress
import posixpath
import re
from typing import Iterator, Sequence
from urllib.parse import urlsplit, urlunsplit

from ..preparer.direct_download import classify_refresh_policy
from .models import (
    ExternalBuildInput,
    ExternalBuildInputIssue,
)
from .shell import command_start


def sanitize_public_url(value: str) -> tuple[str, str, bool] | None:
    raw = value.rstrip(".,;:)]}\\")
    try:
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not host:
        return None
    netloc = f"{host}:{port}" if port else host
    sanitized = urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", "", ""))
    return sanitized, host, bool(parsed.query)


def _sanitize_git_url(value: str) -> tuple[str, str, bool, str, bool] | None:
    raw = value.strip().rstrip(".,;:)]}\\")
    if not raw or "$" in raw or "{" in raw or "}" in raw:
        return None
    public = sanitize_public_url(raw)
    if public is not None:
        url, host, query_present = public
        original = urlsplit(raw)
        credentials_present = (
            original.username is not None or original.password is not None
        )
        return url, host, query_present, urlsplit(url).scheme, credentials_present
    scp_style = re.fullmatch(
        r"(?:[^@/\s]+@)?(?P<host>[A-Za-z0-9.-]+):(?P<path>[^\s]+)", raw
    )
    if scp_style:
        host = scp_style.group("host").lower()
        path = scp_style.group("path").lstrip("/")
        return f"ssh://{host}/{path}", host, False, "ssh", False
    try:
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"git", "ssh"} or not host:
        return None
    netloc = f"{host}:{port}" if port else host
    sanitized = urlunsplit(
        (parsed.scheme.lower(), netloc, parsed.path or "/", "", "")
    )
    credentials_present = parsed.username is not None or parsed.password is not None
    return (
        sanitized,
        host,
        bool(parsed.query),
        parsed.scheme.lower(),
        credentials_present,
    )


def _git_clone_target(arguments: Sequence[str]) -> tuple[str | None, str | None]:
    value_options = {
        "-b",
        "--branch",
        "--depth",
        "--filter",
        "--jobs",
        "-j",
        "--origin",
        "-o",
        "--reference",
        "--reference-if-able",
        "--separate-git-dir",
        "--server-option",
        "--template",
        "--upload-pack",
        "-u",
        "--config",
        "-c",
    }
    reference: str | None = None
    index = 0
    while index < len(arguments):
        token = arguments[index]
        if token == "--":
            return (
                arguments[index + 1] if index + 1 < len(arguments) else None,
                reference,
            )
        if token in value_options:
            if index + 1 < len(arguments) and token in {"-b", "--branch"}:
                reference = arguments[index + 1]
            index += 2
            continue
        if token.startswith("--branch="):
            reference = token.split("=", 1)[1]
            index += 1
            continue
        if token.startswith("-b") and token != "-b":
            reference = token[2:]
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token, reference
    return None, reference


def _git_clone_destination(tokens: Sequence[str]) -> str | None:
    start = command_start(tokens)
    if start >= len(tokens) or tokens[start].rsplit("/", 1)[-1] != "git":
        return None
    arguments = list(tokens[start + 1 :])
    if "clone" not in arguments:
        return None
    clone_index = arguments.index("clone")
    clone_arguments = arguments[clone_index + 1 :]
    value_options = {
        "-b", "--branch", "--depth", "--filter", "--jobs", "-j",
        "--origin", "-o", "--reference", "--reference-if-able",
        "--separate-git-dir", "--server-option", "--template",
        "--upload-pack", "-u", "--config", "-c",
    }
    positional: list[str] = []
    index = 0
    while index < len(clone_arguments):
        token = clone_arguments[index]
        if token == "--":
            positional.extend(clone_arguments[index + 1 :])
            break
        if token in value_options:
            index += 2
            continue
        if token.startswith("--") and "=" in token:
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        positional.append(token)
        index += 1
    if len(positional) >= 2:
        return positional[1]
    if not positional:
        return None
    path = urlsplit(positional[0]).path.rstrip("/")
    name = path.rsplit("/", 1)[-1]
    return name[:-4] if name.lower().endswith(".git") else name


def _git_checkout_reference(tokens: Sequence[str]) -> str | None:
    start = command_start(tokens)
    if start >= len(tokens) or tokens[start].rsplit("/", 1)[-1] != "git":
        return None
    arguments = list(tokens[start + 1 :])
    if not arguments:
        return None
    if arguments[0] == "reset" and "--hard" in arguments:
        candidate = arguments[-1]
    elif arguments[0] == "checkout":
        candidates = [item for item in arguments[1:] if not item.startswith("-")]
        candidate = candidates[-1] if candidates else ""
    elif arguments[:2] == ["switch", "--detach"] and len(arguments) >= 3:
        candidate = arguments[-1]
    else:
        return None
    return candidate if re.fullmatch(r"[0-9a-fA-F]{7,40}", candidate) else None


def _shell_path(current_directory: str | None, value: str) -> str:
    if value.startswith("/"):
        return posixpath.normpath(value)
    return posixpath.normpath(posixpath.join(current_directory or "/", value))


def _download_url_tokens(
    executable: str, arguments: Sequence[str]
) -> Iterator[str]:
    """Yield explicit transfer targets without treating option values as downloads."""

    non_target_value_options = {
        "curl": {
            "-A", "--user-agent", "--cacert", "--cert", "--connect-to",
            "-d", "--data", "--data-ascii", "--data-binary", "--data-raw",
            "-e", "--referer", "-F", "--form", "-H", "--header", "--key",
            "-o", "--output", "-x", "--proxy", "-U", "--proxy-user",
            "--resolve", "-u", "--user",
        },
        "wget": {
            "--ca-certificate", "--certificate", "--execute", "-e", "--header",
            "-O", "--output-document", "--password", "--post-data", "--post-file",
            "--proxy-password", "--proxy-user", "--referer", "--user",
            "-U", "--user-agent",
        },
    }[executable]
    index = 0
    while index < len(arguments):
        token = arguments[index]
        if token == "--":
            for target in arguments[index + 1 :]:
                if target.lower().startswith(("http://", "https://")):
                    yield target
            return
        if executable == "curl" and token == "--url":
            if index + 1 < len(arguments):
                yield arguments[index + 1]
            index += 2
            continue
        if executable == "curl" and token.startswith("--url="):
            yield token.split("=", 1)[1]
            index += 1
            continue
        if token in non_target_value_options:
            index += 2
            continue
        if any(token.startswith(f"{option}=") for option in non_target_value_options):
            index += 1
            continue
        if not token.startswith("-") and token.lower().startswith(
            ("http://", "https://")
        ):
            yield token
        index += 1


def _host_needs_review(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return not address.is_global


def external_build_inputs(tokens: Sequence[str]) -> Iterator[ExternalBuildInput]:
    start = command_start(tokens)
    if start >= len(tokens):
        return
    executable = tokens[start].rsplit("/", 1)[-1].lower()
    arguments = list(tokens[start + 1 :])
    if executable in {"curl", "wget"}:
        command_is_dynamic = any(
            any(marker in argument for marker in ("$", "{", "}", "`"))
            for argument in arguments
        )
        for token in _download_url_tokens(executable, arguments):
            # shlex does not preserve command substitutions as one token.  If
            # any argument is dynamic, URL-looking fragments from the same
            # curl/wget invocation must not become exact cache allowlists.
            dynamic = command_is_dynamic or any(
                marker in token for marker in ("$", "{", "}", "`")
            )
            sanitized = sanitize_public_url(token)
            if sanitized is None:
                continue
            url, host, query_present = sanitized
            original = urlsplit(token.strip().rstrip(".,;:)]}\\"))
            credentials_present = (
                original.username is not None or original.password is not None
            )
            needs_review = (
                dynamic
                or query_present
                or credentials_present
                or _host_needs_review(host)
            )
            yield ExternalBuildInput(
                url=url,
                host=host,
                kind="http-download",
                tool=executable,
                action="review" if needs_review else "cache",
                cache_mode=None if needs_review else "http-static-object",
                refresh_policy=classify_refresh_policy(url),
                reason=(
                    "URL contains shell expansion and requires runtime resolution before "
                    "an exact cache allowlist can be generated"
                    if dynamic
                    else "URL contains credentials; report strips them and requires explicit review"
                    if credentials_present
                    else "URL contains a query; report strips it and source configuration "
                    "requires explicit review"
                    if query_present
                    else "URL targets a loopback or non-global IP address and must not enter a shared source allowlist"
                    if needs_review
                    else "explicit build-time download should use a path-allowlisted static object source"
                ),
                query_present=query_present,
                credentials_present=credentials_present,
            )
        return
    if executable != "git" or "clone" not in arguments:
        return
    clone_index = arguments.index("clone")
    target, reference = _git_clone_target(arguments[clone_index + 1 :])
    if target is None:
        return
    sanitized_git = _sanitize_git_url(target)
    if sanitized_git is None:
        return
    url, host, query_present, transport, credentials_present = sanitized_git
    supported_transport = transport in {"http", "https"}
    needs_review = not supported_transport or query_present or credentials_present
    yield ExternalBuildInput(
        url=url,
        host=host,
        kind="git-repository",
        tool="git",
        action="review" if needs_review else "mirror",
        cache_mode="git-smart-http" if supported_transport and not needs_review else None,
        refresh_policy="reference-aware-manual",
        reason=(
            "Git URL contains credentials or a query; report strips them and requires explicit review"
            if credentials_present or query_present
            else
            "HTTP(S) Git clone requires a Git-aware mirror; file-object caching is insufficient"
            if supported_transport
            else f"{transport} Git transport requires an explicit rewrite to an approved HTTP(S) mirror"
        ),
        query_present=query_present,
        credentials_present=credentials_present,
        reference=reference,
    )


def external_build_input_issues(
    tokens: Sequence[str], inputs: Sequence[ExternalBuildInput]
) -> Iterator[ExternalBuildInputIssue]:
    if inputs:
        return
    start = command_start(tokens)
    if start >= len(tokens):
        return
    executable = tokens[start].rsplit("/", 1)[-1].lower()
    arguments = list(tokens[start + 1 :])
    if executable in {"curl", "wget"}:
        if any("$" in token or "${" in token for token in arguments):
            yield ExternalBuildInputIssue(
                kind="http-download",
                tool=executable,
                reason="download target is dynamic; no URL or command text is persisted",
            )
        return
    if executable != "git" or "clone" not in arguments:
        return
    clone_index = arguments.index("clone")
    target, _reference = _git_clone_target(arguments[clone_index + 1 :])
    if target and target.startswith(("/", "./", "../", "file://")):
        return
    if target is None or _sanitize_git_url(target) is None:
        yield ExternalBuildInputIssue(
            kind="git-repository",
            tool="git",
            reason="clone target is dynamic or unsupported; no URL or command text is persisted",
        )

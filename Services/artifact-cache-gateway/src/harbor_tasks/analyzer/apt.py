"""Static recognition of APT dependencies and repository declarations."""

from __future__ import annotations

import re
from typing import Iterator
from urllib.parse import urlsplit

from .external import sanitize_public_url
from .models import AptDependency
from .shell import logical_shell_lines

URL_RE = re.compile(r"https?://[^\s'\"<>|]+", re.IGNORECASE)


APT_CONTEXT_RE = re.compile(
    r"\bapt(?:-get|-key)?\b|\bdpkg\b|sources\.list|\.sources\b|"
    r"keyrings?|\bgpg\b|add-apt-repository",
    re.IGNORECASE,
)


OFFICIAL_DISTRIBUTION_APT_HOSTS = {
    "archive.ubuntu.com",
    "security.ubuntu.com",
    "ports.ubuntu.com",
    "old-releases.ubuntu.com",
    "deb.debian.org",
    "security.debian.org",
    "snapshot.debian.org",
    "archive.debian.org",
}


KNOWN_DOMESTIC_APT_MIRROR_HOSTS = {
    "mirrors.tuna.tsinghua.edu.cn",
    "mirrors.aliyun.com",
    "mirrors.cloud.tencent.com",
    "mirrors.ustc.edu.cn",
    "mirror.sjtu.edu.cn",
    "repo.huaweicloud.com",
}


def apt_dependencies(text: str) -> Iterator[AptDependency]:
    for line in logical_shell_lines(text):
        for owner, archive in re.findall(
            r"\bppa:([a-z0-9][a-z0-9+.-]*)/([a-z0-9][a-z0-9+.-]*)\b",
            line,
            re.IGNORECASE,
        ):
            url = (
                "https://ppa.launchpadcontent.net/"
                f"{owner.lower()}/{archive.lower()}/ubuntu"
            )
            yield AptDependency(
                url=url,
                host="ppa.launchpadcontent.net",
                kind="repository",
                coverage="not-covered-by-distribution-mirror",
                action="cache",
                reason="explicit Launchpad PPA is outside distribution APT mirrors",
                query_present=False,
            )
        for segment in re.split(r"\s*(?:&&|\|\||;)\s*", line):
            apt_context = bool(APT_CONTEXT_RE.search(segment))
            line_lower = segment.lower()
            for match in URL_RE.finditer(segment):
                raw_url = match.group(0)
                sanitized = sanitize_public_url(raw_url)
                if sanitized is None:
                    if apt_context:
                        yield AptDependency(
                            url="<unresolved-url>",
                            host="<unresolved>",
                            kind="unresolved",
                            coverage="needs-validation",
                            action="validate",
                            reason="APT-related URL could not be parsed safely",
                            query_present="?" in raw_url,
                        )
                    continue
                url, host, query_present = sanitized
                path_lower = urlsplit(url).path.lower()
                nodesource_setup = (
                    re.fullmatch(r"/setup_([0-9]+)\.x", path_lower)
                    if host == "deb.nodesource.com"
                    else None
                )
                is_deb = path_lower.endswith(".deb")
                is_repository = bool(
                    re.search(
                        r"(?:^|[\s'\"])(?:deb|deb-src)\s",
                        segment,
                        re.IGNORECASE,
                    )
                    or "sources.list" in line_lower
                    or re.search(r"\buris\s*:", segment, re.IGNORECASE)
                    or "add-apt-repository" in line_lower
                )
                is_key = bool(
                    "apt-key" in line_lower
                    or "keyring" in line_lower
                    or re.search(r"\bgpg\b", segment, re.IGNORECASE)
                    or path_lower.endswith((".gpg", ".asc", ".pub", ".key"))
                )
                if (
                    not apt_context
                    and not is_deb
                    and not is_repository
                    and not is_key
                    and not nodesource_setup
                ):
                    continue
                if "$" in raw_url or "{" in raw_url or "}" in raw_url:
                    kind = "unresolved"
                    coverage = "needs-validation"
                    action = "validate"
                    reason = "APT URL contains an unresolved shell variable"
                elif nodesource_setup:
                    kind = "apt-bootstrap"
                    coverage = "not-covered-by-distribution-mirror"
                    action = "cache"
                    reason = (
                        "NodeSource setup script installs a third-party key and APT source"
                    )
                elif is_deb:
                    kind = "deb-artifact"
                    coverage = "not-covered-by-distribution-mirror"
                    action = "cache"
                    reason = "direct .deb downloads are outside distribution APT mirrors"
                elif is_repository:
                    kind = "repository"
                    if host in KNOWN_DOMESTIC_APT_MIRROR_HOSTS:
                        coverage = "domestic-mirror"
                        action = "none"
                        reason = "already uses a known domestic distribution mirror"
                    elif host in OFFICIAL_DISTRIBUTION_APT_HOSTS:
                        coverage = "domestic-mirror-replaceable"
                        action = "rewrite"
                        reason = "official distribution repository can be replaced by a domestic mirror"
                    else:
                        coverage = "not-covered-by-distribution-mirror"
                        action = "cache"
                        reason = "third-party repository is not mirrored by Ubuntu/Debian mirrors"
                elif is_key:
                    kind = "signing-key"
                    coverage = "not-covered-by-distribution-mirror"
                    action = "cache"
                    reason = "repository signing keys are separate bootstrap artifacts"
                else:
                    kind = "apt-bootstrap"
                    coverage = "needs-validation"
                    action = "validate"
                    reason = "APT-related download needs runtime source validation"
                yield AptDependency(
                    url=url,
                    host=host,
                    kind=kind,
                    coverage=coverage,
                    action=action,
                    reason=reason,
                    query_present=query_present,
                )
                if nodesource_setup:
                    version = nodesource_setup.group(1)
                    yield AptDependency(
                        url="https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key",
                        host="deb.nodesource.com",
                        kind="signing-key",
                        coverage="not-covered-by-distribution-mirror",
                        action="cache",
                        reason="NodeSource setup script downloads this repository key",
                        query_present=False,
                    )
                    yield AptDependency(
                        url=f"https://deb.nodesource.com/node_{version}.x",
                        host="deb.nodesource.com",
                        kind="repository",
                        coverage="not-covered-by-distribution-mirror",
                        action="cache",
                        reason="NodeSource setup script installs this third-party repository",
                        query_present=False,
                    )


def apt_repository_declarations(text: str) -> list[dict[str, object]]:
    """Parse explicit third-party repository declarations from one shell line."""

    normalized = re.sub(
        r"\$\(\s*lsb_release\s+-(?:cs|sc)\s*\)",
        "$CODENAME",
        text,
        flags=re.IGNORECASE,
    )
    normalized = re.sub(
        r"\$(?:\{VERSION_CODENAME\}|VERSION_CODENAME\b)",
        "$CODENAME",
        normalized,
    )
    declarations: list[dict[str, object]] = []
    for owner, archive in re.findall(
        r"\bppa:([a-z0-9][a-z0-9+.-]*)/([a-z0-9][a-z0-9+.-]*)\b",
        normalized,
        re.IGNORECASE,
    ):
        declarations.append(
            {
                "upstream_url": (
                    "https://ppa.launchpadcontent.net/"
                    f"{owner.lower()}/{archive.lower()}/ubuntu"
                ),
                "suite": "$CODENAME",
                "components": ["main"],
            }
        )
    pattern = re.compile(
        r"(?:^|[\s'\"])(?:deb|deb-src)\s+"
        r"(?:\[[^\]]+\]\s+)?"
        r"(?P<url>https?://[^\s'\"]+)\s+"
        r"(?P<suite>[^\s'\"]+)\s+"
        r"(?P<components>[^'\";&|]+)",
        re.IGNORECASE,
    )
    for match in pattern.finditer(normalized):
        sanitized = sanitize_public_url(match.group("url"))
        if sanitized is None:
            continue
        url, host, query_present = sanitized
        if (
            query_present
            or host in KNOWN_DOMESTIC_APT_MIRROR_HOSTS
            or host in OFFICIAL_DISTRIBUTION_APT_HOSTS
        ):
            continue
        components = [
            token
            for token in match.group("components").split()
            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+./_-]*", token)
        ]
        if not components:
            continue
        declarations.append(
            {
                "upstream_url": url.rstrip("/"),
                "suite": match.group("suite"),
                "components": components,
            }
        )
    unique: dict[tuple[str, str, tuple[str, ...]], dict[str, object]] = {}
    for row in declarations:
        identity = (
            str(row["upstream_url"]),
            str(row["suite"]),
            tuple(str(item) for item in row["components"]),
        )
        unique[identity] = row
    return list(unique.values())

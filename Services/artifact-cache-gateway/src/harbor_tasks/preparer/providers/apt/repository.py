"""APT repository context resolution (gateway source, deb822 record reading)."""

from __future__ import annotations

import bz2
import gzip
import lzma
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import ProxyHandler, Request, build_opener

from .....gateway.services.apt import repository_source_name
from ...models import AptEnvironment
from .resolver import _record_matches_package


class _RepositoryMixin:
    @staticmethod
    def _gateway_source(site: str, environment: AptEnvironment) -> str:
        if "/apt-repo-" in site:
            return site.rstrip("/").rsplit("/", 1)[-1]
        if "/debian-security-upstream" in site:
            return "debian-security"
        if environment.distro == "debian":
            return "debian"
        return "ubuntu"

    @staticmethod
    def _deb822_records(content: bytes, encoding: str) -> list[dict[str, str]]:
        if encoding == "xz":
            decoded = lzma.decompress(content)
        elif encoding == "gz":
            decoded = gzip.decompress(content)
        elif encoding == "bz2":
            decoded = bz2.decompress(content)
        else:
            decoded = content
        records: list[dict[str, str]] = []
        for block in decoded.decode("utf-8", errors="replace").split("\n\n"):
            fields: dict[str, str] = {}
            for line in block.splitlines():
                if ": " in line and not line.startswith((" ", "\t")):
                    name, value = line.split(": ", 1)
                    fields[name] = value
            if fields:
                records.append(fields)
        return records

    @staticmethod
    def _gateway_read(url: str, timeout_seconds: float) -> bytes:
        opener = build_opener(ProxyHandler({}))
        request = Request(url, method="GET")
        with opener.open(request, timeout=timeout_seconds) as response:
            return response.read()

    def _resolve_repository_context(
        self,
        package: str,
        environment: AptEnvironment,
        context: dict[str, object],
        *,
        gateway_url: str,
        timeout_seconds: float,
    ) -> dict[str, object] | None:
        upstream_url = str(context.get("upstream_url", "")).rstrip("/")
        suite = str(context.get("suite", "")).replace(
            "$CODENAME", environment.codename
        )
        raw_components = context.get("components", [])
        components = (
            [str(item) for item in raw_components]
            if isinstance(raw_components, list)
            else []
        )
        if not upstream_url or not suite or "$" in suite or not components:
            return None
        source = repository_source_name(upstream_url)
        root = f"{gateway_url.rstrip('/')}/{source}"
        encoded_suite = quote(suite, safe="/+~._-")
        # Cache signed top-level metadata when the repository publishes it.
        try:
            self._gateway_read(
                f"{root}/dists/{encoded_suite}/InRelease", timeout_seconds
            )
        except (HTTPError, URLError, TimeoutError, OSError):
            pass
        for component in components:
            encoded_component = quote(component, safe="+~._-")
            base = (
                f"{root}/dists/{encoded_suite}/{encoded_component}/"
                f"binary-{environment.architecture}/Packages"
            )
            for suffix, encoding in (
                (".xz", "xz"),
                (".gz", "gz"),
                (".bz2", "bz2"),
                ("", "plain"),
            ):
                try:
                    content = self._gateway_read(base + suffix, timeout_seconds)
                    records = self._deb822_records(content, encoding)
                except HTTPError:
                    # Some repositories return an upstream 5xx for unsupported
                    # compression variants instead of a clean 404.  Each path
                    # is still constrained by the reviewed Gateway source, so
                    # continue through the fixed format list.
                    continue
                except (URLError, TimeoutError, OSError, EOFError, lzma.LZMAError):
                    break
                for record in records:
                    if not _record_matches_package(record, package) or not record.get(
                        "Filename"
                    ):
                        continue
                    size = record.get("Size", "")
                    return {
                        "state": "resolved",
                        "version": record.get("Version"),
                        "filename": record["Filename"],
                        "sha256": record.get("SHA256"),
                        "size": int(size) if size.isdigit() else None,
                        "site": root,
                    }
                break
        return None

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import unquote, urlsplit, urlunsplit

from ..ecosystems import handler_for_kind
from ..ecosystems.node import _NPM_PATH_COMPONENT
from ..ecosystems.pip import (
    _PYTORCH_PACKAGE_INDEX,
    _PYTORCH_ROOT_ARTIFACT,
)
from ..source_naming import decode_frozen_download_relative_path
from ._shared import _origin
from .errors import ConfigError
from .upstream import (
    _PROXY_MODES,
    UpstreamConfig,
    _upstream_from_dict,
    _upstream_policy,
)

_SOURCE_NAME = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
_SOURCE_KINDS = {
    "generic",
    "apt-repository",
    "cargo-crates",
    "cargo-sparse",
    "dart-pub",
    "frozen-download",
    "transparent-download",
    "go-proxy",
    "go-sumdb",
    "julia-pkg",
    "npm-registry",
    "pytorch-wheels",
    "static-objects",
}
_SOURCE_ECOSYSTEMS = {
    "apt",
    "pip",
    "node",
    "go",
    "cargo",
    "dart",
    "julia",
    "download",
    "generic",
}
_CONFIG_UPDATE_POLICIES = {"manual", "expires"}

def _safe_relative_path(
    value: object, *, prefix: bool = False, exact: bool = False
) -> str:
    path = unquote(str(value)).lstrip("/")
    segments = path.split("/")
    if (
        not path
        or "\\" in path
        or "\x00" in path
        or any(segment in {".", ".."} for segment in segments)
        or posixpath.normpath(path) != path.rstrip("/")
    ):
        raise ConfigError(f"invalid allowlist path: {value!r}")
    if prefix:
        return path.rstrip("/") + "/"
    if exact and path.endswith("/"):
        raise ConfigError(f"exact allowlist path cannot end with /: {value!r}")
    return path


def _path_list(
    value: dict[str, object], name: str, *, prefix: bool = False
) -> tuple[str, ...]:
    configured = value.get(name, [])
    if not isinstance(configured, list):
        raise ConfigError(f"{name} must be an array")
    return tuple(
        dict.fromkeys(
            _safe_relative_path(item, prefix=prefix, exact=not prefix)
            for item in configured
        )
    )

@dataclass(frozen=True)
class SourceConfig:
    name: str
    base_url: str
    allowed_redirect_origins: frozenset[str]
    html_rewrite_origins: frozenset[str]
    html_rewrite_url_prefixes: tuple[str, ...] = ()
    html_rewrite_routes: tuple[tuple[str, str], ...] = ()
    html_rewrite_relative_routes: tuple[tuple[str, str], ...] = ()
    rewrite_html: bool = False
    kind: str = "generic"
    ecosystem: str = "generic"
    allowed_path_prefixes: tuple[str, ...] = ()
    allowed_exact_paths: frozenset[str] = frozenset()
    mutable_path_prefixes: tuple[str, ...] = ()
    mutable_exact_paths: frozenset[str] = frozenset()
    flat_index_packages: frozenset[str] = frozenset()
    root_artifact_packages: frozenset[str] = frozenset()
    metadata_ttl_seconds: float | None = None
    allow_query: bool = True
    proxy_mode: str = "configured"
    upstream_label: str = "primary"
    attempt_timeout_seconds: float | None = None
    slow_after_seconds: float | None = None
    min_bytes_per_second: float | None = None
    fallback_upstreams: tuple[UpstreamConfig, ...] = ()
    config_updated_at: str | None = None
    config_update_policy: str = "manual"
    config_expires_at: str | None = None

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "SourceConfig":
        if not isinstance(value, dict):
            raise ConfigError("source must be an object")
        try:
            name = str(value["name"])
            base_url = str(value["base_url"])
        except KeyError as exc:
            raise ConfigError(f"source is missing a field: {exc.args[0]}") from exc

        if not _SOURCE_NAME.fullmatch(name):
            raise ConfigError(f"invalid source name: {name!r}")

        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ConfigError(f"base_url must be an absolute HTTP(S) URL: {base_url!r}")
        if parsed.query or parsed.fragment:
            raise ConfigError("base_url must not contain a query or fragment")

        normalized_path = parsed.path or "/"
        if not normalized_path.endswith("/"):
            normalized_path += "/"
        normalized_base = urlunsplit(
            (parsed.scheme.lower(), parsed.netloc.lower(), normalized_path, "", "")
        )

        configured_origins = value.get("allowed_redirect_origins", [])
        if not isinstance(configured_origins, list):
            raise ConfigError("allowed_redirect_origins must be an array")
        origins = {_origin(normalized_base)}
        for item in configured_origins:
            candidate = str(item).rstrip("/").lower()
            parsed_origin = urlsplit(candidate)
            if (
                parsed_origin.scheme not in {"http", "https"}
                or not parsed_origin.netloc
                or parsed_origin.path
                or parsed_origin.query
                or parsed_origin.fragment
            ):
                raise ConfigError(f"invalid redirect origin: {item!r}")
            origins.add(candidate)

        rewrite_origins = {_origin(normalized_base)}
        configured_rewrite_origins = value.get("html_rewrite_origins", [])
        if not isinstance(configured_rewrite_origins, list):
            raise ConfigError("html_rewrite_origins must be an array")
        for item in configured_rewrite_origins:
            candidate = str(item).rstrip("/").lower()
            parsed_origin = urlsplit(candidate)
            if (
                parsed_origin.scheme not in {"http", "https"}
                or not parsed_origin.netloc
                or parsed_origin.path
                or parsed_origin.query
                or parsed_origin.fragment
            ):
                raise ConfigError(f"invalid HTML rewrite origin: {item!r}")
            rewrite_origins.add(candidate)

        configured_rewrite_prefixes = value.get("html_rewrite_url_prefixes", [])
        if not isinstance(configured_rewrite_prefixes, list):
            raise ConfigError("html_rewrite_url_prefixes must be an array")
        rewrite_prefixes: list[str] = []
        for item in configured_rewrite_prefixes:
            parsed_prefix = urlsplit(str(item))
            if (
                parsed_prefix.scheme not in {"http", "https"}
                or not parsed_prefix.netloc
                or not parsed_prefix.path.endswith("/")
                or parsed_prefix.query
                or parsed_prefix.fragment
            ):
                raise ConfigError(f"invalid HTML rewrite URL prefix: {item!r}")
            rewrite_prefixes.append(
                urlunsplit(
                    (
                        parsed_prefix.scheme.lower(),
                        parsed_prefix.netloc.lower(),
                        parsed_prefix.path,
                        "",
                        "",
                    )
                )
            )

        configured_rewrite_routes = value.get("html_rewrite_routes", {})
        if not isinstance(configured_rewrite_routes, dict):
            raise ConfigError("html_rewrite_routes must be an origin-to-source mapping")
        rewrite_routes: list[tuple[str, str]] = []
        for raw_origin, raw_source in configured_rewrite_routes.items():
            candidate = str(raw_origin).rstrip("/").lower()
            parsed_origin = urlsplit(candidate)
            target_source = str(raw_source)
            if (
                parsed_origin.scheme not in {"http", "https"}
                or not parsed_origin.netloc
                or parsed_origin.path
                or parsed_origin.query
                or parsed_origin.fragment
            ):
                raise ConfigError(f"invalid HTML rewrite route origin: {raw_origin!r}")
            if not _SOURCE_NAME.fullmatch(target_source):
                raise ConfigError(
                    f"invalid HTML rewrite route source: {target_source!r}"
                )
            rewrite_routes.append((candidate, target_source))

        configured_relative_routes = value.get(
            "html_rewrite_relative_routes", {}
        )
        if not isinstance(configured_relative_routes, dict):
            raise ConfigError(
                "html_rewrite_relative_routes must be a path-prefix-to-source mapping"
            )
        relative_routes: list[tuple[str, str]] = []
        for raw_prefix, raw_source in configured_relative_routes.items():
            prefix = _safe_relative_path(raw_prefix, prefix=True)
            target_source = str(raw_source)
            if not _SOURCE_NAME.fullmatch(target_source):
                raise ConfigError(
                    f"invalid HTML relative rewrite source: {target_source!r}"
                )
            relative_routes.append((prefix, target_source))

        rewrite_html = value.get("rewrite_html", False)
        if not isinstance(rewrite_html, bool):
            raise ConfigError("rewrite_html must be a boolean")

        kind = str(value.get("kind", "generic"))
        if kind not in _SOURCE_KINDS:
            raise ConfigError(f"invalid source kind: {kind!r}")
        ecosystem = value.get(
            "ecosystem", "apt" if kind == "apt-repository" else "generic"
        )
        if not isinstance(ecosystem, str) or ecosystem not in _SOURCE_ECOSYSTEMS:
            raise ConfigError(f"invalid source ecosystem: {ecosystem!r}")
        allowed_path_prefixes = _path_list(
            value, "allowed_path_prefixes", prefix=True
        )
        allowed_exact_paths = frozenset(
            _path_list(value, "allowed_exact_paths")
        )
        mutable_path_prefixes = _path_list(
            value, "mutable_path_prefixes", prefix=True
        )
        mutable_exact_paths = frozenset(
            _path_list(value, "mutable_exact_paths")
        )
        configured_flat_packages = value.get("flat_index_packages", [])
        if not isinstance(configured_flat_packages, list):
            raise ConfigError("flat_index_packages must be an array")
        flat_index_packages = frozenset(str(item) for item in configured_flat_packages)
        if any(not _NPM_PATH_COMPONENT.fullmatch(item) for item in flat_index_packages):
            raise ConfigError("flat_index_packages contains invalid package names")
        configured_root_artifact_packages = value.get(
            "root_artifact_packages", []
        )
        if not isinstance(configured_root_artifact_packages, list):
            raise ConfigError("root_artifact_packages must be an array")
        root_artifact_packages = frozenset(
            str(item) for item in configured_root_artifact_packages
        )
        if any(
            not _NPM_PATH_COMPONENT.fullmatch(item)
            for item in root_artifact_packages
        ):
            raise ConfigError("root_artifact_packages contains invalid package names")
        allow_query = value.get("allow_query", True)
        if not isinstance(allow_query, bool):
            raise ConfigError("allow_query must be a boolean")
        proxy_mode = value.get("proxy_mode", "configured")
        if not isinstance(proxy_mode, str) or proxy_mode not in _PROXY_MODES:
            raise ConfigError(f"invalid proxy_mode: {proxy_mode!r}")
        raw_fallbacks = value.get("fallback_upstreams", [])
        if not isinstance(raw_fallbacks, list):
            raise ConfigError("fallback_upstreams must be an array")
        parsed_fallbacks = tuple(
            _upstream_from_dict(
                item,
                default_proxy_mode="configured",
                default_label=f"fallback-{index}",
            )
            for index, item in enumerate(raw_fallbacks, start=1)
        )
        fallback_upstreams = tuple(
            UpstreamConfig(
                base_url=parsed_fallback.base_url,
                allowed_redirect_origins=parsed_fallback.allowed_redirect_origins,
                proxy_mode=parsed_fallback.proxy_mode,
                label=parsed_fallback.label,
                attempt_timeout_seconds=parsed_fallback.attempt_timeout_seconds,
                slow_after_seconds=parsed_fallback.slow_after_seconds,
                min_bytes_per_second=parsed_fallback.min_bytes_per_second,
                allowed_path_prefixes=allowed_path_prefixes,
                allowed_filename_prefixes=tuple(
                    sorted(
                        {
                            prefix
                            for package in root_artifact_packages
                            for prefix in (
                                f"{package}-",
                                f"{package.replace('-', '_')}-",
                            )
                        }
                    )
                ),
                allowed_exact_paths=allowed_exact_paths,
                allow_query=allow_query,
            )
            for parsed_fallback in parsed_fallbacks
        )
        upstream_bases = [normalized_base] + [
            item.base_url for item in fallback_upstreams
        ]
        if len(set(upstream_bases)) != len(upstream_bases):
            raise ConfigError("primary and fallback upstream base_url must not repeat")
        upstream_label, attempt_timeout, slow_after, min_speed = _upstream_policy(
            value,
            label_key="upstream_label",
            default_label="primary",
            prefix="primary upstream",
        )
        labels = [upstream_label, *(item.label for item in fallback_upstreams)]
        if len(labels) != len(set(labels)):
            raise ConfigError("upstream labels must be unique within a source")

        raw_ttl = value.get("metadata_ttl_seconds")
        metadata_ttl_seconds: float | None = None
        if raw_ttl is not None:
            if isinstance(raw_ttl, bool):
                raise ConfigError("metadata_ttl_seconds must be a positive number")
            try:
                metadata_ttl_seconds = float(raw_ttl)
            except (TypeError, ValueError) as exc:
                raise ConfigError("metadata_ttl_seconds must be a positive number") from exc
            if metadata_ttl_seconds <= 0:
                raise ConfigError("metadata_ttl_seconds must be a positive number")

        raw_config_updated_at = value.get("config_updated_at")
        config_updated_at: str | None = None
        if raw_config_updated_at is not None:
            if not isinstance(raw_config_updated_at, str):
                raise ConfigError("config_updated_at must be a YYYY-MM-DD date")
            try:
                parsed_updated_at = date.fromisoformat(raw_config_updated_at)
            except ValueError as exc:
                raise ConfigError("config_updated_at must be a YYYY-MM-DD date") from exc
            config_updated_at = parsed_updated_at.isoformat()
        config_update_policy = value.get("config_update_policy", "manual")
        if (
            not isinstance(config_update_policy, str)
            or config_update_policy not in _CONFIG_UPDATE_POLICIES
        ):
            raise ConfigError("config_update_policy must be 'manual' or 'expires'")
        raw_config_expires_at = value.get("config_expires_at")
        config_expires_at: str | None = None
        if raw_config_expires_at is not None:
            if not isinstance(raw_config_expires_at, str):
                raise ConfigError("config_expires_at must be a YYYY-MM-DD date")
            try:
                parsed_expires_at = date.fromisoformat(raw_config_expires_at)
            except ValueError as exc:
                raise ConfigError("config_expires_at must be a YYYY-MM-DD date") from exc
            config_expires_at = parsed_expires_at.isoformat()
        if config_update_policy == "expires":
            if config_updated_at is None or config_expires_at is None:
                raise ConfigError(
                    "expires policy requires config_updated_at and config_expires_at"
                )
            if config_expires_at < config_updated_at:
                raise ConfigError("config_expires_at must not precede config_updated_at")
        elif config_expires_at is not None:
            raise ConfigError("manual policy cannot set config_expires_at")
        if (
            config_updated_at is None
            and ("config_update_policy" in value or "config_expires_at" in value)
        ):
            raise ConfigError("config update policy requires config_updated_at")

        if kind == "apt-repository":
            if not allowed_path_prefixes:
                raise ConfigError("apt-repository sources must configure allowed_path_prefixes")
            if not mutable_path_prefixes or metadata_ttl_seconds is None:
                raise ConfigError(
                    "apt-repository sources must configure mutable_path_prefixes and metadata_ttl_seconds"
                )
            for mutable in mutable_path_prefixes:
                if not any(
                    mutable.startswith(allowed) or allowed.startswith(mutable)
                    for allowed in allowed_path_prefixes
                ):
                    raise ConfigError(
                        f"mutable path is outside the allowed path range: {mutable!r}"
                    )
        if kind == "static-objects" and not allowed_exact_paths:
            raise ConfigError("static-objects sources must configure allowed_exact_paths")
        if kind in {"frozen-download", "transparent-download"}:
            if kind == "frozen-download" and ecosystem != "download":
                raise ConfigError("frozen-download sources must use the download ecosystem")
            if kind == "transparent-download" and ecosystem not in {
                "download",
                "apt",
            }:
                raise ConfigError(
                    "transparent-download sources must use the download or apt ecosystem"
                )
            if kind == "frozen-download" and allow_query:
                raise ConfigError("frozen-download sources must disallow query")
            if normalized_path != "/":
                raise ConfigError("download source base_url must be the origin root")
            if allowed_path_prefixes or allowed_exact_paths:
                raise ConfigError("download sources do not accept object-level allowlists")
            if kind == "frozen-download" and (
                mutable_path_prefixes
                or mutable_exact_paths
                or metadata_ttl_seconds
            ):
                raise ConfigError("frozen-download objects must be frozen permanently")
        if kind == "npm-registry":
            if ecosystem != "node":
                raise ConfigError("npm-registry sources must use the node ecosystem")
            if allow_query:
                raise ConfigError("npm-registry sources must disallow query")
            if metadata_ttl_seconds is None:
                raise ConfigError("npm-registry sources must configure metadata_ttl_seconds")
        if kind == "go-proxy":
            if ecosystem != "go":
                raise ConfigError("go-proxy sources must use the go ecosystem")
            if allow_query:
                raise ConfigError("go-proxy sources must disallow query")
            if metadata_ttl_seconds is None:
                raise ConfigError("go-proxy sources must configure metadata_ttl_seconds")
        if kind == "go-sumdb":
            if ecosystem != "go":
                raise ConfigError("go-sumdb sources must use the go ecosystem")
            if allow_query:
                raise ConfigError("go-sumdb sources must disallow query")
            if metadata_ttl_seconds is None:
                raise ConfigError("go-sumdb sources must configure metadata_ttl_seconds")
        if kind == "cargo-sparse":
            if ecosystem != "cargo":
                raise ConfigError("cargo-sparse sources must use the cargo ecosystem")
            if allow_query:
                raise ConfigError("cargo-sparse sources must disallow query")
            if metadata_ttl_seconds is None:
                raise ConfigError("cargo-sparse sources must configure metadata_ttl_seconds")
        if kind == "cargo-crates":
            if ecosystem != "cargo":
                raise ConfigError("cargo-crates sources must use the cargo ecosystem")
            if allow_query:
                raise ConfigError("cargo-crates sources must disallow query")
        if kind == "dart-pub":
            if ecosystem != "dart":
                raise ConfigError("dart-pub sources must use the dart ecosystem")
            if allow_query:
                raise ConfigError("dart-pub sources must disallow query")
            if not allowed_path_prefixes:
                raise ConfigError("dart-pub sources must configure allowed_path_prefixes")
            if (
                any(
                    prefix.startswith("api/packages/")
                    for prefix in allowed_path_prefixes
                )
                and metadata_ttl_seconds is None
            ):
                raise ConfigError("dart-pub sources must configure metadata_ttl_seconds")
        if kind == "julia-pkg":
            if ecosystem != "julia":
                raise ConfigError("julia-pkg sources must use the julia ecosystem")
            if allow_query:
                raise ConfigError("julia-pkg sources must disallow query")
            if "registries" not in allowed_exact_paths:
                raise ConfigError("julia-pkg sources must explicitly allow registries")
            if metadata_ttl_seconds is None:
                raise ConfigError("julia-pkg sources must configure metadata_ttl_seconds")
        if kind == "pytorch-wheels":
            if ecosystem != "pip":
                raise ConfigError("pytorch-wheels sources must use the pip ecosystem")
            if allow_query:
                raise ConfigError("pytorch-wheels sources must disallow query")
            if not flat_index_packages:
                raise ConfigError("pytorch-wheels sources must configure flat_index_packages")

        return cls(
            name=name,
            base_url=normalized_base,
            allowed_redirect_origins=frozenset(origins),
            html_rewrite_origins=frozenset(rewrite_origins),
            html_rewrite_url_prefixes=tuple(sorted(set(rewrite_prefixes))),
            html_rewrite_routes=tuple(sorted(set(rewrite_routes))),
            html_rewrite_relative_routes=tuple(
                sorted(set(relative_routes))
            ),
            rewrite_html=rewrite_html,
            kind=kind,
            ecosystem=ecosystem,
            allowed_path_prefixes=allowed_path_prefixes,
            allowed_exact_paths=allowed_exact_paths,
            mutable_path_prefixes=mutable_path_prefixes,
            mutable_exact_paths=mutable_exact_paths,
            flat_index_packages=flat_index_packages,
            root_artifact_packages=root_artifact_packages,
            metadata_ttl_seconds=metadata_ttl_seconds,
            allow_query=allow_query,
            proxy_mode=proxy_mode,
            upstream_label=upstream_label,
            attempt_timeout_seconds=attempt_timeout,
            slow_after_seconds=slow_after,
            min_bytes_per_second=min_speed,
            fallback_upstreams=fallback_upstreams,
            config_updated_at=config_updated_at,
            config_update_policy=config_update_policy,
            config_expires_at=config_expires_at,
        )

    @property
    def primary_upstream(self) -> UpstreamConfig:
        return UpstreamConfig(
            base_url=self.base_url,
            allowed_redirect_origins=self.allowed_redirect_origins,
            proxy_mode=self.proxy_mode,
            label=self.upstream_label,
            attempt_timeout_seconds=self.attempt_timeout_seconds,
            slow_after_seconds=self.slow_after_seconds,
            min_bytes_per_second=self.min_bytes_per_second,
            allowed_path_prefixes=self.allowed_path_prefixes,
            allowed_filename_prefixes=tuple(
                sorted(
                    {
                        prefix
                        for package in self.root_artifact_packages
                        for prefix in (
                            f"{package}-",
                            f"{package.replace('-', '_')}-",
                        )
                    }
                )
            ),
            allowed_exact_paths=self.allowed_exact_paths,
            allow_query=self.allow_query,
        )

    def fetch_candidates(
        self, relative_path: str, query: str = "", *, prefer_fallback: bool = False
    ) -> tuple[tuple[UpstreamConfig, str], ...]:
        self.build_url(relative_path, query)
        frozen_path = (
            decode_frozen_download_relative_path(relative_path)
            if self.kind in {"frozen-download", "transparent-download"}
            else None
        )
        upstreams = (self.primary_upstream, *self.fallback_upstreams)
        decoded = unquote(relative_path)
        package_index = (
            _PYTORCH_PACKAGE_INDEX.fullmatch(decoded)
            if self.kind == "pytorch-wheels"
            else None
        )
        if (
            package_index
            and package_index.group("package") not in self.flat_index_packages
        ) or (
            self.kind == "pytorch-wheels"
            and _PYTORCH_ROOT_ARTIFACT.fullmatch(decoded)
        ):
            # The domestic mirror has a usable flat page for only the
            # configured packages. Its other 200 pages can be semantically
            # empty, so route those package indexes to the reviewed fallback.
            upstreams = self.fallback_upstreams
        elif prefer_fallback and self.fallback_upstreams:
            upstreams = (*self.fallback_upstreams, self.primary_upstream)
        candidates: list[tuple[UpstreamConfig, str]] = []
        for upstream in upstreams:
            candidate_path = (
                frozen_path[1:]
                if self.kind in {"frozen-download", "transparent-download"}
                and frozen_path is not None
                else relative_path
            )
            if (
                self.kind == "pytorch-wheels"
                and upstream.label == self.upstream_label
                and package_index
                and package_index.group("package") in self.flat_index_packages
            ):
                # Aliyun exposes one flat channel page, whereas the official
                # PyTorch index exposes one PEP 503 page per package.
                candidate_path = f"{package_index.group('channel')}/"
            candidate_url = (
                upstream.base_url + (f"?{query}" if query else "")
                if (
                    self.kind == "static-objects" and decoded == "@root"
                ) or (
                    self.kind in {"frozen-download", "transparent-download"}
                    and frozen_path is None
                )
                else upstream.build_url(candidate_path, query)
            )
            candidates.append((upstream, candidate_url))
        return tuple(candidates)

    def build_url(self, relative_path: str, query: str = "") -> str:
        if self.kind in {"frozen-download", "transparent-download"}:
            if query and not self.allow_query:
                raise ConfigError("this source does not allow query")
            try:
                path = decode_frozen_download_relative_path(relative_path)
            except ValueError as exc:
                raise ConfigError(str(exc)) from exc
            result = self.base_url if path is None else f"{self.base_url}{path[1:]}"
            return result + (f"?{query}" if query else "")
        decoded = _safe_relative_path(relative_path)
        handler = handler_for_kind(self.kind)
        if handler is not None and handler.validate_path is not None:
            message = handler.validate_path(self, decoded)
            if message is not None:
                raise ConfigError(message)
        if (
            self.kind != "pytorch-wheels"
            and (self.allowed_path_prefixes or self.allowed_exact_paths)
        ) and not (
            decoded in self.allowed_exact_paths
            or any(decoded.startswith(prefix) for prefix in self.allowed_path_prefixes)
        ):
            raise ConfigError("upstream path is not in the source allowlist")
        if query and not self.allow_query:
            raise ConfigError("this source does not allow query")
        if self.kind == "static-objects" and decoded == "@root":
            return self.base_url + (f"?{query}" if query else "")
        return f"{self.base_url}{relative_path.lstrip('/')}" + (
            f"?{query}" if query else ""
        )

    def allows_url(self, url: str) -> bool:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or _origin(url) not in self.allowed_redirect_origins
        ):
            return False
        base = urlsplit(self.base_url)
        if _origin(url) != _origin(self.base_url):
            return True
        if not parsed.path.startswith(base.path):
            return False
        relative_path = unquote(parsed.path[len(base.path) :]).lstrip("/")
        if not (self.allowed_path_prefixes or self.allowed_exact_paths):
            return True
        return (
            relative_path in self.allowed_exact_paths
            or (relative_path == "" and "@root" in self.allowed_exact_paths)
            or any(
                relative_path.startswith(prefix)
                for prefix in self.allowed_path_prefixes
            )
        )

    def allows_upstream_url(
        self,
        upstream: UpstreamConfig,
        url: str,
        *,
        candidate_url: str,
    ) -> bool:
        """Validate a candidate or server-selected redirect URL.

        The client-selected candidate is still checked against the exact source
        path and query policy.  A redirect is selected by that reviewed
        upstream, so it may use a canonical same-origin path or a signed query
        on an explicitly allowed CDN origin without widening the client route.
        """
        if self.kind == "transparent-download":
            parsed = urlsplit(url)
            return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
        if url == candidate_url:
            # Some reviewed mirrors map a canonical package index to a flat
            # index page.  The mapped candidate was constructed internally.
            if self.kind == "pytorch-wheels":
                parsed = urlsplit(url)
                upstream_base = urlsplit(upstream.base_url)
                return (
                    parsed.scheme in {"http", "https"}
                    and _origin(url) == _origin(upstream.base_url)
                    and parsed.path.startswith(upstream_base.path)
                    and (not parsed.query or upstream.allow_query)
                )
            return upstream.allows_url(url)

        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or _origin(url) not in upstream.allowed_redirect_origins
        ):
            return False
        upstream_base = urlsplit(upstream.base_url)
        if _origin(url) != _origin(upstream.base_url):
            # External redirects remain constrained to explicitly reviewed
            # origins. Their object paths and signed queries are CDN-generated.
            return True
        if not parsed.path.startswith(upstream_base.path):
            return False
        if self.kind in {"static-objects", "frozen-download"}:
            # Same-origin canonicalization may change an exact legacy path,
            # but it must remain below the source's reviewed base path.
            return True
        relative_path = unquote(parsed.path[len(upstream_base.path) :]).lstrip("/")
        try:
            self.build_url(relative_path, parsed.query)
        except ConfigError:
            return False
        return True

    def freshness_ttl(
        self,
        relative_path: str,
        content_type: str,
        default_index_ttl_seconds: float,
    ) -> float | None:
        if self.kind == "transparent-download" and self.ecosystem == "apt":
            path = decode_frozen_download_relative_path(relative_path)
            if path is not None and path.lower().endswith(".deb"):
                return None
            return default_index_ttl_seconds
        if self.kind == "transparent-download":
            return default_index_ttl_seconds
        decoded = unquote(relative_path).lstrip("/")
        # A few reviewed third-party repositories (notably MongoDB) publish
        # versioned package blobs below dists/ instead of pool/.  The path is
        # still a package object, not mutable repository metadata.
        if self.kind == "apt-repository" and decoded.lower().endswith(".deb"):
            return None
        if any(decoded.startswith(prefix) for prefix in self.mutable_path_prefixes):
            return self.metadata_ttl_seconds
        if decoded in self.mutable_exact_paths:
            return self.metadata_ttl_seconds
        if self.kind == "go-proxy" and decoded.endswith(("/@v/list", "/@latest")):
            return self.metadata_ttl_seconds
        if self.kind == "go-sumdb" and decoded == "latest":
            return self.metadata_ttl_seconds
        if self.kind == "cargo-sparse":
            return self.metadata_ttl_seconds
        if self.kind == "npm-registry" and not decoded.lower().endswith(".tgz"):
            return self.metadata_ttl_seconds
        if self.kind == "dart-pub" and decoded.startswith("api/packages/"):
            return self.metadata_ttl_seconds
        if self.kind == "julia-pkg" and decoded == "registries":
            return self.metadata_ttl_seconds
        if content_type.lower().startswith("text/html"):
            return (
                self.metadata_ttl_seconds
                if self.metadata_ttl_seconds is not None
                else default_index_ttl_seconds
            )
        return None

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from ...core.config import ConfigError, SourceConfig
from ...core.source_naming import readable_source_name


_KNOWN_STATIC_REDIRECT_ORIGINS = {
    "https://www.mongodb.org/": ["https://pgp.mongodb.com"],
}


class AptGatewayPlanError(ValueError):
    """Raised when an APT candidate report cannot become a safe source plan."""


def _source_name(prefix: str, identity: str) -> str:
    return readable_source_name(prefix, identity, include_path=True)


def repository_source_name(base_url: str) -> str:
    """Return the stable Gateway source name for one reviewed APT repository."""

    return _source_name("apt-repo", base_url.rstrip("/") + "/")


def _candidate_rows(report: dict[str, object]) -> list[dict[str, object]]:
    if report.get("kind") != "artifact-cache-apt-candidate-report":
        raise AptGatewayPlanError("input is not an APT candidate report")
    rows = report.get("cache_candidates")
    if not isinstance(rows, list):
        raise AptGatewayPlanError("APT candidate report has no cache_candidates array")
    if not all(isinstance(row, dict) for row in rows):
        raise AptGatewayPlanError("APT cache candidate must be an object")
    return rows  # type: ignore[return-value]


def compile_apt_gateway_plan(report: dict[str, object]) -> dict[str, object]:
    updated_at = date.today().isoformat()
    repositories: dict[str, dict[str, object]] = {}
    static_by_origin: dict[str, set[str]] = {}
    rewrites: list[dict[str, object]] = []
    rejected: list[dict[str, str]] = []

    for row in _candidate_rows(report):
        raw_url = row.get("url")
        kind = row.get("kind")
        if not isinstance(raw_url, str) or not isinstance(kind, str):
            rejected.append({"url": str(raw_url), "reason": "missing URL or kind"})
            continue
        parsed = urlsplit(raw_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.query
            or parsed.fragment
            or row.get("query_present") is True
        ):
            rejected.append(
                {"url": raw_url, "reason": "URL is not a query-free absolute HTTP(S) URL"}
            )
            continue

        if kind == "repository":
            base_url = raw_url.rstrip("/") + "/"
            name = repository_source_name(base_url)
            source = {
                "name": name,
                "kind": "apt-repository",
                "ecosystem": "apt",
                "base_url": base_url,
                "allowed_redirect_origins": [],
                "allowed_path_prefixes": ["dists/", "pool/"],
                "mutable_path_prefixes": ["dists/"],
                "metadata_ttl_seconds": 300,
                "allow_query": False,
                "config_updated_at": updated_at,
                "config_update_policy": "manual",
            }
            SourceConfig.from_dict(source)
            repositories[base_url] = source
            rewrites.append(
                {
                    "kind": kind,
                    "upstream_url": raw_url,
                    "source": name,
                    "gateway_path": f"/v1/cache/{name}/",
                }
            )
            continue

        if kind not in {"signing-key", "deb-artifact", "apt-bootstrap"}:
            rejected.append({"url": raw_url, "reason": f"unsupported APT kind: {kind}"})
            continue
        origin = urlunsplit((parsed.scheme, parsed.netloc, "/", "", ""))
        path = parsed.path.lstrip("/")
        if not path or parsed.path.endswith("/"):
            rejected.append({"url": raw_url, "reason": "static object path is empty"})
            continue
        static_by_origin.setdefault(origin, set()).add(path)

    sources = list(repositories.values())
    static_names: dict[str, str] = {}
    for origin, paths in sorted(static_by_origin.items()):
        name = _source_name("apt-objects", origin)
        static_names[origin] = name
        source = {
            "name": name,
            "kind": "static-objects",
            "ecosystem": "apt",
            "base_url": origin,
            "allowed_redirect_origins": _KNOWN_STATIC_REDIRECT_ORIGINS.get(
                origin, []
            ),
            "allowed_exact_paths": sorted(paths),
            "allow_query": False,
            "config_updated_at": updated_at,
            "config_update_policy": "manual",
        }
        SourceConfig.from_dict(source)
        sources.append(source)

    for row in _candidate_rows(report):
        raw_url = row.get("url")
        kind = row.get("kind")
        if (
            not isinstance(raw_url, str)
            or kind not in {"signing-key", "deb-artifact", "apt-bootstrap"}
            or row.get("query_present") is True
        ):
            continue
        parsed = urlsplit(raw_url)
        origin = urlunsplit((parsed.scheme, parsed.netloc, "/", "", ""))
        name = static_names.get(origin)
        path = parsed.path.lstrip("/")
        if name and path:
            rewrites.append(
                {
                    "kind": kind,
                    "upstream_url": raw_url,
                    "source": name,
                    "gateway_path": f"/v1/cache/{name}/{quote(path, safe='/@:+~.-')}",
                }
            )

    sources.sort(key=lambda item: str(item["name"]))
    rewrites.sort(key=lambda item: (str(item["upstream_url"]), str(item["kind"])))
    packages = report.get("packages_needing_resolution", [])
    if not isinstance(packages, list):
        packages = []
    return {
        "schema_version": 1,
        "kind": "artifact-cache-apt-gateway-plan",
        "dataset": report.get("dataset"),
        "sources": sources,
        "rewrites": rewrites,
        "rejected": rejected,
        "packages_needing_resolution": packages,
    }


def merge_gateway_config(
    base_document: dict[str, object], plan: dict[str, object]
) -> dict[str, object]:
    raw_base = base_document.get("sources")
    raw_added = plan.get("sources")
    if not isinstance(raw_base, list) or not isinstance(raw_added, list):
        raise AptGatewayPlanError("base config and APT plan must contain sources arrays")
    merged: dict[str, dict[str, object]] = {}
    parsed_sources: dict[str, SourceConfig] = {}
    for raw in [*raw_base, *raw_added]:
        if not isinstance(raw, dict):
            raise AptGatewayPlanError("gateway source must be an object")
        incoming = raw
        try:
            parsed = SourceConfig.from_dict(raw)
        except ConfigError as exc:
            raise AptGatewayPlanError(str(exc)) from exc
        previous = merged.get(parsed.name)
        if previous is not None and previous != raw:
            previous_parsed = parsed_sources[parsed.name]
            comparable = replace(
                parsed,
                allowed_exact_paths=(
                    frozenset()
                    if parsed.kind == "static-objects"
                    else parsed.allowed_exact_paths
                ),
                config_updated_at=None,
                config_update_policy="manual",
                config_expires_at=None,
            )
            previous_comparable = replace(
                previous_parsed,
                allowed_exact_paths=(
                    frozenset()
                    if previous_parsed.kind == "static-objects"
                    else previous_parsed.allowed_exact_paths
                ),
                config_updated_at=None,
                config_update_policy="manual",
                config_expires_at=None,
            )
            if comparable == previous_comparable:
                raw = dict(previous)
                if parsed.kind == "static-objects":
                    raw["allowed_exact_paths"] = sorted(
                        previous_parsed.allowed_exact_paths
                        | parsed.allowed_exact_paths
                    )
                metadata_source = (
                    previous
                    if (previous_parsed.config_updated_at or "")
                    >= (parsed.config_updated_at or "")
                    else incoming
                )
                for field in (
                    "config_updated_at",
                    "config_update_policy",
                    "config_expires_at",
                ):
                    raw.pop(field, None)
                    if field in metadata_source:
                        raw[field] = metadata_source[field]
                parsed = SourceConfig.from_dict(raw)
            else:
                raise AptGatewayPlanError(
                    f"conflicting gateway source: {parsed.name}"
                )
        merged[parsed.name] = raw
        parsed_sources[parsed.name] = parsed
    return {"sources": [merged[name] for name in sorted(merged)]}


def read_json_object(path: Path) -> dict[str, object]:
    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise AptGatewayPlanError(f"JSON document must be an object: {path}")
    return value


def write_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path

from artifact_cache_gateway.gateway.services.apt import repository_source_name
from artifact_cache_gateway.core.config import SourceConfig
from artifact_cache_gateway.core.source_naming import (
    readable_source_name,
    without_legacy_source_hash,
)


def _new_name(source: dict[str, object]) -> str:
    old_name = str(source.get("name", ""))
    if without_legacy_source_hash(old_name) == old_name:
        return old_name
    base_url = str(source.get("base_url", ""))
    ecosystem = source.get("ecosystem")
    kind = source.get("kind")
    if ecosystem == "apt" and kind == "apt-repository":
        return repository_source_name(base_url)
    if ecosystem == "apt":
        return readable_source_name("apt-objects", base_url, include_path=True)
    if ecosystem == "download":
        return readable_source_name(
            "download-objects", base_url, include_path=False
        )
    return without_legacy_source_hash(old_name)


def migrate(document: dict[str, object], updated_at: str) -> dict[str, object]:
    raw_sources = document.get("sources")
    if not isinstance(raw_sources, list) or not all(
        isinstance(source, dict) for source in raw_sources
    ):
        raise ValueError("document must contain a sources array")

    mapping: dict[str, str] = {}
    names: dict[str, str] = {}
    for source in raw_sources:
        assert isinstance(source, dict)
        old_name = str(source.get("name", ""))
        new_name = _new_name(source)
        previous = names.get(new_name)
        if previous is not None and previous != old_name:
            raise ValueError(
                f"readable source name collision: {previous!r} and "
                f"{old_name!r} both become {new_name!r}"
            )
        mapping[old_name] = new_name
        names[new_name] = old_name
        source["name"] = new_name
        source["config_updated_at"] = updated_at
        source["config_update_policy"] = "manual"
        source.pop("config_expires_at", None)
        SourceConfig.from_dict(source)

    for source in raw_sources:
        assert isinstance(source, dict)
        for field in ("html_rewrite_routes", "html_rewrite_relative_routes"):
            routes = source.get(field)
            if isinstance(routes, dict):
                source[field] = {
                    key: mapping.get(str(target), str(target))
                    for key, target in routes.items()
                }

    rewrites = document.get("rewrites", [])
    if isinstance(rewrites, list):
        for rewrite in rewrites:
            if not isinstance(rewrite, dict):
                continue
            old_source = rewrite.get("source")
            if not isinstance(old_source, str) or old_source not in mapping:
                continue
            new_source = mapping[old_source]
            rewrite["source"] = new_source
            gateway_path = rewrite.get("gateway_path")
            if isinstance(gateway_path, str):
                rewrite["gateway_path"] = gateway_path.replace(
                    f"/v1/cache/{old_source}/",
                    f"/v1/cache/{new_source}/",
                    1,
                )
    return document


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Remove legacy source-name hashes and stamp config review metadata."
    )
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--updated-at", default=date.today().isoformat())
    args = parser.parse_args()
    try:
        date.fromisoformat(args.updated_at)
    except ValueError as exc:
        parser.error(f"--updated-at must be YYYY-MM-DD: {exc}")

    for path in args.paths:
        document = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(document, dict):
            raise ValueError(f"JSON root must be an object: {path}")
        migrated = migrate(document, args.updated_at)
        temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
        temporary.write_text(
            json.dumps(migrated, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

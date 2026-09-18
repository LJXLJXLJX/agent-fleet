from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import cast

from ..core.config import load_config
from ..gateway.engine import Gateway
from ..gateway.fetcher import Fetcher
from ..storage.gpfs import FileStorage
from ..storage.s3 import S3Settings, S3Storage


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Backfill the read-only cache object inventory from cache metadata"
    )
    result.add_argument(
        "--storage",
        choices=("s3", "file"),
        default=os.environ.get("ARTIFACT_CACHE_STORAGE", "s3"),
    )
    result.add_argument(
        "--cache-dir",
        type=Path,
        default=Path(os.environ.get("ARTIFACT_CACHE_DIR", "/data/artifact-cache")),
    )
    result.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ["ARTIFACT_CACHE_CONFIG"])
        if os.environ.get("ARTIFACT_CACHE_CONFIG")
        else None,
    )
    return result


def main() -> None:
    args = parser().parse_args()
    config = load_config(args.config)
    if args.storage == "s3":
        storage = S3Storage(S3Settings.from_env(), work_dir=args.cache_dir)
    else:
        storage = FileStorage(args.cache_dir)
    gateway = Gateway(
        config=config,
        storage=storage,
        fetcher=cast(Fetcher, None),
        index_ttl_seconds=300,
    )
    print(json.dumps(gateway.backfill_inventory(), sort_keys=True))


if __name__ == "__main__":
    main()

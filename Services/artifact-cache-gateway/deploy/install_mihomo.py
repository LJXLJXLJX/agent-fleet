#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import shutil
import tempfile
import urllib.request
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    digest = hashlib.sha256()
    temporary = tempfile.NamedTemporaryFile(prefix="mihomo-", suffix=".gz", delete=False)
    temporary_path = Path(temporary.name)
    try:
        with temporary, urllib.request.urlopen(args.url, timeout=300) as response:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                temporary.write(chunk)
        if digest.hexdigest() != args.sha256:
            raise SystemExit("mihomo release SHA-256 mismatch")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(temporary_path, "rb") as source, args.output.open("wb") as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)
        args.output.chmod(0o755)
    finally:
        try:
            os.unlink(temporary_path)
        except FileNotFoundError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

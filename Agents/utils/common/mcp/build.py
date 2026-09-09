"""Build a deterministic, source-only zipapp in the existing dependency cache."""

import argparse
import hashlib
import io
import os
import tempfile
import zipfile
from pathlib import Path


def prepare(cache: Path, *, path_only: bool = False) -> Path:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for source in sorted((Path(__file__).parent / "exa").glob("*.py")):
            archive.writestr(zipfile.ZipInfo(source.name), source.read_bytes())
    payload = buffer.getvalue()
    target = cache / f"exa-web-mcp-{hashlib.sha256(payload).hexdigest()}.pyz"
    if not path_only and (not target.is_file() or target.read_bytes() != payload):
        cache.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=cache, delete=False) as stream:
            temporary = Path(stream.name)
            try:
                stream.write(payload)
                stream.flush()
                temporary.chmod(0o644)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cache", type=Path)
    parser.add_argument("--path-only", action="store_true")
    args = parser.parse_args()
    print(prepare(args.cache, path_only=args.path_only))

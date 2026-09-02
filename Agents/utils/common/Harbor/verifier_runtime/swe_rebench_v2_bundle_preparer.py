from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

try:
    from .. import python_runtime
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import python_runtime

BUNDLE_ID = "agent-fleet-swe-rebench-v2-verifier-bundle"
SELF_CHECK = "bin/harbor-verifier-bundle-check"


def _members(path: Path) -> dict[str, tarfile.TarInfo] | None:
    if not path.is_file():
        return None
    try:
        with tarfile.open(path) as archive:
            return {member.name.rstrip("/"): member for member in archive}
    except (OSError, tarfile.TarError):
        return None


def archive_ready(path: Path) -> bool:
    members = _members(path)
    if members is None:
        return False
    bin_root = f"{BUNDLE_ID}/bin"
    check = members.get(f"{BUNDLE_ID}/{SELF_CHECK}")
    python312 = members.get(f"{bin_root}/python3.12")
    python3 = members.get(f"{bin_root}/python3")
    python = members.get(f"{bin_root}/python")
    runtime_marker = members.get(
        f"{BUNDLE_ID}/{python_runtime.RUNTIME_MARKER}"
    )
    return bool(
        check
        and check.isfile()
        and check.mode & 0o111
        and python312
        and python312.isfile()
        and python312.mode & 0o111
        and python3
        and python3.issym()
        and python3.linkname == "python3.12"
        and python
        and python.issym()
        and python.linkname == "python3.12"
        and runtime_marker
        and runtime_marker.isfile()
    )


def _extract_safely(archive_path: Path, destination: Path) -> None:
    with tarfile.open(archive_path) as archive:
        for member in archive:
            member_path = PurePosixPath(member.name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise RuntimeError(f"unsafe archive member: {member.name}")
        archive.extractall(destination)


def build(python_runtime_archive: Path, output: Path) -> None:
    if archive_ready(output):
        print(f"[prepare] skip verifier runtime bundle (cached): {output}")
        return
    if not python_runtime.archive_ready(python_runtime_archive):
        raise RuntimeError(
            f"invalid Python runtime archive: {python_runtime_archive}"
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary_name:
        temporary = Path(temporary_name)
        _extract_safely(python_runtime_archive, temporary)
        source_root = temporary / python_runtime.RUNTIME_ROOT
        bundle_root = temporary / BUNDLE_ID
        source_root.rename(bundle_root)

        runtime_bin = bundle_root / "bin"
        for name in ("python", "python3", "harbor-verifier-bundle-check"):
            (runtime_bin / name).unlink(missing_ok=True)
        (runtime_bin / "python3").symlink_to("python3.12")
        (runtime_bin / "python").symlink_to("python3.12")
        self_check = runtime_bin / "harbor-verifier-bundle-check"
        self_check.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            'SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\n'
            'exec "${SELF_DIR}/python3" -c "import sys"\n',
            encoding="utf-8",
        )
        self_check.chmod(
            self_check.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        )

        file_descriptor, temporary_tar_name = tempfile.mkstemp(
            prefix=f".{output.name}.", suffix=".tmp", dir=output.parent
        )
        os.close(file_descriptor)
        temporary_tar = Path(temporary_tar_name)
        try:
            with tarfile.open(temporary_tar, "w:gz") as archive:
                archive.add(bundle_root, arcname=BUNDLE_ID)
            if not archive_ready(temporary_tar):
                raise RuntimeError("generated verifier runtime bundle is invalid")
            os.replace(temporary_tar, output)
        finally:
            temporary_tar.unlink(missing_ok=True)
    print(f"[prepare] built verifier runtime bundle: {output}")


def prepare(cache_dir: Path, output: Path) -> None:
    python_runtime_archive = cache_dir / "python3.12-runtime.tar.gz"
    if not python_runtime.archive_ready(python_runtime_archive):
        python_bin = os.environ.get("PYTHON_BIN") or "python3.12"
        subprocess.run(
            [
                python_bin,
                str(Path(__file__).resolve().parent.parent / "python_runtime.py"),
                "--output",
                str(python_runtime_archive),
            ],
            check=True,
        )
    build(python_runtime_archive, output)


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    build_parser = subparsers.add_parser("build")
    build_parser.add_argument("--cache-dir", required=True, type=Path)
    build_parser.add_argument("--output", required=True, type=Path)
    check_parser = subparsers.add_parser("check")
    check_parser.add_argument("--archive", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "check":
        return 0 if archive_ready(args.archive) else 1
    prepare(args.cache_dir, args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

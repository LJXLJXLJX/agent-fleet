from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
import sysconfig
import tarfile
import tempfile
from pathlib import Path

RUNTIME_ROOT = "python3.12-runtime"
RUNTIME_MARKER = ".harbor-python-runtime-v2"


def archive_ready(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        with tarfile.open(path) as archive:
            members = {member.name.rstrip("/"): member for member in archive}
    except (OSError, tarfile.TarError):
        return False

    wrapper = members.get(f"{RUNTIME_ROOT}/bin/python3.12")
    executable = members.get(f"{RUNTIME_ROOT}/bin/python3.12.real")
    stdlib = members.get(f"{RUNTIME_ROOT}/lib/python3.12")
    marker = members.get(f"{RUNTIME_ROOT}/{RUNTIME_MARKER}")
    return bool(
        wrapper
        and wrapper.isfile()
        and wrapper.mode & 0o111
        and executable
        and executable.isfile()
        and executable.mode & 0o111
        and stdlib
        and stdlib.isdir()
        and marker
        and marker.isfile()
    )


def build(target: Path) -> None:
    if archive_ready(target):
        print("[prepare] skip python3.12 runtime tarball (cached)")
        return

    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary_name:
        temporary = Path(temporary_name)
        runtime_root = temporary / RUNTIME_ROOT
        runtime_bin = runtime_root / "bin"
        runtime_lib = runtime_root / "lib"
        runtime_bin.mkdir(parents=True)
        runtime_lib.mkdir()

        python_real = Path(sys.executable).resolve()
        stdlib = Path(sysconfig.get_path("stdlib"))
        version = sysconfig.get_config_var("VERSION") or "3.12"
        if version != "3.12":
            raise RuntimeError(f"Python 3.12 is required, got {version}")
        libdir = Path(sysconfig.get_config_var("LIBDIR") or "")
        libpython = next(iter(sorted(libdir.glob(f"libpython{version}*.so*"))), None)
        shutil.copy2(python_real, runtime_bin / "python3.12.real")
        shutil.copytree(stdlib, runtime_lib / "python3.12", symlinks=True)
        if libpython and libpython.is_file():
            shutil.copy2(libpython, runtime_lib / libpython.name)

        # Never carry glibc or other host system libraries into task images.
        # Mixing the host's libc with a task image's dynamic loader is not a
        # portable runtime and can crash before Python reports an error. The
        # managed CPython used here targets glibc 2.17 and must use the target
        # image's own system libraries.
        (runtime_root / RUNTIME_MARKER).write_text(
            "target-system-libraries\n", encoding="utf-8"
        )

        wrapper = runtime_bin / "python3.12"
        wrapper.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            'SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\n'
            'RUNTIME_ROOT="$(cd "${SELF_DIR}/.." && pwd)"\n'
            'export PYTHONHOME="${RUNTIME_ROOT}"\n'
            'export LD_LIBRARY_PATH="${RUNTIME_ROOT}/lib:${LD_LIBRARY_PATH:-}"\n'
            'exec "${SELF_DIR}/python3.12.real" "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(
            wrapper.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        )

        file_descriptor, temporary_tar_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        os.close(file_descriptor)
        temporary_tar = Path(temporary_tar_name)
        try:
            with tarfile.open(temporary_tar, "w:gz") as archive:
                archive.add(runtime_root, arcname=runtime_root.name)
            if not archive_ready(temporary_tar):
                raise RuntimeError("generated Python runtime archive is invalid")
            os.replace(temporary_tar, target)
        finally:
            temporary_tar.unlink(missing_ok=True)
    print(f"[prepare] built python3.12 runtime tarball: {target}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check", type=Path)
    args = parser.parse_args()
    if bool(args.output) == bool(args.check):
        parser.error("exactly one of --output or --check is required")
    if args.check:
        return 0 if archive_ready(args.check) else 1
    build(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

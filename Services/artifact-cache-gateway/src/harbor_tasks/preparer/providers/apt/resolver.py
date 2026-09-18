"""APT resolver container script (resource loading) and command execution."""

from __future__ import annotations

import importlib.resources
import re
import subprocess
from typing import Callable

_RESOLVER_SCRIPT = (
    importlib.resources.files(__package__)
    .joinpath("resolver_script.sh")
    .read_text(encoding="utf-8")
)


CommandRunner = Callable[[list[str], float, str], str]


_APT_PACKAGE_NAME = re.compile(r"^[a-z0-9][a-z0-9+.-]*$")


_APT_PROVIDES_NAME = re.compile(r"(?:^|,)\s*([a-z0-9][a-z0-9+.-]*)")


def _record_matches_package(record: dict[str, str], package: str) -> bool:
    if record.get("Package") == package:
        return True
    return package in _APT_PROVIDES_NAME.findall(record.get("Provides", ""))


def _run_container(command: list[str], timeout: float, name: str) -> str:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        subprocess.run(
            ["docker", "stop", "--timeout", "5", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        raise
    if process.returncode != 0:
        tail = "\n".join(stderr.splitlines()[-20:])
        raise RuntimeError(
            f"APT resolver container failed with exit {process.returncode}: {tail}"
        )
    return stdout

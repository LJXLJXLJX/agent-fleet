"""pip resolver container script (resource loading) and command execution."""

from __future__ import annotations

import importlib.resources
import subprocess
from typing import Callable

_RESOLVER_SCRIPT = (
    importlib.resources.files(__package__)
    .joinpath("resolver_script.py")
    .read_text(encoding="utf-8")
)


_TUNA_SIMPLE = "https://pypi.tuna.tsinghua.edu.cn/simple"


_PYPI_FILES_ORIGIN = "https://files.pythonhosted.org"


CommandRunner = Callable[[list[str], float, str], str]


def _run_container(command: list[str], timeout: float, name: str) -> str:
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
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
            f"pip resolver container failed with exit {process.returncode}: {tail}"
        )
    return stdout

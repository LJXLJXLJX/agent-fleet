"""Execute Buildx OCI builds with timeout and process-group cleanup."""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

DEFAULT_PLATFORM = "linux/amd64"


def run_build(
    *,
    environment_dir: Path,
    dockerfile: Path,
    archive_path: Path,
    log_path: Path,
    platform: str,
    timeout_sec: float,
    build_args: dict[str, str],
    target: str | None = None,
    no_cache: bool = False,
    build_network: str = "default",
    secret_files: dict[str, Path] | None = None,
    build_contexts: dict[str, str] | None = None,
) -> None:
    child_env = os.environ.copy()
    child_env.update(build_args)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def execute(command: list[str], log_handle, operation: str, timeout: float) -> None:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            env=child_env,
            start_new_session=True,
        )

        def terminate_process_group() -> None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                process.wait()
                return
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()

        try:
            return_code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            terminate_process_group()
            raise RuntimeError(
                f"timed out {operation} after {timeout:g}s; see {log_path}"
            ) from exc
        except BaseException:
            terminate_process_group()
            raise
        if return_code != 0:
            raise RuntimeError(
                f"{operation} failed with exit code {return_code}; see {log_path}"
            )

    buildx_available = (
        subprocess.run(
            ["docker", "buildx", "version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0
    )
    if not buildx_available:
        raise RuntimeError(
            "docker buildx is required to build benchmark task images, but the "
            "plugin is unavailable or broken; install Docker Buildx and verify "
            "`docker buildx version` before retrying"
        )

    with log_path.open("w", encoding="utf-8") as log_handle:
        command = [
            "docker",
            "buildx",
            "build",
            f"--file={dockerfile}",
            f"--platform={platform}",
            f"--output=type=oci,dest={archive_path},compression=gzip,force-compression=true",
            "--provenance=false",
            "--progress=plain",
        ]
        for name, context in sorted((build_contexts or {}).items()):
            command.extend(("--build-context", f"{name}={context}"))
        for name in sorted(build_args):
            command.extend(("--build-arg", name))
        for secret_id, secret_path in sorted((secret_files or {}).items()):
            command.extend(("--secret", f"id={secret_id},src={secret_path}"))
        if build_network != "default":
            command.append(f"--network={build_network}")
        if no_cache:
            command.append("--no-cache")
        if target:
            command.append(f"--target={target}")
        command.append(str(environment_dir))
        execute(command, log_handle, "building task image", timeout_sec)

#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path


def _wait_for_proxy(process: subprocess.Popen[bytes], host: str, port: int) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        return_code = process.poll()
        if return_code is not None:
            raise RuntimeError(f"mihomo exited during startup with status {return_code}")
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.25)
    raise RuntimeError(f"mihomo did not listen on {host}:{port} within 30 seconds")


def _terminate(process: subprocess.Popen[bytes] | None) -> None:
    if process is not None and process.poll() is None:
        process.terminate()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mihomo-config", required=True, type=Path)
    args = parser.parse_args()

    if os.geteuid() == 0:
        os.setgid(int(os.environ.get("ARTIFACT_CACHE_RUNTIME_GID", "10250")))
        os.setuid(int(os.environ.get("ARTIFACT_CACHE_RUNTIME_UID", "10250")))

    mihomo_host = os.environ.get("MIHOMO_HOST", "127.0.0.1")
    mihomo_port = int(os.environ.get("MIHOMO_PORT", "7890"))
    gateway_host = os.environ.get("ARTIFACT_CACHE_HOST", "0.0.0.0")
    gateway_port = os.environ.get("ARTIFACT_CACHE_PORT", "8080")
    gateway_config = os.environ["ARTIFACT_CACHE_CONFIG"]

    mihomo: subprocess.Popen[bytes] | None = None
    gateway: subprocess.Popen[bytes] | None = None

    def stop(_signum: int, _frame: object) -> None:
        _terminate(gateway)
        _terminate(mihomo)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    try:
        mihomo = subprocess.Popen(
            [
                "mihomo",
                "-d",
                "/tmp/mihomo-home",
                "-f",
                str(args.mihomo_config),
            ]
        )
        _wait_for_proxy(mihomo, mihomo_host, mihomo_port)
        print(
            f"hosted runtime: mihomo is ready on {mihomo_host}:{mihomo_port}",
            flush=True,
        )

        gateway = subprocess.Popen(
            [
                "artifact-cache-gateway",
                "--host",
                gateway_host,
                "--port",
                gateway_port,
                "--config",
                gateway_config,
            ]
        )

        while True:
            mihomo_status = mihomo.poll()
            gateway_status = gateway.poll()
            if mihomo_status is not None:
                _terminate(gateway)
                return mihomo_status or 1
            if gateway_status is not None:
                _terminate(mihomo)
                return gateway_status
            time.sleep(0.5)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"ERROR: hosted runtime failed: {exc}", file=sys.stderr, flush=True)
        _terminate(gateway)
        _terminate(mihomo)
        return 1
    finally:
        for process in (gateway, mihomo):
            if process is not None:
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    raise SystemExit(main())

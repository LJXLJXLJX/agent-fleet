#!/usr/bin/env python3
"""Collect Linux /proc resource samples for one benchmark server process."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stop-file", type=Path, required=True)
    parser.add_argument("--interval-seconds", type=float, default=0.05)
    return parser.parse_args()


def _status_values(path: Path) -> tuple[int, int]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            values[key] = value.strip()
    rss_kib = int(values.get("VmRSS", "0 kB").split()[0])
    threads = int(values.get("Threads", "0"))
    return rss_kib, threads


def _cpu_seconds(path: Path) -> float:
    raw = path.read_text(encoding="utf-8")
    closing_paren = raw.rfind(")")
    fields = raw[closing_paren + 2 :].split()
    user_ticks = int(fields[11])
    system_ticks = int(fields[12])
    return (user_ticks + system_ticks) / os.sysconf("SC_CLK_TCK")


def _fd_counts(path: Path) -> tuple[int, int, set[str]]:
    fd_count = 0
    socket_count = 0
    socket_inodes: set[str] = set()
    for entry in path.iterdir():
        fd_count += 1
        try:
            target = os.readlink(entry)
        except OSError:
            continue
        if target.startswith("socket:["):
            socket_count += 1
            socket_inodes.add(target.removeprefix("socket:[").removesuffix("]"))
    return fd_count, socket_count, socket_inodes


def _tcp_counts(socket_inodes: set[str]) -> tuple[int, int, int]:
    tcp_sockets = 0
    established = 0
    listening = 0
    for table in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        if not table.exists():
            continue
        for line in table.read_text(encoding="utf-8").splitlines()[1:]:
            fields = line.split()
            if len(fields) < 10 or fields[9] not in socket_inodes:
                continue
            tcp_sockets += 1
            if fields[3] == "01":
                established += 1
            elif fields[3] == "0A":
                listening += 1
    return tcp_sockets, established, listening


def main() -> int:
    args = _parse_args()
    if not sys.platform.startswith("linux"):
        raise RuntimeError("resource collection requires Linux /proc; use --skip-metrics for a local smoke")
    process_root = Path("/proc") / str(args.pid)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    previous_monotonic: float | None = None
    previous_cpu: float | None = None
    with args.output.open("w", encoding="utf-8") as handle:
        while not args.stop_file.exists() and process_root.exists():
            try:
                monotonic = time.monotonic()
                cpu_seconds = _cpu_seconds(process_root / "stat")
                rss_kib, threads = _status_values(process_root / "status")
                fd_count, socket_fds, socket_inodes = _fd_counts(process_root / "fd")
                tcp_sockets, established_tcp, listening_tcp = _tcp_counts(socket_inodes)
            except (FileNotFoundError, ProcessLookupError):
                break
            cpu_percent = 0.0
            if previous_monotonic is not None and previous_cpu is not None:
                elapsed = monotonic - previous_monotonic
                if elapsed > 0:
                    cpu_percent = 100.0 * (cpu_seconds - previous_cpu) / elapsed
            sample = {
                "timestamp": time.time(),
                "monotonic": monotonic,
                "pid": args.pid,
                "rss_kib": rss_kib,
                "threads": threads,
                "open_fds": fd_count,
                "socket_fds": socket_fds,
                "tcp_sockets": tcp_sockets,
                "established_tcp": established_tcp,
                "listening_tcp": listening_tcp,
                "cpu_seconds": cpu_seconds,
                "cpu_percent": cpu_percent,
            }
            handle.write(json.dumps(sample, sort_keys=True) + "\n")
            handle.flush()
            previous_monotonic = monotonic
            previous_cpu = cpu_seconds
            time.sleep(args.interval_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

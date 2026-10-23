"""Bounded directory packing without blocking the native async command loop."""

from __future__ import annotations

import asyncio
import contextvars
import tarfile
import weakref
from concurrent.futures import ThreadPoolExecutor
from functools import cache
from pathlib import Path

from async_utils import await_owned, configured_threads

_slots: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


@cache
def _packing_pool() -> tuple[ThreadPoolExecutor, int]:
    workers = configured_threads("HARBOR_OPENSANDBOX_PACKING_THREADS", 4)
    return ThreadPoolExecutor(max_workers=workers, thread_name_prefix="sandbox-pack"), workers


def pack_directory(source: Path, archive: Path) -> None:
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(source, arcname=".")


async def pack_directory_async(source: Path, archive: Path) -> None:
    """Retain ownership until the writer exits, even under repeated cancellation.

    Cancelling a Future cannot stop a running tar writer. Its caller may remove
    the temporary directory only after this function returns or raises.
    Admission happens before submission so waiting trials do not fill the pool.
    """
    loop = asyncio.get_running_loop()
    pool, workers = _packing_pool()
    slots = _slots.setdefault(loop, asyncio.Semaphore(workers))
    async with slots:
        future = loop.run_in_executor(
            pool, contextvars.copy_context().run,
            pack_directory, source, archive,
        )
        await await_owned(future)

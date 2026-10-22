import asyncio
import os
import sys
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harbor_environment_adapters.providers.opensandbox_yicloud import packing


class PackingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        settings = patch.dict(os.environ, HARBOR_OPENSANDBOX_PACKING_THREADS="4")
        settings.start()
        self.addCleanup(settings.stop)
        self.addCleanup(self.close_pool)
        self.close_pool()

    def close_pool(self):
        if packing._packing_pool.cache_info().currsize:
            pool, _ = packing._packing_pool()
            pool.shutdown(wait=True)
            packing._packing_pool.cache_clear()

    async def test_archive_preserves_contents_modes_and_symlinks(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source"
            source.mkdir()
            executable = source / "script"
            executable.write_text("echo hello\n")
            executable.chmod(0o755)
            (source / "link").symlink_to("script")
            archive = root / "upload.tar.gz"
            await packing.pack_directory_async(source, archive)
            with tarfile.open(archive) as tar:
                self.assertEqual(tar.extractfile("./script").read(), b"echo hello\n")
                self.assertEqual(tar.getmember("./script").mode, 0o755)
                self.assertEqual(tar.getmember("./link").linkname, "script")

    async def test_bounded_workers_leave_loop_responsive(self):
        workers = 2
        admitted, release = threading.Event(), threading.Event()
        lock = threading.Lock()
        started = 0

        def worker(*_):
            nonlocal started
            with lock:
                started += 1
                if started == workers:
                    admitted.set()
            if not release.wait(5):
                raise TimeoutError("test worker was not released")

        with patch.dict(os.environ, HARBOR_OPENSANDBOX_PACKING_THREADS=str(workers)), \
                patch.object(packing, "pack_directory", worker):
            tasks = [asyncio.create_task(packing.pack_directory_async(Path("."), Path("unused")))
                     for _ in range(16)]
            try:
                self.assertTrue(await asyncio.to_thread(admitted.wait, 1))
                self.assertEqual(started, workers)
                # A cancelled admission must never submit another writer.
                tasks[workers].cancel()
                done, _ = await asyncio.wait([tasks[workers]], timeout=.02)
                self.assertTrue(done, "cancelled admission was already queued to a writer")
                with self.assertRaises(asyncio.CancelledError):
                    await tasks[workers]
            finally:
                release.set()
                await asyncio.gather(*tasks, return_exceptions=True)
            self.assertEqual(started, 15)

    async def test_repeated_cancellation_waits_for_writer_before_cleanup(self):
        started = threading.Event()
        release = threading.Event()
        finished = threading.Event()
        archive = None

        def worker(source, target):
            nonlocal archive
            archive = target
            started.set()
            if not release.wait(5):
                raise TimeoutError("test worker was not released")
            target.write_bytes(b"finished")
            finished.set()

        async def owned_temporary_directory():
            with tempfile.TemporaryDirectory() as folder:
                await packing.pack_directory_async(Path("."), Path(folder) / "archive")

        with patch.object(packing, "pack_directory", worker):
            task = asyncio.create_task(owned_temporary_directory())
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                task.cancel()
                await asyncio.sleep(0.01)
                task.cancel()
                await asyncio.sleep(0.01)
                self.assertFalse(task.done())
                self.assertTrue(archive.parent.exists())
            finally:
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertTrue(finished.is_set())
            self.assertFalse(archive.parent.exists())

    async def test_writer_failure_propagates_and_releases_admission(self):
        with patch.object(packing, "pack_directory", side_effect=OSError("packing failed")):
            for _ in range(5):
                with self.assertRaisesRegex(OSError, "packing failed"):
                    await packing.pack_directory_async(Path("."), Path("unused"))

    async def test_invalid_thread_count_is_rejected(self):
        with patch.dict(os.environ, HARBOR_OPENSANDBOX_PACKING_THREADS="0"), \
                self.assertRaisesRegex(ValueError, "HARBOR_OPENSANDBOX_PACKING_THREADS"):
            await packing.pack_directory_async(Path("."), Path("unused"))


if __name__ == "__main__":
    unittest.main()

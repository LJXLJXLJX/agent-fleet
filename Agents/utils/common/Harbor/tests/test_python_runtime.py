import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from Agents.utils.common.Harbor import python_runtime


class PreparePythonRuntimeTest(unittest.TestCase):
    def test_archive_ready_checks_only_runtime_primitive(self):
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            valid = root / "python-runtime.tar.gz"
            with tarfile.open(valid, "w:gz") as archive:
                for name in (
                    "python3.12-runtime/bin/python3.12",
                    "python3.12-runtime/bin/python3.12.real",
                ):
                    payload = b"#!/bin/sh\nexit 0\n"
                    info = tarfile.TarInfo(name)
                    info.mode = 0o755
                    info.size = len(payload)
                    archive.addfile(info, io.BytesIO(payload))
                stdlib = tarfile.TarInfo("python3.12-runtime/lib/python3.12")
                stdlib.type = tarfile.DIRTYPE
                archive.addfile(stdlib)
                marker_payload = b"target-system-libraries\n"
                marker = tarfile.TarInfo("python3.12-runtime/.harbor-python-runtime-v2")
                marker.size = len(marker_payload)
                archive.addfile(marker, io.BytesIO(marker_payload))

            self.assertTrue(python_runtime.archive_ready(valid))
            self.assertFalse(python_runtime.archive_ready(root / "missing"))

    def test_archive_ready_rejects_runtime_without_portability_marker(self):
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            legacy = root / "legacy-python-runtime.tar.gz"
            with tarfile.open(legacy, "w:gz") as archive:
                for name in (
                    "python3.12-runtime/bin/python3.12",
                    "python3.12-runtime/bin/python3.12.real",
                ):
                    payload = b"#!/bin/sh\nexit 0\n"
                    info = tarfile.TarInfo(name)
                    info.mode = 0o755
                    info.size = len(payload)
                    archive.addfile(info, io.BytesIO(payload))
                stdlib = tarfile.TarInfo("python3.12-runtime/lib/python3.12")
                stdlib.type = tarfile.DIRTYPE
                archive.addfile(stdlib)

            self.assertFalse(python_runtime.archive_ready(legacy))


if __name__ == "__main__":
    unittest.main()

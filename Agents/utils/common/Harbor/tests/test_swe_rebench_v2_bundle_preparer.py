import io
import tarfile
import tempfile
import unittest
from pathlib import Path

from Agents.utils.common.Harbor.verifier_runtime import (
    swe_rebench_v2_bundle_preparer,
)


class SweRebenchV2BundlePreparerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.python_runtime = self.root / "python3.12-runtime.tar.gz"
        with tarfile.open(self.python_runtime, "w:gz") as archive:
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

    def tearDown(self):
        self.temporary.cleanup()

    def test_builds_rebench_bundle_around_runtime_primitive(self):
        output = self.root / "bundle.tar.gz"

        swe_rebench_v2_bundle_preparer.build(self.python_runtime, output)

        self.assertTrue(swe_rebench_v2_bundle_preparer.archive_ready(output))
        root = swe_rebench_v2_bundle_preparer.BUNDLE_ID
        with tarfile.open(output) as archive:
            members = {member.name.rstrip("/"): member for member in archive}
        self.assertIn(f"{root}/bin/python3.12.real", members)
        self.assertEqual(members[f"{root}/bin/python3"].linkname, "python3.12")
        self.assertEqual(members[f"{root}/bin/python"].linkname, "python3.12")
        self.assertTrue(
            members[f"{root}/bin/harbor-verifier-bundle-check"].mode & 0o111
        )
        self.assertIn(f"{root}/.harbor-python-runtime-v2", members)

    def test_preparer_owns_runtime_source_selection(self):
        cache_dir = self.root / "cache"
        cache_dir.mkdir()
        cached_runtime = cache_dir / "python3.12-runtime.tar.gz"
        self.python_runtime.replace(cached_runtime)
        output = self.root / "bundle.tar.gz"

        swe_rebench_v2_bundle_preparer.prepare(cache_dir, output)

        self.assertTrue(swe_rebench_v2_bundle_preparer.archive_ready(output))

    def test_rejects_invalid_runtime_primitive(self):
        invalid = self.root / "invalid.tar.gz"
        invalid.write_text("not an archive", encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "invalid Python runtime archive"):
            swe_rebench_v2_bundle_preparer.build(
                invalid, self.root / "bundle.tar.gz"
            )


if __name__ == "__main__":
    unittest.main()

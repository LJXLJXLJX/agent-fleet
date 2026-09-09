import importlib.util
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).parents[1] / "build.py"
spec = importlib.util.spec_from_file_location("mcp_build", SCRIPT)
build = importlib.util.module_from_spec(spec)
spec.loader.exec_module(build)


class BuildTest(unittest.TestCase):
    def test_source_versioning_cache_reuse_and_corruption_repair(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "exa"
            source.mkdir()
            main = source / "__main__.py"
            main.write_text("print('v1')\n")
            (source / "private.env").write_text("do-not-package")
            (source / "__pycache__").mkdir()
            with patch.object(build, "__file__", str(root / "build.py")):
                cache = root / "cache"
                predicted = build.prepare(cache, path_only=True)
                self.assertFalse(cache.exists())
                first = build.prepare(cache)
                self.assertEqual(first, predicted)
                before = first.stat().st_mtime_ns
                main.touch()
                self.assertEqual(build.prepare(cache), first)
                self.assertEqual(first.stat().st_mtime_ns, before)
                with zipfile.ZipFile(first) as archive:
                    self.assertEqual(archive.namelist(), ["__main__.py"])
                payload = first.read_bytes()
                first.write_bytes(b"incomplete")
                self.assertEqual(build.prepare(cache).read_bytes(), payload)
                main.write_text("print('v2')\n")
                second = build.prepare(cache)
                self.assertNotEqual(first, second)
                self.assertEqual(first.read_bytes(), payload)


if __name__ == "__main__":
    unittest.main()

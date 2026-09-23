"""Check the actual script boundary without requiring Registry or Buildx."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CLI = Path(__file__).resolve().parents[1] / "task_image_manager" / "task_cli.py"


class ImageManagerCliTest(unittest.TestCase):
    def test_absolute_entrypoint_works_outside_repo_without_pythonpath(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [sys.executable, "-I", str(CLI), "--help"],
                cwd=directory,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--bundle-manifest-output", result.stdout)

    def test_operational_failure_exits_nonzero_without_traceback(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(
                [
                    sys.executable, "-I", str(CLI),
                    "--task-dir", str(Path(directory) / "missing"),
                    "--registry", "registry.example", "--project", "test",
                    "--dry-run",
                ],
                cwd=directory,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("task.toml not found", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_dry_run_produces_bundle_through_script_entrypoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = root / "example"
            (task / "environment").mkdir(parents=True)
            (task / "task.toml").write_text("[environment]\nbuild_timeout_sec = 60\n")
            (task / "environment" / "Dockerfile").write_text("FROM scratch\n")
            manifest = root / "bundle.json"
            result = subprocess.run(
                [
                    sys.executable, "-I", str(CLI),
                    "--task-dir", str(task),
                    "--registry", "registry.example", "--project", "test",
                    "--cache-root", str(root / "cache"),
                    "--bundle-manifest-output", str(manifest),
                    "--output", "json", "--dry-run",
                ],
                cwd=directory,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            self.assertEqual(output["bundle_manifest_path"], str(manifest))
            self.assertIn("registry.example/test/example@sha256:", output["main_image_ref"])
            self.assertEqual(set(json.loads(manifest.read_text())["services"]), {"main"})

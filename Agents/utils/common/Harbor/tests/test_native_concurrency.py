import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from Agents.utils.common.Harbor.scripts.monitor_native_harbor import render

HARBOR = Path(__file__).resolve().parents[1]


class NativeConcurrencyTest(unittest.TestCase):
    def test_native_command_selection_images_and_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tools = root / "bin"
            tools.mkdir()
            for name in ("uv", "uvx"):
                tool = tools / name
                tool.write_text("#!/bin/sh\nexit 0\n")
                tool.chmod(0o755)
            for name in ("one", "two"):
                task = root / "data" / name
                (task / "environment").mkdir(parents=True)
                (task / "task.toml").write_text(
                    f'[environment]\ndocker_image="registry.test/{name}:test"\n'
                )
            selected = root / "selected.txt"
            selected.write_text("two\none\n")
            env = {
                "PATH": f"{tools}:{os.environ['PATH']}", "HOME": str(root),
                "AGENT_FLEET_PATHS_FILE": str(root / "absent"),
                "DATASET_NAME": "auto", "DATASET_PATH": str(root / "data"),
                "TASK_SOURCE_FILE": str(selected), "RUN_ID": "native-test",
                "OUTPUT_PATH": str(root / "run"), "AGENT": "oracle",
                "HARBOR_NATIVE_CONCURRENCY": "1", "HARBOR_N_CONCURRENT": "500",
                "HARBOR_DRY_RUN": "1", "HARBOR_ENVIRONMENT_TYPE": "opensandbox",
                "HARBOR_MONITOR_ENABLED": "0", "HARBOR_ANALYZER_ENABLED": "0",
                "HARBOR_TASK_IMAGE_CLI": str(root / "must-not-run"),
                "YICLOUD_PUBLIC_KEY": "test", "YICLOUD_SECRET_KEY": "test",
                "YICLOUD_PROJECT_NAME": "test", "YICLOUD_HARBOR_HOST": "registry.test",
                "YICLOUD_SANDBOX_ENVIRONMENT_ID": "test",
                "YICLOUD_SANDBOX_ENVIRONMENT_NAME": "test",
            }
            command = ["bash", str(HARBOR / "start.sh"), "bash", str(HARBOR / "harboropik.sh")]
            result = subprocess.run(command, env=env, text=True, capture_output=True, timeout=30, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("--n-concurrent 500", result.stdout)
            self.assertIn("--config", result.stdout)
            config = next((root / "run").rglob("native-tasks.json"))
            self.assertEqual(json.loads(config.read_text())["tasks"], [
                {"path": str(root / "data" / name)} for name in ("two", "one")
            ])
            self.assertNotIn("preparing Harbor task image", result.stdout)
            limited = subprocess.run(command, env=env | {"HARBOR_LIMIT": "1"}, text=True,
                                     capture_output=True, timeout=30, check=False)
            self.assertEqual(limited.returncode, 0, limited.stdout + limited.stderr)
            self.assertEqual(len(json.loads(config.read_text())["tasks"]), 1)
            self.assertNotIn(" -l 1", limited.stdout)
            for override in ({"ROLLOUT": "1"}, {"HARBOR_N_CONCURRENT": "0"},
                             {"HARBOR_NATIVE_CONCURRENCY": "yes"},
                             {"HARBOR_OPENSANDBOX_IMAGE_REF": "registry.test/one:test"}):
                failed = subprocess.run(command, env=env | override, text=True,
                                        capture_output=True, timeout=30, check=False)
                self.assertNotEqual(failed.returncode, 0, failed.stdout + failed.stderr)

    def test_monitor_reads_native_job_subdirectory(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            job = parent / "job"
            job.mkdir()
            (job / "result.json").write_text(json.dumps({
                "n_total_trials": 500,
                "stats": {"n_running_trials": 500, "n_pending_trials": 0},
            }))
            for path in (parent, job):
                self.assertIn("Total: 500", render(path))
                self.assertIn("running: 500", render(path))


if __name__ == "__main__":
    unittest.main()

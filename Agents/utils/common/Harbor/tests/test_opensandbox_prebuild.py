"""Exercise prebuild dispatch with stub tools; no network or image publication."""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[5]


class PrebuildFailureTest(unittest.TestCase):
    def run_batch(self, status, *, extra_env=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            script = root / 'Agents/utils/common/Harbor/prebuild_opensandbox_dataset.sh'
            script.parent.mkdir(parents=True)
            shutil.copyfile(REPO / script.relative_to(root), script)
            (root / 'scripts').mkdir()
            shutil.copyfile(REPO / 'scripts/config_loader.sh', root / 'scripts/config_loader.sh')
            (root / 'config.env').write_text('')
            dataset = root / 'dataset'
            for name in ('first', 'second'):
                task = dataset / name
                (task / 'environment').mkdir(parents=True)
                (task / 'task.toml').write_text('')
                (task / 'environment/Dockerfile').write_text('FROM scratch\n')
            tools = root / 'bin'
            tools.mkdir()
            docker = tools / 'docker'
            docker.write_text('#!/bin/sh\nexit 0\n')
            docker.chmod(0o755)
            curl = tools / 'curl'
            curl.write_text(
                '#!/bin/sh\ncase "$*" in *unavailable-health*) exit 1;; esac\nexit 0\n'
            )
            curl.chmod(0o755)
            config = root / 'docker.json'
            config.write_text('{}')
            calls = root / 'calls'
            manager = script.parent / 'manager.py'
            manager.write_text('import os, sys\nfrom pathlib import Path\n'
                               'with Path(os.environ["CALLS"]).open("a") as f: f.write(sys.argv[sys.argv.index("--task-dir") + 1] + "\\n")\n'
                               'expected = os.environ.get("EXPECTED_DOWNLOAD_SOURCE")\n'
                               'if expected is not None: assert sys.argv[sys.argv.index("--download-source-url") + 1] == expected\n'
                               'assert os.environ.get("HARBOR_OPENSANDBOX_PREBUILD_RUN_DIR")\n'
                               'print("test-image")\n'
                               f'sys.exit({status})\n')
            env = {
                'PATH': str(tools) + os.pathsep + os.environ['PATH'],
                'HOME': str(root), 'CALLS': str(calls),
                'YICLOUD_HARBOR_HOST': 'registry.invalid',
                'YICLOUD_HARBOR_PROJECT': 'test',
                'HARBOR_OPENSANDBOX_DOCKER_CONFIG': str(config),
                'HARBOR_OPENSANDBOX_IMAGE_MANAGER': str(manager),
                'HARBOR_OPENSANDBOX_MANAGER_PYTHON': sys.executable,
                'HARBOR_OPENSANDBOX_PREBUILD_ROOT': str(root / 'runs'),
                'HARBOR_OPENSANDBOX_PREBUILD_GC_INTERVAL_SEC': '0',
                'HARBOR_OPENSANDBOX_BUILD_USE_PROXY': '0',
                'HARBOR_OPENSANDBOX_PREBUILD_CONCURRENCY': '1',
            }
            env.update(extra_env or {})
            result = subprocess.run(['bash', str(script), str(dataset), 'test'], env=env,
                                    capture_output=True, text=True, timeout=30, check=False)
            return result, calls.read_text().splitlines()

    def test_shared_frontend_failure_stops_dispatch(self):
        result, calls = self.run_batch(78)
        self.assertEqual(result.returncode, 124, result.stdout + result.stderr)
        self.assertEqual(len(calls), 1)
        self.assertIn('[prebuild][fatal]', result.stdout)
        self.assertNotIn('[prebuild][failed] task=', result.stdout)

    def test_regular_task_failure_does_not_stop_remaining_tasks(self):
        result, calls = self.run_batch(1)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(calls), 2)

    def test_successful_batch_keeps_normal_dispatch(self):
        result, calls = self.run_batch(0)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(calls), 2)

    def test_download_source_is_forwarded_unchanged(self):
        source = 'http://third-party-source.internal/v1/cache'
        result, calls = self.run_batch(
            0,
            extra_env={
                'HARBOR_OPENSANDBOX_DOWNLOAD_SOURCE_URL': source,
                'HARBOR_OPENSANDBOX_PACKAGE_SOURCE_HEALTH_URL': (
                    'http://unavailable-health.invalid/health'
                ),
                'EXPECTED_DOWNLOAD_SOURCE': source,
            },
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(calls), 2)
        self.assertIn('download_source=configured', result.stdout)
        self.assertIn('package_sources=domestic-defaults', result.stdout)


if __name__ == '__main__':
    unittest.main()

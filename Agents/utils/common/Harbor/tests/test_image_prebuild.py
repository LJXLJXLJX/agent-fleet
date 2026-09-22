"""Batch preparation tests without Registry, Buildx, or Sandbox access."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

HARBOR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HARBOR))
from task_image_manager import dataset_cli as batch
from task_image_manager.build_engine.frontend import FrontendBuildError


class DatasetPrebuildTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.dataset = self.root / "dataset"
        for name in ("first", "second", "third"):
            task = self.dataset / name
            (task / "environment").mkdir(parents=True)
            (task / "task.toml").write_text("[environment]\nbuild_timeout_sec = 60\n")
            (task / "environment/Dockerfile").write_text("FROM scratch\n")
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def args(self, *options):
        return batch.parse_args(
            [
                str(self.dataset),
                "test",
                "--registry",
                "registry.example",
                "--project",
                "test",
                "--prebuild-root",
                str(self.root / "runs"),
                "--cache-root",
                str(self.root / "cache"),
                "--dry-run",
                *options,
            ]
        )

    def test_discovery_accepts_compose_and_records_skips(self):
        compose = self.dataset / "second" / "environment"
        (compose / "Dockerfile").unlink()
        (compose / "docker-compose.yaml").write_text("services: {}\n")
        (self.dataset / "third" / "task.toml").unlink()
        tasks, skipped = batch.discover_tasks(self.dataset)
        self.assertEqual([p.name for p in tasks], ["first", "second"])
        self.assertEqual(skipped, ["third\tmissing-task.toml"])

    def test_shared_failure_stops_new_dispatch(self):
        with patch.object(
            batch, "prepare_task_images", side_effect=FrontendBuildError("unavailable")
        ) as prepare:
            status = batch.run(self.args())
        self.assertEqual(status, 124)
        self.assertEqual(prepare.call_count, 1)
        summary = json.loads(
            next((self.root / "runs").glob("*/summary.json")).read_text()
        )
        self.assertEqual(summary["not_dispatched"], 2)

    def test_ordinary_failures_continue_dispatch(self):
        with patch.object(
            batch, "prepare_task_images", side_effect=RuntimeError("bad task")
        ) as prepare:
            status = batch.run(self.args())
        self.assertEqual(status, 123)
        self.assertEqual(prepare.call_count, 3)

    def test_concurrency_is_bounded(self):
        lock = threading.Lock()
        active = maximum = 0

        def prepare(task, args, run_dir):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.03)
            with lock:
                active -= 1
            return batch.TaskResult(task.name, "ready")

        with patch.object(batch, "prepare_task", side_effect=prepare):
            status = batch.run(self.args("--concurrency", "2"))
        self.assertEqual(status, 0)
        self.assertEqual(maximum, 2)

    def test_source_options_are_forwarded_without_batch_retries(self):
        args = self.args(
            "--health-url", "https://health.example",
            "--probe-timeout-sec", "3",
            "--pip-index-url", "https://pip.example/simple",
        )
        with patch.object(batch, "prepare_task_images", side_effect=RuntimeError("failed")) as prepare:
            result = batch.prepare_task(self.dataset / "first", args, self.root)
        self.assertEqual(result.status, "failed")
        prepare.assert_called_once()
        task_args = prepare.call_args.args[0]
        self.assertEqual(task_args.health_url, "https://health.example")
        self.assertEqual(task_args.probe_timeout_sec, 3)
        self.assertEqual(task_args.pip_index_url, "https://pip.example/simple")
        self.assertEqual(args.task_args.pip_index_url, "https://pip.example/simple")

    def test_batch_scope_restored_even_on_interruption(self):
        os.environ[batch.PREFIX + "PREBUILD_RUN_DIR"] = "previous"
        with (
            patch.object(batch, "dispatch", side_effect=KeyboardInterrupt),
            self.assertRaises(KeyboardInterrupt),
        ):
            batch.run(self.args())
        self.assertEqual(os.environ[batch.PREFIX + "PREBUILD_RUN_DIR"], "previous")
        self.assertNotIn("NO_PROXY", os.environ)

    def test_gc_thread_is_joined_on_exit(self):
        args = self.args()
        args.dry_run = False
        args.gc_interval_sec = 0.01
        repeated = threading.Event()
        count = 0

        def prune_once(*args, **kwargs):
            nonlocal count
            count += 1
            if count >= 2:
                repeated.set()

        with patch.object(batch.subprocess, "run", side_effect=prune_once) as prune:
            with batch.periodic_gc(args, self.root):
                self.assertTrue(repeated.wait(2), "periodic GC did not run")
            count_at_exit = prune.call_count
            time.sleep(0.03)
            self.assertEqual(prune.call_count, count_at_exit)
        self.assertFalse(any(t.name == "image-cache-gc" for t in threading.enumerate()))

    def test_invalid_batch_options_fail_before_build(self):
        for options in (
            ("--concurrency", "0"),
            ("--gc-interval-sec", "-1"),
            ("--skip-hash-verification", "--no-reuse-local-upload-cache"),
            ("--task-dir", "/unexpected"),
        ):
            with self.subTest(options=options), self.assertRaises(SystemExit):
                self.args(*options)

    def test_shell_executes_dataset_cli_with_project_config_and_overrides(self):
        # Copy only the wrapper/config loader and a stub dataset CLI to prove
        # path resolution and argument/env handoff without shell dispatch logic.
        repo = self.root / "repo"
        directory = repo / "Agents/utils/common/Harbor/task_image_manager"
        directory.mkdir(parents=True)
        (repo / "scripts").mkdir()
        shutil.copyfile(
            HARBOR / "task_image_manager/prebuild_dataset.sh",
            directory / "prebuild_dataset.sh",
        )
        shutil.copyfile(
            HARBOR.parents[3] / "scripts/config_loader.sh",
            repo / "scripts/config_loader.sh",
        )
        (repo / "config.env").write_text("HARBOR_TASK_IMAGE_PREBUILD_CONCURRENCY=2\n")
        (directory / "dataset_cli.py").write_text(
            "import os, sys\n"
            'assert os.environ["HARBOR_TASK_IMAGE_PREBUILD_CONCURRENCY"] == "3"\n'
            'assert sys.argv[1:] == ["dataset with spaces", "benchmark"]\n'
            "sys.exit(124)\n"
        )
        result = subprocess.run(
            [
                "/bin/bash",
                str(directory / "prebuild_dataset.sh"),
                "dataset with spaces",
                "benchmark",
            ],
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": str(self.root),
                "HARBOR_TASK_IMAGE_PYTHON": sys.executable,
                "HARBOR_TASK_IMAGE_PREBUILD_CONCURRENCY": "3",
            },
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 124, result.stderr)

    def test_real_dataset_cli_dry_run_has_no_backend_dependency(self):
        for backend in ("docker", "opensandbox"):
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    str(HARBOR / "task_image_manager/dataset_cli.py"),
                    str(self.dataset),
                    "test",
                    "--registry",
                    "registry.example",
                    "--project",
                    "test",
                    "--prebuild-root",
                    str(self.root / "runs"),
                    "--cache-root",
                    str(self.root / "cache"),
                    "--dry-run",
                    "--concurrency",
                    "2",
                ],
                env={
                    "PATH": "/usr/bin:/bin",
                    "HOME": str(self.root),
                    "HARBOR_ENVIRONMENT_TYPE": backend,
                },
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
        for summary in (self.root / "runs").glob("*/summary.json"):
            self.assertEqual(json.loads(summary.read_text())["ready"], 3)
            self.assertEqual(len(list((summary.parent / "bundles").glob("*.json"))), 3)

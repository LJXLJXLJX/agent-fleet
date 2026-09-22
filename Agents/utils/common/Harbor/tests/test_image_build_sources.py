"""Source selection is shared by task and dataset preparation, without retries."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from task_image_manager import build_sources, dependency_gateway, task_preparation
from task_image_manager.task_cli import parse_args


class BuildSourcesTest(unittest.TestCase):
    def args(self, *options):
        with patch.dict("os.environ", {}, clear=True):
            return parse_args(
                [
                    "--task-dir",
                    "/tmp/example",
                    "--registry",
                    "registry.example",
                    "--project",
                    "test",
                    "--health-url",
                    "https://health.example",
                    "--pip-index-url",
                    "https://original.example/simple",
                    "--download-source-url",
                    "https://downloads.example",
                    *options,
                ]
            )

    def test_legacy_unavailable_selects_defaults_without_mutating_input(self):
        args = self.args()
        with patch.object(build_sources.subprocess, "run", return_value=Mock(returncode=1)):
            selected = build_sources.select_build_sources(args)
        self.assertEqual(
            selected.pip_index_url, build_sources.DOMESTIC_SOURCES["pip_index_url"]
        )
        self.assertEqual(selected.download_source_url, args.download_source_url)
        self.assertEqual(args.pip_index_url, "https://original.example/simple")

    def test_gateway_precedes_legacy_even_when_unavailable(self):
        args = self.args("--dependency-gateway-url", "https://gateway.example")
        with (
            patch.object(build_sources.subprocess, "run") as legacy,
            patch.object(dependency_gateway, "gateway_available", return_value=False),
        ):
            self.assertIs(build_sources.select_build_sources(args), args)
        legacy.assert_not_called()

    def test_dry_run_skips_both_probes(self):
        args = self.args(
            "--dry-run", "--dependency-gateway-url", "https://gateway.example"
        )
        with (
            patch.object(build_sources.subprocess, "run") as legacy,
            patch.object(dependency_gateway, "gateway_available") as gateway,
        ):
            self.assertIs(build_sources.select_build_sources(args), args)
        legacy.assert_not_called()
        gateway.assert_not_called()

    def test_cache_hit_skips_source_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            args = self.args()
            args.task_dir = Path(directory)
            args.task_repository = "example"
            (args.task_dir / "task.toml").write_text("")
            with (
                patch.object(
                    task_preparation.UploadedBundleCache,
                    "try_restore",
                    return_value="cached",
                ),
                patch.object(task_preparation, "select_build_sources") as select,
            ):
                self.assertEqual(task_preparation.prepare_task_images(args), "cached")
            select.assert_not_called()

    def test_real_task_pipeline_never_retries_service_or_manifest_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "task.toml").write_text("")
            (root / "environment").mkdir()
            (root / "environment" / "Dockerfile").write_text("FROM scratch\n")
            args = self.args(
                "--dependency-gateway-url",
                "https://gateway.example",
                "--force",
                "--no-use-proxy",
                "--bundle-manifest-output",
                str(root / "bundle.json"),
            )
            args.task_dir = root
            args.task_repository = "example"
            args.cache_root = root / "cache"
            for stage in ("service", "manifest", "write"):
                failure = OSError(stage + " failed")
                with (
                    self.subTest(stage=stage),
                    patch.object(
                        dependency_gateway,
                        "gateway_available",
                        side_effect=[True, False],
                    ) as probe,
                    patch.object(
                        task_preparation, "registry_credentials", return_value=("", "")
                    ),
                    patch.object(task_preparation, "SkopeoPublisher"),
                    patch.object(task_preparation, "RegistryClient"),
                    patch.object(
                        task_preparation,
                        "prepare_service_image",
                        return_value={"digest_ref": "registry.example/test/image@sha256:abc"},
                        side_effect=failure if stage == "service" else None,
                    ) as service,
                    patch.object(
                        task_preparation,
                        "assemble_bundle_manifest",
                        return_value={"bundle_identity": "sha256:abc"},
                        side_effect=failure if stage == "manifest" else None,
                    ),
                    patch.object(
                        task_preparation,
                        "atomic_write_json",
                        side_effect=failure if stage == "write" else None,
                    ),
                    self.assertRaises(OSError) as raised,
                ):
                    task_preparation.prepare_task_images(args)
                self.assertIs(raised.exception, failure)
                service.assert_called_once()
                probe.assert_called_once()

    def test_probe_is_bounded_and_bypasses_proxies(self):
        args = self.args("--probe-timeout-sec", "3")
        with patch.object(build_sources.subprocess, "run") as run:
            run.return_value.returncode = 0
            self.assertIs(build_sources.select_build_sources(args), args)
            self.assertIn("--noproxy", run.call_args.args[0])
            self.assertEqual(run.call_args.kwargs["timeout"], 4)
            run.return_value.returncode = 22
            self.assertIsNot(build_sources.select_build_sources(args), args)
            for timeout in (0, -1, float("inf"), float("nan"), 61):
                args.probe_timeout_sec = timeout
                with self.assertRaises(ValueError):
                    build_sources.select_build_sources(args)


if __name__ == "__main__":
    unittest.main()

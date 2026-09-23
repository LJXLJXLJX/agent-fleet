"""Public uploaded-Bundle cache policy and restoration regressions."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from task_image_manager.registry import RegistryTarget
from task_image_manager.uploaded_bundle_cache import UploadedBundleCache


class UploadedBundleCacheTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.cache = UploadedBundleCache(
            self.root,
            RegistryTarget("registry.example", "benchmark", "example"),
            "benchmark",
            "linux/amd64",
            "example",
        )
        self.manifest = json.loads(
            (
                Path(__file__).parent
                / "fixtures/implicit_dockerfile_bundle_manifest.json"
            ).read_text()
        )
        self.loader = Mock(side_effect=AssertionError("task parsing not expected"))
        self.options = {
            "enabled": True,
            "force": False,
            "dry_run": False,
            "skip_hash_verification": True,
            "load_bundle": self.loader,
        }

    def test_fast_restore_copies_manifest_without_task_parsing(self):
        self.cache.store(self.manifest)
        output = self.root / "job" / "bundle.json"
        result = self.cache.try_restore(**self.options, configured_output=output)
        self.assertEqual(result.manifest, self.manifest)
        self.assertEqual(result.manifest_path, output)
        self.assertEqual(json.loads(output.read_text()), self.manifest)
        self.assertEqual(
            result.main_image_ref,
            self.manifest["services"]["main"]["image"]["digest_ref"],
        )
        self.loader.assert_not_called()

    def test_disabled_force_and_dry_run_never_read_or_parse(self):
        self.cache.store(self.manifest)
        for override in ({"enabled": False}, {"force": True}, {"dry_run": True}):
            with (
                self.subTest(override=override),
                patch(
                    "task_image_manager.uploaded_bundle_cache.read_json_record",
                    side_effect=AssertionError("cache read not expected"),
                ),
            ):
                self.assertIsNone(
                    self.cache.try_restore(**{**self.options, **override})
                )
        self.loader.assert_not_called()

    def test_missing_corrupt_and_wrong_target_are_misses_without_output(self):
        output = self.root / "output.json"
        self.assertIsNone(
            self.cache.try_restore(**self.options, configured_output=output)
        )
        self.cache.store(self.manifest)
        entry = next((self.root / "uploaded-bundles").glob("*/*.json"))
        for raw in ("{", "[]", "{}"):
            entry.write_text(raw)
            self.assertIsNone(
                self.cache.try_restore(**self.options, configured_output=output)
            )
        for field, value in (("schema_version", -1), ("task_identity", "different")):
            self.cache.store({**self.manifest, field: value})
            self.assertIsNone(
                self.cache.try_restore(**self.options, configured_output=output)
            )
        self.assertFalse(output.exists())
        self.loader.assert_not_called()

    def test_verified_restore_checks_content_and_propagates_parse_failure(self):
        self.cache.store(self.manifest)
        self.options["skip_hash_verification"] = False
        self.loader.side_effect = None
        self.loader.return_value = SimpleNamespace(
            definition_identity=self.manifest["definition_identity"].removeprefix(
                "sha256:"
            ),
            environment_dir=self.root,
            services={"main": SimpleNamespace(build=object())},
        )
        expected_hash = self.manifest["services"]["main"]["image"]["input_hash"]
        with patch(
            "task_image_manager.task_image_identity.image_identity",
            return_value=expected_hash.removeprefix("sha256:"),
        ) as identity:
            self.assertIsNotNone(self.cache.try_restore(**self.options))
            self.loader.assert_called_once_with()
            identity.return_value = "0" * 64
            self.assertIsNone(self.cache.try_restore(**self.options))
        self.loader.side_effect = ValueError("invalid task")
        with self.assertRaisesRegex(ValueError, "invalid task"):
            self.cache.try_restore(**self.options)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import hashlib
import io
import sys
import tarfile
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from Agents.utils.common.Harbor import prepare_local_deps

MODULE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(MODULE_DIR))

import prepare_dsh_sdk_minimal_cli_runtime as cli_runtime
import prepare_dsh_sdk_minimal_runtime as sdk_runtime


class PrepareDshCliRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_prepare_checks_node_version_from_shared_gzip_cache(self) -> None:
        shared = prepare_local_deps.Config.from_environment({"WHEEL_DIR": str(self.root)})
        with tarfile.open(shared.node_runtime_tarball, "w:gz") as archive:
            for name, payload in (
                ("node/bin/node", b"#!/bin/sh\necho 21\n"),
                ("node/bin/corepack", b"#!/bin/sh\nexit 0\n"),
            ):
                info = tarfile.TarInfo(name)
                info.mode = 0o755
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
        legacy = self.root / "node-runtime.tar.xz"
        legacy.touch()
        prepare_local_deps.DependencyPreparer(shared)._prepare_node_runtime_tarball()
        self.assertFalse(legacy.exists())

        source = self.root / "source.tar.gz"
        with tarfile.open(source, "w:gz") as archive:
            payload = b"lockfileVersion: 9\n"
            info = tarfile.TarInfo("source/pnpm-lock.yaml")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        config = cli_runtime.Config.from_environment(
            {
                "WHEEL_DIR": str(self.root),
                "DSH_CLI_SOURCE_ARCHIVE": str(source),
                "DSH_CLI_SOURCE_ARCHIVE_SHA256": cli_runtime.sha256(source),
            }
        )

        # Reaching the version check proves the shared archive was found,
        # extracted, and executed without a NODE_RUNTIME_TARBALL override.
        with self.assertRaisesRegex(RuntimeError, "DSH requires Node 22 or newer"):
            cli_runtime.prepare(config)

    def test_config_and_cache_require_the_pinned_cli_version(self) -> None:
        config = cli_runtime.Config.from_environment(
            {
                "WHEEL_DIR": str(self.root),
                "DSH_CLI_VERSION": "0.1.3-alpha.1",
                "NPM_CONFIG_REGISTRY": "https://npm.example.test",
            }
        )
        self.assertEqual(
            config.runtime_tarball,
            self.root / "dsh-sdk-minimal-cli-runtime-0.1.3-alpha.1.tar.gz",
        )
        self.assertEqual(config.npm_registry_url, "https://npm.example.test")
        self.assertEqual(config.npm_cache_dir, self.root / "dsh-sdk-minimal-npm-cache")
        self.assertEqual(config.source_sha, cli_runtime.DEFAULT_SOURCE_SHA)
        self.assertIn("pnpm=11.7.0", config.runtime_version)
        self.assertTrue(
            cli_runtime._version_matches("dsh/0.1.3-alpha.1", config.version)
        )
        self.assertFalse(
            cli_runtime._version_matches("dsh/0.1.3-alpha.10", config.version)
        )
        with (
            mock.patch.object(cli_runtime.platform, "machine", return_value="aarch64"),
            self.assertRaisesRegex(RuntimeError, "do not support architecture"),
        ):
            cli_runtime._target()
        with tarfile.open(config.runtime_tarball, "w:gz") as archive:
            for name in ("bin/dsh", "bin/dsh-rg"):
                payload = b"runtime"
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
        config.version_file.write_text("stale\n", encoding="utf-8")
        self.assertFalse(cli_runtime.runtime_ready(config))
        config.version_file.write_text(config.runtime_version, encoding="utf-8")
        self.assertTrue(cli_runtime.runtime_ready(config))

    def test_source_archive_requires_the_pinned_checksum(self) -> None:
        payload = b"source"
        config = cli_runtime.Config.from_environment(
            {
                "WHEEL_DIR": str(self.root),
                "DSH_CLI_SOURCE_ARCHIVE_SHA256": hashlib.sha256(payload).hexdigest(),
            }
        )
        with tarfile.open(config.source_archive, "w:gz") as archive:
            info = tarfile.TarInfo("source")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        self.assertFalse(cli_runtime.source_archive_ready(config))

        digest = cli_runtime.sha256(config.source_archive)
        matching = cli_runtime.Config.from_environment(
            {
                "WHEEL_DIR": str(self.root),
                "DSH_CLI_SOURCE_ARCHIVE_SHA256": digest,
            }
        )
        self.assertTrue(cli_runtime.source_archive_ready(matching))

        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli_runtime.main(["--print-runtime-version"]), 0)
        self.assertEqual(
            output.getvalue().strip(),
            cli_runtime.Config.from_environment().runtime_version,
        )


class PrepareDshSdkRuntimeTests(unittest.TestCase):
    @staticmethod
    def _write_tarball(path: Path) -> None:
        payload = path.with_suffix(".payload")
        payload.write_text("runtime", encoding="utf-8")
        with tarfile.open(path, "w:gz") as archive:
            archive.add(payload, arcname="runtime")

    def test_cache_identity_pins_source_and_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            config = sdk_runtime.Config.from_environment({"WHEEL_DIR": temporary_name})
            self._write_tarball(config.runtime_tarball)
            self._write_tarball(config.python_runtime_tarball)
            config.version_file.write_text(config.source_version, encoding="utf-8")
            self.assertFalse(sdk_runtime.runtime_ready(config))
            config.version_file.write_text(config.runtime_version, encoding="utf-8")
            self.assertTrue(sdk_runtime.runtime_ready(config))
            self.assertEqual(config.source_ref, "dsh-v0.1.3-alpha.1")
            self.assertEqual(
                config.source_sha, "d347e703908d0406b7a7ef80e3a0e594d86b2215"
            )
            self.assertEqual(
                config.source_archive,
                Path(temporary_name)
                / f"deepseek-harness-{sdk_runtime.DEFAULT_SOURCE_SHA}.tar.gz",
            )
            self.assertIn("pydantic==2.13.4", config.runtime_version)
            self.assertIn("python=", config.runtime_version)
            self.assertNotIn("certifi", config.runtime_version)

        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(sdk_runtime.main(["--print-runtime-version"]), 0)
        self.assertEqual(
            output.getvalue().strip(),
            sdk_runtime.Config.from_environment().runtime_version,
        )

    def test_archive_sdk_requirement_uses_a_file_uri(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            config = sdk_runtime.Config.from_environment({"WHEEL_DIR": temporary_name})
            with mock.patch.object(
                sdk_runtime, "source_archive_ready", return_value=True
            ):
                requirement = sdk_runtime.sdk_source_requirement(config)
            self.assertEqual(
                requirement,
                f"{config.source_archive.resolve().as_uri()}#subdirectory=python/sdk",
            )

    def test_python_runtime_must_be_managed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_name:
            root = Path(temporary_name)
            executable = root / "bin" / "python3.12"
            executable.parent.mkdir()
            executable.touch()
            patches = (
                mock.patch.object(sdk_runtime.sys, "executable", str(executable)),
                mock.patch.object(sdk_runtime.sys, "version_info", (3, 12, 0)),
            )
            with (
                patches[0],
                patches[1],
                self.assertRaisesRegex(RuntimeError, "python-build-standalone"),
            ):
                sdk_runtime.managed_python_root()

            (root / "BUILD").touch()
            with (
                mock.patch.object(sdk_runtime.sys, "executable", str(executable)),
                mock.patch.object(sdk_runtime.sys, "version_info", (3, 12, 0)),
            ):
                self.assertEqual(sdk_runtime.managed_python_root(), root)

            archive = root / "python.tar.gz"
            with tarfile.open(archive, "w:gz") as tar:
                tar.add(
                    root / "BUILD",
                    arcname="dsh-sdk-minimal-python3.12-runtime/BUILD",
                )
            self.assertTrue(sdk_runtime.python_runtime_ready(archive, root))
            (root / "BUILD").write_text("new-build", encoding="utf-8")
            self.assertFalse(sdk_runtime.python_runtime_ready(archive, root))


if __name__ == "__main__":
    unittest.main()

import hashlib
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request

HARBOR_DIR = Path(__file__).resolve().parents[1]
if str(HARBOR_DIR) not in os.sys.path:
    os.sys.path.insert(0, str(HARBOR_DIR))

from opensandbox_s3_upload import (
    S3UploadStore,
    S3WriteUnavailableError,
    _direct_urlopen,
)


class S3UploadStoreTest(unittest.TestCase):
    def make_store(self, root: Path, *, writer: bool = True) -> S3UploadStore:
        config = None
        if writer:
            config = root / "s3cfg"
            config.write_text("[default]\n", encoding="utf-8")
            config.chmod(0o600)
        return S3UploadStore(
            config_path=config,
            bucket="cache",
            read_origin="http://ceph.example/cache",
            cache_root=root / "cache",
            lock_root=root / "locks",
            directory_compression="none",
        )

    @staticmethod
    def anonymous_response(size: int) -> MagicMock:
        response = MagicMock()
        response.__enter__.return_value.headers = {"Content-Length": str(size)}
        return response

    def publish_locally(self, store: S3UploadStore):
        return patch.object(store, "_ensure_remote")

    def test_anonymous_probe_bypasses_ambient_proxy(self) -> None:
        opener = MagicMock()
        response = MagicMock()
        opener.open.return_value = response
        request = Request("http://ceph.example/cache/object", method="HEAD")

        with patch(
            "opensandbox_s3_upload.build_opener", return_value=opener
        ) as build:
            self.assertIs(_direct_urlopen(request, 15), response)

        handler = build.call_args.args[0]
        self.assertIsInstance(handler, ProxyHandler)
        self.assertEqual(handler.proxies, {})
        opener.open.assert_called_once_with(request, timeout=15)

    def test_file_uses_stable_content_addressed_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "agent.tgz"
            source.write_bytes(b"agent-runtime")
            store = self.make_store(root)
            remote = self.publish_locally(store)

            with remote:
                first = store.stage_file(source)
                second = store.stage_file(source)

            digest = hashlib.sha256(b"agent-runtime").hexdigest()
            self.assertEqual(first.object_key, second.object_key)
            self.assertEqual(first.logical_digest, digest)
            self.assertIn(f"/{digest[:2]}/{digest}/", first.object_key)
            self.assertTrue(Path(first.local_payload_path).is_file())
            self.assertEqual(
                first.download_url,
                f"http://ceph.example/cache/{first.object_key}",
            )
            self.assertNotIn("?", first.download_url)

    def test_directory_archive_preserves_modes_and_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "task"
            source.mkdir()
            executable = source / "run.sh"
            executable.write_text("#!/bin/sh\n", encoding="utf-8")
            executable.chmod(0o755)
            (source / "run-link").symlink_to("run.sh")
            store = self.make_store(root)
            remote = self.publish_locally(store)

            with remote:
                artifact = store.stage_directory(source)
            extracted = root / "extracted"
            extracted.mkdir()
            with tarfile.open(artifact.local_payload_path, "r:*") as archive:
                archive.extractall(extracted)

            self.assertEqual((extracted / "run.sh").stat().st_mode & 0o777, 0o755)
            self.assertEqual(os.readlink(extracted / "run-link"), "run.sh")

    def test_read_origin_rejects_credentials_query_and_fragment(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "s3cfg"
            config.write_text("[default]\n", encoding="utf-8")
            for origin in (
                "http://key@ceph.example/cache",
                "http://ceph.example/cache?signature=test",
                "http://ceph.example/cache#fragment",
            ):
                with self.subTest(origin=origin), self.assertRaises(ValueError):
                    S3UploadStore(
                        config_path=config,
                        bucket="cache",
                        read_origin=origin,
                        cache_root=root / "cache",
                        lock_root=root / "locks",
                    )

    def test_existing_anonymous_object_does_not_require_writer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "agent.tgz"
            source.write_bytes(b"agent-runtime")
            store = self.make_store(root, writer=False)

            with (
                patch(
                    "opensandbox_s3_upload._direct_urlopen",
                    return_value=self.anonymous_response(
                        source.stat().st_size
                    ),
                ) as probe,
                patch.object(store, "_run") as credentialed,
            ):
                artifact = store.stage_file(source)

            request = probe.call_args.args[0]
            self.assertEqual(request.get_method(), "HEAD")
            self.assertEqual(artifact.payload_size, source.stat().st_size)
            self.assertNotIn("?", artifact.download_url)
            credentialed.assert_not_called()

    def test_missing_anonymous_object_without_writer_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "agent.tgz"
            source.write_bytes(b"agent-runtime")
            store = self.make_store(root, writer=False)
            missing = HTTPError(
                "http://ceph.example/cache/object",
                404,
                "Not Found",
                None,
                None,
            )

            with (
                patch(
                    "opensandbox_s3_upload._direct_urlopen", side_effect=missing
                ),
                self.assertRaises(S3WriteUnavailableError),
            ):
                store.stage_file(source)

    def test_insecure_writer_is_not_used_after_anonymous_miss(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "agent.tgz"
            source.write_bytes(b"agent-runtime")
            store = self.make_store(root)
            assert store.config_path is not None
            store.config_path.chmod(0o644)
            missing = HTTPError(
                "http://ceph.example/cache/object",
                404,
                "Not Found",
                None,
                None,
            )

            with (
                patch(
                    "opensandbox_s3_upload._direct_urlopen", side_effect=missing
                ),
                patch("opensandbox_s3_upload.subprocess.run") as run,
                self.assertRaises(PermissionError),
            ):
                store.stage_file(source)

            run.assert_not_called()

    def test_insecure_writer_is_irrelevant_for_existing_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "agent.tgz"
            source.write_bytes(b"agent-runtime")
            store = self.make_store(root)
            assert store.config_path is not None
            store.config_path.chmod(0o644)

            with (
                patch(
                    "opensandbox_s3_upload._direct_urlopen",
                    return_value=self.anonymous_response(
                        source.stat().st_size
                    ),
                ),
                patch("opensandbox_s3_upload.subprocess.run") as run,
            ):
                store.stage_file(source)

            run.assert_not_called()

    def test_missing_object_uses_safe_writer_then_verifies_anonymous_read(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "agent.tgz"
            source.write_bytes(b"agent-runtime")
            store = self.make_store(root)

            with (
                patch.object(
                    store,
                    "_anonymous_remote_size",
                    side_effect=[
                        (False, None),
                        (True, source.stat().st_size),
                    ],
                ),
                patch.object(store, "_run") as run,
            ):
                artifact = store.stage_file(source)

            run.assert_called_once_with(
                "--no-progress",
                "put",
                artifact.local_payload_path,
                artifact.object_uri,
            )

    def test_anonymous_forbidden_uses_authenticated_exact_key_probe(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "agent.tgz"
            source.write_bytes(b"agent-runtime")
            store = self.make_store(root)
            forbidden = HTTPError(
                "http://ceph.example/cache/object",
                403,
                "Forbidden",
                None,
                None,
            )
            missing = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )
            uploaded = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="", stderr=""
            )

            with (
                patch(
                    "opensandbox_s3_upload._direct_urlopen",
                    side_effect=[
                        forbidden,
                        self.anonymous_response(source.stat().st_size),
                    ],
                ),
                patch.object(store, "_run", side_effect=[missing, uploaded]) as run,
            ):
                artifact = store.stage_file(source)

            self.assertEqual(run.call_args_list[0].args, ("ls", artifact.object_uri))
            self.assertEqual(
                run.call_args_list[1].args,
                (
                    "--no-progress",
                    "put",
                    artifact.local_payload_path,
                    artifact.object_uri,
                ),
            )

    def test_anonymous_forbidden_existing_private_object_is_not_overwritten(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "agent.tgz"
            source.write_bytes(b"agent-runtime")
            store = self.make_store(root)
            forbidden = HTTPError(
                "http://ceph.example/cache/object",
                403,
                "Forbidden",
                None,
                None,
            )

            def authenticated_ls(*args: str, **_kwargs: object):
                object_uri = args[-1]
                return subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout=f"2026-08-28 00:00 13 {object_uri}\n",
                    stderr="",
                )

            with (
                patch(
                    "opensandbox_s3_upload._direct_urlopen",
                    side_effect=forbidden,
                ),
                patch.object(store, "_run", side_effect=authenticated_ls) as run,
                self.assertRaisesRegex(RuntimeError, "not anonymously readable"),
            ):
                store.stage_file(source)

            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[0], "ls")

    def test_anonymous_forbidden_without_writer_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "agent.tgz"
            source.write_bytes(b"agent-runtime")
            store = self.make_store(root, writer=False)
            forbidden = HTTPError(
                "http://ceph.example/cache/object",
                403,
                "Forbidden",
                None,
                None,
            )

            with (
                patch(
                    "opensandbox_s3_upload._direct_urlopen",
                    side_effect=forbidden,
                ),
                self.assertRaises(S3WriteUnavailableError),
            ):
                store.stage_file(source)

    def test_preflight_without_writer_does_not_require_s3cmd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = self.make_store(root, writer=False)

            with patch.object(store, "_run") as run:
                store.preflight()

            self.assertTrue(store.cache_root.is_dir())
            self.assertTrue(store.lock_root.is_dir())
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

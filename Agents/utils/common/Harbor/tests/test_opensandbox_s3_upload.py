import hashlib
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HARBOR_DIR = Path(__file__).resolve().parents[1]
if str(HARBOR_DIR) not in os.sys.path:
    os.sys.path.insert(0, str(HARBOR_DIR))

from opensandbox_s3_upload import S3UploadStore  # noqa: E402


class S3UploadStoreTest(unittest.TestCase):
    def make_store(self, root: Path) -> S3UploadStore:
        config = root / "s3cfg"
        config.write_text("[default]\n", encoding="utf-8")
        config.chmod(0o600)
        return S3UploadStore(
            config_path=config,
            bucket="cache",
            cache_root=root / "cache",
            lock_root=root / "locks",
            directory_compression="none",
        )

    def publish_locally(self, store: S3UploadStore):
        return (
            patch.object(store, "_ensure_remote"),
            patch.object(
                store,
                "_signed_url",
                return_value="http://ceph.example/cache/object?signature=test",
            ),
        )

    def test_file_uses_stable_content_addressed_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "agent.tgz"
            source.write_bytes(b"agent-runtime")
            store = self.make_store(root)
            remote, signed = self.publish_locally(store)

            with remote, signed:
                first = store.stage_file(source)
                second = store.stage_file(source)

            digest = hashlib.sha256(b"agent-runtime").hexdigest()
            self.assertEqual(first.object_key, second.object_key)
            self.assertEqual(first.logical_digest, digest)
            self.assertIn(f"/{digest[:2]}/{digest}/", first.object_key)
            self.assertTrue(Path(first.local_payload_path).is_file())

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
            remote, signed = self.publish_locally(store)

            with remote, signed:
                artifact = store.stage_directory(source)
            extracted = root / "extracted"
            extracted.mkdir()
            with tarfile.open(artifact.local_payload_path, "r:*") as archive:
                archive.extractall(extracted)

            self.assertEqual((extracted / "run.sh").stat().st_mode & 0o777, 0o755)
            self.assertEqual(os.readlink(extracted / "run-link"), "run.sh")

    def test_virtual_host_signed_url_is_rewritten_to_path_style(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self.make_store(Path(tmp))
            rewritten = store._path_style_url(
                "http://cache.ceph.example:80/prefix/object?sig=abc"
            )
            self.assertEqual(
                rewritten,
                "http://ceph.example:80/cache/prefix/object?sig=abc",
            )


if __name__ == "__main__":
    unittest.main()

import hashlib
import io
import json
import sys
import tarfile
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import Mock, patch

HARBOR_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HARBOR_DIR))

from opensandbox_image_manager import (  # noqa: E402
    DOCKER_CONFIG,
    DOCKER_LAYER_GZIP,
    DOCKER_MANIFEST,
    OCI_CONFIG,
    OCI_LAYER_GZIP,
    SourcePolicy,
    environment_content_hash,
    prepare,
    render_build_dockerfile,
    run_build,
    schema2_manifest,
    validate_single_container_task,
)


def sha256(data: bytes) -> str:
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def add_tar_bytes(archive: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    archive.addfile(info, io.BytesIO(data))


class OpenSandboxImageManagerTest(unittest.TestCase):
    def make_task(self, root: Path, name: str = "0") -> Path:
        task = root / name
        environment = task / "environment"
        environment.mkdir(parents=True)
        (task / "task.toml").write_text(
            "[environment]\nbuild_timeout_sec = 60\n", encoding="utf-8"
        )
        (environment / "Dockerfile").write_text(
            "FROM ubuntu:24.04\nRUN echo ok\n", encoding="utf-8"
        )
        return task

    def test_content_hash_is_stable_and_ignores_generated_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            environment = self.make_task(Path(tmp)) / "environment"
            first = environment_content_hash(environment)
            (environment / "__pycache__").mkdir()
            (environment / "__pycache__" / "ignored.pyc").write_bytes(b"ignored")
            self.assertEqual(first, environment_content_hash(environment))
            (environment / "payload.txt").write_text("changed", encoding="utf-8")
            self.assertNotEqual(first, environment_content_hash(environment))

    def test_compose_task_fails_fast(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task = self.make_task(Path(tmp))
            (task / "environment" / "docker-compose.yaml").write_text(
                "services: {}\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "does not support compose"):
                validate_single_container_task(task)

    def test_render_uses_domestic_sources_and_preserves_stage_alias(self) -> None:
        rendered = render_build_dockerfile(
            "FROM ubuntu:24.04 AS builder\nRUN echo build\nFROM builder\n",
            SourcePolicy(
                "m.daocloud.io/docker.io",
                "https://mirrors.tuna.tsinghua.edu.cn",
            ),
        )
        self.assertIn("FROM m.daocloud.io/docker.io/library/ubuntu:24.04 AS builder", rendered)
        self.assertIn("mirrors.tuna.tsinghua.edu.cn/ubuntu/", rendered)
        self.assertIn("FROM builder\n", rendered)
        self.assertNotIn("docker.io/library/builder", rendered)
        self.assertEqual(rendered.count("RUN set -eu;"), 1)

    def test_oci_build_disables_default_provenance_attestation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            process = Mock()
            process.wait.return_value = 0
            with (
                patch(
                    "opensandbox_image_manager.subprocess.run",
                    return_value=Mock(returncode=0),
                ),
                patch(
                    "opensandbox_image_manager.subprocess.Popen",
                    return_value=process,
                ) as popen,
            ):
                run_build(
                    environment_dir=root,
                    dockerfile=root / "Dockerfile",
                    archive_path=root / "image.oci.tar",
                    log_path=root / "build.log",
                    platform="linux/amd64",
                    timeout_sec=60,
                    build_args={},
                )

        command = popen.call_args.args[0]
        self.assertIn("--provenance=false", command)

    def test_schema2_conversion_keeps_blob_digests(self) -> None:
        config = b'{"architecture":"amd64","os":"linux"}'
        layer = b"compressed-layer-placeholder"
        source_manifest = json.dumps(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": {
                    "mediaType": OCI_CONFIG,
                    "digest": sha256(config),
                    "size": len(config),
                },
                "layers": [
                    {
                        "mediaType": OCI_LAYER_GZIP,
                        "digest": sha256(layer),
                        "size": len(layer),
                    }
                ],
            },
            separators=(",", ":"),
        ).encode()
        index = json.dumps(
            {
                "schemaVersion": 2,
                "manifests": [
                    {
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "digest": sha256(source_manifest),
                        "size": len(source_manifest),
                    }
                ],
            },
            separators=(",", ":"),
        ).encode()

        with tempfile.TemporaryDirectory() as tmp:
            archive_path = Path(tmp) / "image.tar"
            with tarfile.open(archive_path, "w") as archive:
                add_tar_bytes(archive, "index.json", index)
                for data in (source_manifest, config, layer):
                    add_tar_bytes(archive, f"blobs/sha256/{sha256(data).split(':')[1]}", data)
            with tarfile.open(archive_path, "r") as archive:
                manifest, descriptors = schema2_manifest(archive)

        self.assertEqual(manifest["mediaType"], DOCKER_MANIFEST)
        self.assertEqual(manifest["config"]["mediaType"], DOCKER_CONFIG)
        self.assertEqual(manifest["layers"][0]["mediaType"], DOCKER_LAYER_GZIP)
        self.assertEqual([item["digest"] for item in descriptors], [sha256(config), sha256(layer)])

    def test_dry_run_returns_platform_image_ref_without_external_access(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_task(root, "0")
            args = Namespace(
                task_dir=None,
                dataset_root=root,
                include="0",
                registry="registry.gate.yicloud.com.cn",
                repository="test-project/example",
                sandbox_image_prefix="test-project/example",
                docker_config=root / "missing-config.json",
                cache_root=root / "cache",
                platform="linux/amd64",
                tag_prefix="harbor",
                dockerhub_mirror_prefix="m.daocloud.io/docker.io",
                apt_mirror="https://mirrors.tuna.tsinghua.edu.cn",
                build_args_json="{}",
                force=False,
                use_proxy=False,
                dry_run=True,
            )
            image_ref = prepare(args)
        self.assertRegex(image_ref, r"^test-project/example:harbor-0-[0-9a-f]{20}$")


if __name__ == "__main__":
    unittest.main()

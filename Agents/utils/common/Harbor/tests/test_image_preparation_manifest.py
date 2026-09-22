import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

HARBOR_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HARBOR_DIR))

from compose_bundle import BuildSpec, BundleSpec, ServiceSpec
from task_image_manager import build_engine, image_build_state, uploaded_bundle_cache
from task_image_manager import oci as image_oci
from task_image_manager import registry as image_registry
from task_image_manager import task_cli as manager
from task_image_manager import task_image_identity as image_identity
from task_image_manager import task_preparation as image_planner
from task_image_manager.task_bundle import manifest as image_manifest

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "implicit_dockerfile_bundle_manifest.json"


def implicit_dockerfile_bundle():
    environment = Path("/tasks/example/environment")
    service = ServiceSpec(
        name="main",
        build=BuildSpec(
            environment,
            environment / "Dockerfile",
            {"PIP_INDEX_URL": "https://pypi.example/simple"},
            None,
        ),
        source_image=None,
        entrypoint=None,
        entrypoint_present=False,
        command=None,
        command_present=False,
        working_dir=None,
        environment={"MODE": "test"},
        ports=[{"target": 8080, "protocol": "tcp"}],
        expose=["22/tcp"],
        aliases=["app"],
        depends_on={},
        healthcheck=None,
        volumes=[],
        networks={},
        cap_add=[],
        privileged=False,
        container_name=None,
        resources={},
        unsupported_fields=[],
    )
    bundle = BundleSpec(
        task_dir=Path("/tasks/example"),
        environment_dir=environment,
        definition_kind="dockerfile",
        main_service="main",
        services={"main": service},
        requirements={"multi_service": False},
        normalization_backend="dockerfile",
    )
    digest = "sha256:" + "2" * 64
    artifact = {
        "source": "build",
        "input_hash": "sha256:" + "1" * 64,
        "tag": "main-" + "1" * 20,
        "tag_ref": "registry.example/benchmark/example:main-" + "1" * 20,
        "artifact_digest": digest,
        "digest_ref": f"registry.example/benchmark/example@{digest}",
        "media_type": image_oci.DOCKER_MANIFEST,
        "platform": "linux/amd64",
        "build_arg_names": ["PIP_INDEX_URL"],
        "config": {
            "entrypoint": ["/usr/bin/python3"],
            "cmd": ["-c", "print(1)"],
            "working_dir": "/app",
            "exposed_ports": [{"port": 8080, "protocol": "tcp"}],
            "healthcheck": None,
        },
        "config_resolved": True,
    }
    manifest = image_manifest.assemble_bundle_manifest(
        bundle,
        {"main": artifact},
        benchmark="benchmark",
        registry_host="registry.example",
        project="benchmark",
        task_repository="example",
        repository="benchmark/example",
    )
    return manifest


class ImagePreparationManifestTest(unittest.TestCase):
    def test_facade_reexports_split_implementations(self) -> None:
        self.assertIs(manager.prepare_task_images, image_planner.prepare_task_images)
        self.assertIs(manager.run_build, build_engine.run_build)
        self.assertIs(manager.SkopeoPublisher, image_registry.SkopeoPublisher)
        self.assertIs(manager.schema2_manifest, image_oci.schema2_manifest)
        self.assertTrue(all(not name.startswith("_") for name in manager.__all__))
        self.assertIs(
            manager.environment_content_hash, image_identity.environment_content_hash
        )

    def test_bundle_assembly_rejects_missing_or_extra_image_artifacts(self) -> None:
        # The public boundary must not silently omit a service from the Bundle.
        from types import SimpleNamespace

        for artifacts in ({}, {"main": {}, "extra": {}}):
            with self.subTest(artifacts=artifacts), self.assertRaisesRegex(ValueError, "service names"):
                image_manifest.assemble_bundle_manifest(
                    SimpleNamespace(services={"main": object()}), artifacts,
                    benchmark="test", registry_host="registry.example",
                    project="test", task_repository="example", repository="test/example",
                )

    def test_implicit_dockerfile_bundle_matches_golden_manifest(self) -> None:
        manifest = json.loads(json.dumps(implicit_dockerfile_bundle()))
        golden = json.loads(FIXTURE.read_text(encoding="utf-8"))

        self.assertEqual(manifest, golden)
        self.assertEqual(manifest["schema_version"], 2)
        self.assertEqual(manifest["bundle_format"], "harbor-environment-bundle-v2")
        image = manifest["services"]["main"]["image"]
        self.assertEqual(
            image["media_type"],
            "application/vnd.docker.distribution.manifest.v2+json",
        )
        self.assertEqual(
            image["digest_ref"],
            "registry.example/benchmark/example@" + image["artifact_digest"],
        )
        self.assertEqual(
            manifest["services"]["main"]["runtime"]["start_argv"],
            [
                "/usr/bin/python3",
                "sh",
                "-c",
                "while :; do sleep 60; done",
            ],
        )
        self.assertEqual(
            manifest["services"]["main"]["runtime"]["start_argv_source"],
            "image-config.entrypoint+adapter.legacy-keepalive",
        )

    def test_cached_bundle_reader_rejects_incompatible_manifests(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)

        def accepts(manifest, *, target, benchmark, task_identity, platform):
            cache = uploaded_bundle_cache.UploadedBundleCache(
                Path(temporary.name), target, benchmark, platform, task_identity,
            )
            cache.store(manifest)
            return cache.try_restore(
                enabled=True, force=False, dry_run=False, skip_hash_verification=True,
                load_bundle=Mock(side_effect=AssertionError("unexpected task parse")),
            ) is not None

        manifest = implicit_dockerfile_bundle()
        target = image_registry.RegistryTarget("registry.example", "benchmark", "example")
        self.assertTrue(
            accepts(
                manifest,
                target=target,
                benchmark="benchmark",
                task_identity="example",
                platform="linux/amd64",
            )
        )

        incompatible = json.loads(json.dumps(manifest))
        incompatible["schema_version"] = 1
        self.assertFalse(
            accepts(
                incompatible,
                target=target,
                benchmark="benchmark",
                task_identity="example",
                platform="linux/amd64",
            )
        )

        incompatible = json.loads(json.dumps(manifest))
        incompatible["bundle_format"] = "harbor-environment-bundle-v1"
        self.assertFalse(
            accepts(
                incompatible,
                target=target,
                benchmark="benchmark",
                task_identity="example",
                platform="linux/amd64",
            )
        )

        incompatible = json.loads(json.dumps(manifest))
        incompatible["services"]["main"]["image"]["digest_ref"] = (
            "registry.example/benchmark/example:not-a-digest"
        )
        self.assertFalse(
            accepts(
                incompatible,
                target=target,
                benchmark="benchmark",
                task_identity="example",
                platform="linux/amd64",
            )
        )

        incompatible = json.loads(json.dumps(manifest))
        incompatible["services"]["main"]["image"]["artifact_digest"] = "sha256:" + "3" * 64
        self.assertFalse(
            accepts(
                incompatible,
                target=target,
                benchmark="benchmark",
                task_identity="example",
                platform="linux/amd64",
            )
        )

    def test_image_build_state_paths_follow_repository_and_tag(self) -> None:
        root = Path("/cache")
        lock_path, record_path, log_path = image_build_state.image_build_state_paths(
            root, "benchmark/example", "main-abc", "example"
        )
        key = hashlib.sha256(b"benchmark/example").hexdigest()[:16]
        self.assertEqual(lock_path, root / "locks" / "images" / f"{key}-main-abc.lock")
        self.assertEqual(
            record_path, root / "records" / "images" / key / "main-abc.json"
        )
        self.assertEqual(log_path, root / "logs" / "example" / "main-abc.log")

    def test_skopeo_copy_publishes_docker_schema2_and_checks_digest(self) -> None:
        target = image_registry.RegistryTarget("registry.example", "benchmark", "example")
        publisher = image_registry.SkopeoPublisher(
            target, "user", "password", tls_verify=False
        )
        digest = "sha256:" + "a" * 64
        commands: list[list[str]] = []

        def fake_run(command: list[str], **kwargs):
            if command[1] == "login":
                return Mock(returncode=0, stdout="", stderr="")
            commands.append(command)
            if command[1] == "copy":
                Path(command[command.index("--digestfile") + 1]).write_text(
                    digest, encoding="utf-8"
                )
                return Mock(returncode=0, stdout="", stderr="")
            if command[1] == "inspect":
                return Mock(returncode=0, stdout=digest + "\n", stderr="")
            raise AssertionError(command)

        transport = patch.object(image_registry.subprocess, "run", side_effect=fake_run)
        transport.start()
        try:
            inspected = publisher.copy(
                "/tmp/image.oci.tar",
                "registry.example/benchmark/example:main",
                source_is_archive=True,
            )
        finally:
            transport.stop()
            publisher.close()

        copy_command = commands[0]
        self.assertEqual(copy_command[copy_command.index("--format") + 1], "v2s2")
        self.assertEqual(copy_command[-2], "oci-archive:/tmp/image.oci.tar")
        self.assertEqual(
            copy_command[-1], "docker://registry.example/benchmark/example:main"
        )
        self.assertEqual(inspected["artifact_digest"], digest)
        self.assertEqual(inspected["media_type"], image_oci.DOCKER_MANIFEST)

        publisher = image_registry.SkopeoPublisher(
            target, "user", "password", tls_verify=False
        )

        def mismatched(command: list[str], **kwargs):
            if command[1] == "login":
                return Mock(returncode=0, stdout="", stderr="")
            if command[1] == "copy":
                Path(command[command.index("--digestfile") + 1]).write_text(
                    digest, encoding="utf-8"
                )
                return Mock(returncode=0, stdout="", stderr="")
            if command[1] == "inspect":
                return Mock(returncode=0, stdout="sha256:" + "b" * 64 + "\n", stderr="")
            raise AssertionError(command)

        transport = patch.object(image_registry.subprocess, "run", side_effect=mismatched)
        transport.start()
        try:
            with self.assertRaisesRegex(RuntimeError, "digest mismatch"):
                publisher.copy(
                    "/tmp/image.oci.tar",
                    "registry.example/benchmark/example:main",
                    source_is_archive=True,
                )
        finally:
            transport.stop()
            publisher.close()


class BundleDependencyBoundaryTest(unittest.TestCase):
    def test_bundle_import_does_not_load_preparation_or_image_operations(self):
        for prefix, root in (("task_image_manager", HARBOR_DIR),
                             ("Agents.utils.common.Harbor.task_image_manager", HARBOR_DIR.parents[3])):
            with self.subTest(prefix=prefix):
                code = (
                    f"import sys; sys.path.insert(0, {str(root)!r}); "
                    f"from {prefix}.task_bundle import PreparedBundle, assemble_bundle_manifest; "
                    f"prefix = {prefix!r}; "
                    "blocked = ('build_engine', 'registry', 'task_preparation', 'service_images', "
                    "'uploaded_bundle_cache', 'image_build_state', 'oci', 'task_image_identity'); "
                    "assert not any(name == prefix + '.' + part or name.startswith(prefix + '.' + part + '.') "
                    "for name in sys.modules for part in blocked)"
                )
                result = subprocess.run([sys.executable, "-I", "-c", code],
                                        capture_output=True, text=True, timeout=30, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()

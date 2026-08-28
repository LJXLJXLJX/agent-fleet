import sys
import tempfile
import unittest
from pathlib import Path

HARBOR_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HARBOR_DIR))

from compose_bundle import resolve_bundle_spec


class ComposeBundleTest(unittest.TestCase):
    def make_task(self, root: Path, compose: str | None = None) -> Path:
        task = root / "973"
        environment = task / "environment"
        environment.mkdir(parents=True)
        (task / "task.toml").write_text("[environment]\n", encoding="utf-8")
        (environment / "Dockerfile").write_text(
            "FROM ubuntu:24.04\n", encoding="utf-8"
        )
        if compose is not None:
            (environment / "docker-compose.yaml").write_text(
                compose, encoding="utf-8"
            )
        return task

    def test_dockerfile_becomes_single_main_service(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            spec = resolve_bundle_spec(self.make_task(Path(tmp)))

        self.assertEqual(spec.definition_kind, "dockerfile")
        self.assertEqual(spec.main_service, "main")
        self.assertEqual(list(spec.services), ["main"])
        self.assertFalse(spec.requirements["multi_service"])
        self.assertEqual(len(spec.definition_identity), 64)

    def test_compose_builds_named_service_specs_and_dependencies(self) -> None:
        compose = """
services:
  main:
    build:
      context: ${CONTEXT_DIR}
    command: [sh, -c, "sleep infinity"]
    deploy:
      resources:
        limits:
          cpus: 2
    depends_on:
      worker:
        condition: service_healthy
  worker:
    build:
      dockerfile: Dockerfile.worker
      args:
        FEATURE: enabled
    hostname: worker-host
    networks:
      locale-net:
        aliases: [worker-alias]
    healthcheck:
      test: [CMD, test, -f, /ready]
networks:
  locale-net:
    driver: bridge
"""
        with tempfile.TemporaryDirectory() as tmp:
            task = self.make_task(Path(tmp), compose)
            (task / "environment" / "Dockerfile.worker").write_text(
                "FROM alpine:3.20\n", encoding="utf-8"
            )
            spec = resolve_bundle_spec(task)

        self.assertEqual(spec.definition_kind, "compose")
        self.assertEqual(set(spec.services), {"main", "worker"})
        self.assertEqual(
            spec.services["main"].depends_on["worker"]["condition"],
            "service_healthy",
        )
        self.assertEqual(
            spec.services["worker"].aliases,
            ["worker", "worker-alias", "worker-host"],
        )
        self.assertEqual(spec.services["worker"].build.args, {"FEATURE": "enabled"})
        self.assertEqual(
            spec.services["main"].resources,
            {"limits": {"cpus": 2}},
        )
        self.assertTrue(spec.requirements["multi_service"])
        self.assertFalse(spec.requirements["multiple_networks"])

    def test_requirements_preserve_platform_gaps(self) -> None:
        compose = """
services:
  main:
    build: .
    networks:
      corp:
        ipv4_address: 10.1.0.2
    depends_on: [worker]
  worker:
    build:
      dockerfile: Dockerfile.worker
    cap_add: [NET_ADMIN]
    privileged: true
    volumes: [shared:/data]
    networks: [corp, external]
networks:
  corp:
    ipam:
      config:
        - subnet: 10.1.0.0/24
  external: {}
volumes:
  shared: {}
"""
        with tempfile.TemporaryDirectory() as tmp:
            task = self.make_task(Path(tmp), compose)
            (task / "environment" / "Dockerfile.worker").write_text(
                "FROM alpine:3.20\n", encoding="utf-8"
            )
            requirements = resolve_bundle_spec(task).requirements

        self.assertTrue(requirements["fixed_ip"])
        self.assertTrue(requirements["multiple_networks"])
        self.assertTrue(requirements["shared_volumes"])
        self.assertTrue(requirements["net_admin"])
        self.assertTrue(requirements["privileged"])
        self.assertIn("services.worker.privileged", requirements["unsupported_features"])

    def test_bind_mounts_are_reported_as_unsupported(self) -> None:
        compose = """
services:
  main:
    build: .
    volumes:
      - ./cache:/cache
      - type: bind
        source: /host/data
        target: /data
"""
        with tempfile.TemporaryDirectory() as tmp:
            requirements = resolve_bundle_spec(
                self.make_task(Path(tmp), compose)
            ).requirements

        self.assertIn(
            "services.main.volumes.bind",
            requirements["unsupported_features"],
        )

    def test_legacy_log_bind_mounts_remain_adapter_managed(self) -> None:
        compose = """
services:
  main:
    build: .
    volumes:
      - ${HOST_VERIFIER_LOGS_PATH}:${ENV_VERIFIER_LOGS_PATH}
      - ${HOST_AGENT_LOGS_PATH}:${ENV_AGENT_LOGS_PATH}
"""
        with tempfile.TemporaryDirectory() as tmp:
            requirements = resolve_bundle_spec(
                self.make_task(Path(tmp), compose)
            ).requirements

        self.assertFalse(requirements["shared_volumes"])
        self.assertNotIn(
            "services.main.volumes.bind",
            requirements["unsupported_features"],
        )

    def test_rejects_build_context_escape(self) -> None:
        compose = """
services:
  main:
    build: ../outside
"""
        with tempfile.TemporaryDirectory() as tmp:
            task = self.make_task(Path(tmp), compose)
            (task / "outside").mkdir()
            with self.assertRaisesRegex(ValueError, "escapes task environment"):
                resolve_bundle_spec(task)

    def test_allows_dockerfile_outside_context_but_inside_environment(self) -> None:
        compose = """
services:
  main:
    build:
      context: context
      dockerfile: ../Dockerfile
"""
        with tempfile.TemporaryDirectory() as tmp:
            task = self.make_task(Path(tmp), compose)
            (task / "environment" / "context").mkdir()
            spec = resolve_bundle_spec(task)

        self.assertEqual(spec.services["main"].build.dockerfile.name, "Dockerfile")

    def test_rejects_dependency_cycle(self) -> None:
        compose = """
services:
  main:
    build: .
    depends_on: [worker]
  worker:
    build:
      dockerfile: Dockerfile.worker
    depends_on: [main]
"""
        with tempfile.TemporaryDirectory() as tmp:
            task = self.make_task(Path(tmp), compose)
            (task / "environment" / "Dockerfile.worker").write_text(
                "FROM alpine:3.20\n", encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "contains a cycle"):
                resolve_bundle_spec(task)

    def test_rejects_unsupported_build_fields(self) -> None:
        compose = """
services:
  main:
    build:
      context: .
      secrets: [registry-token]
"""
        with tempfile.TemporaryDirectory() as tmp:
            task = self.make_task(Path(tmp), compose)
            with self.assertRaisesRegex(ValueError, "unsupported build fields"):
                resolve_bundle_spec(task)

    def test_rejects_unsupported_top_level_compose_fields(self) -> None:
        compose = """
include:
  - worker.yaml
services:
  main:
    build: .
"""
        with tempfile.TemporaryDirectory() as tmp:
            task = self.make_task(Path(tmp), compose)
            with self.assertRaisesRegex(
                ValueError, "unsupported top-level Compose fields: include"
            ):
                resolve_bundle_spec(task)

    def test_allows_top_level_metadata_and_extensions(self) -> None:
        compose = """
name: example
version: "3.9"
x-main: &main
  build: .
services:
  main:
    <<: *main
"""
        with tempfile.TemporaryDirectory() as tmp:
            spec = resolve_bundle_spec(self.make_task(Path(tmp), compose))

        self.assertEqual(list(spec.services), ["main"])

    def test_preserves_compose_working_directory(self) -> None:
        compose = """
services:
  main:
    build: .
    working_dir: /workspace
"""
        with tempfile.TemporaryDirectory() as tmp:
            task = self.make_task(Path(tmp), compose)
            spec = resolve_bundle_spec(task)

        self.assertEqual(spec.services["main"].working_dir, "/workspace")
        self.assertEqual(spec.services["main"].unsupported_fields, [])
        self.assertNotIn(
            "services.main.working_dir", spec.requirements["unsupported_features"]
        )

    def test_preserves_compose_override_presence_and_expose_metadata(self) -> None:
        compose = """
services:
  main:
    build: .
    entrypoint: null
    command: []
    expose: [8080/tcp, 9090]
"""
        with tempfile.TemporaryDirectory() as tmp:
            spec = resolve_bundle_spec(self.make_task(Path(tmp), compose))

        service = spec.services["main"]
        self.assertTrue(service.entrypoint_present)
        self.assertIsNone(service.entrypoint)
        self.assertTrue(service.command_present)
        self.assertEqual(service.command, [])
        self.assertEqual(service.expose, ["8080/tcp", 9090])


if __name__ == "__main__":
    unittest.main()

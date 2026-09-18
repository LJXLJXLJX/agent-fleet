from __future__ import annotations

import gzip
import io
import json
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import MagicMock, patch

from artifact_cache_gateway.harbor_tasks.analyzer import (
    analyze,
    apt_dependencies,
    apt_repository_declarations,
    external_build_inputs,
    external_build_input_issues,
    shell_commands,
    write_outputs,
)
from artifact_cache_gateway.harbor_tasks.preparer.direct_download import (
    classify_refresh_policy,
    compile_download_gateway_plan,
    merge_download_plan_directory,
)
from artifact_cache_gateway.gateway.services.image import (
    _is_missing_manifest,
    MirrorError,
    Platform,
    build_image_plan,
    mirror_images,
    parse_image_reference,
    parse_source_prefix_map,
    run_command,
)
from artifact_cache_gateway.core.local_env import parse_local_env
from artifact_cache_gateway.harbor_tasks.preparer.cli import main as prepare_main
from artifact_cache_gateway.harbor_tasks.preparer.models import (
    AptEnvironment,
    ProbeResult,
    ProbeSettings,
    WarmResult,
)
from artifact_cache_gateway.harbor_tasks.preparer.orchestrator import probe_packages, warm_packages
from artifact_cache_gateway.harbor_tasks.preparer.providers.apt import AptProvider
from artifact_cache_gateway.harbor_tasks.preparer.providers.npm import (
    NpmProvider,
    _tarball_relative_path,
)
from artifact_cache_gateway.harbor_tasks.preparer.providers.pip import PipProvider
from artifact_cache_gateway.gateway.services.apt import (
    compile_apt_gateway_plan,
    merge_gateway_config,
    repository_source_name,
)


SOURCE_DIGEST = "sha256:" + "a" * 64
TARGET_DIGEST = "sha256:" + "b" * 64


def make_dataset(root: Path) -> Path:
    dataset = root / "dataset"
    environment = dataset / "task-1" / "environment"
    environment.mkdir(parents=True)
    (dataset / "task-1" / "task.toml").write_text(
        "[environment]\nbuild_timeout_sec = 600\n", encoding="utf-8"
    )
    (environment / "Dockerfile").write_text(
        "FROM ubuntu:22.04\nRUN apt-get update && apt-get install -y curl\n",
        encoding="utf-8",
    )
    return dataset


def make_apt_dataset(root: Path) -> Path:
    dataset = root / "apt-dataset"
    environment = dataset / "task-apt" / "environment"
    environment.mkdir(parents=True)
    (dataset / "task-apt" / "task.toml").write_text(
        "[environment]\nbuild_timeout_sec = 600\n", encoding="utf-8"
    )
    (environment / "Dockerfile").write_text(
        "FROM ubuntu:22.04\n"
        "RUN curl -fsSL https://dl.google.com/linux/linux_signing_key.pub "
        "| gpg --dearmor -o /usr/share/keyrings/google.gpg "
        "&& echo \"deb [signed-by=/usr/share/keyrings/google.gpg] "
        "https://dl.google.com/linux/chrome/deb/ stable main\" "
        "> /etc/apt/sources.list.d/google.list "
        "&& apt-get update "
        "&& apt-get install -y google-chrome-stable curl\n"
        "RUN wget "
        "https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/"
        "x86_64/cuda-keyring_1.1-1_all.deb?token=must-not-leak "
        "&& dpkg -i cuda-keyring_1.1-1_all.deb\n"
        "RUN echo \"deb http://archive.ubuntu.com/ubuntu jammy main\" "
        "> /etc/apt/sources.list\n",
        encoding="utf-8",
    )
    (environment / "vendor.sources").write_text(
        "Types: deb\nURIs: https://packages.microsoft.com/repos/code\n"
        "Suites: stable\nComponents: main\n",
        encoding="utf-8",
    )
    return dataset


class ImageReferenceTest(unittest.TestCase):
    def test_normalizes_docker_hub_and_explicit_registry(self) -> None:
        self.assertEqual(
            parse_image_reference("ubuntu:22.04").canonical,
            "docker.io/library/ubuntu:22.04",
        )
        self.assertEqual(
            parse_image_reference("ocaml/opam:debian-12").canonical,
            "docker.io/ocaml/opam:debian-12",
        )
        self.assertEqual(
            parse_image_reference("mcr.microsoft.com/dotnet/sdk:8.0").canonical,
            "mcr.microsoft.com/dotnet/sdk:8.0",
        )

    def test_keeps_tag_and_digest_reference(self) -> None:
        value = f"ubuntu:22.04@{SOURCE_DIGEST}"
        self.assertEqual(
            parse_image_reference(value).canonical,
            f"docker.io/library/ubuntu:22.04@{SOURCE_DIGEST}",
        )

    def test_rejects_unresolved_from_variable(self) -> None:
        with self.assertRaises(MirrorError):
            parse_image_reference("$BASE_IMAGE")

    def test_old_skopeo_repository_missing_message_is_retryable(self) -> None:
        error = MirrorError(
            "unknown: repository public-mirror/library/ubuntu not found"
        )
        self.assertTrue(_is_missing_manifest(error))

    def test_current_harbor_artifact_missing_message_is_retryable(self) -> None:
        error = MirrorError(
            "unknown: artifact public-mirror/library/ubuntu:22.04 not found"
        )
        self.assertTrue(_is_missing_manifest(error))

    def test_all_current_reported_base_images_are_supported(self) -> None:
        images = [
            "ubuntu:22.04",
            "node:20-bookworm",
            "golang:1.23-bookworm",
            "python:3.10-bookworm",
            "rust:1.84-bookworm",
            "eclipse-temurin:21-jdk-jammy",
            "php:8.3-bookworm",
            "python:3.9-bookworm",
            "julia:1.10-bookworm",
            "elixir:1.16",
            "gcc:13-bookworm",
            "swift:5.10",
            "dart:stable",
            "mcr.microsoft.com/dotnet/sdk:8.0",
            "r-base:4.4.0",
            "ocaml/opam:debian-12-ocaml-5.1",
            "nickblah/lua:5.4-luarocks-debian",
        ]
        self.assertEqual(len({parse_image_reference(item).canonical for item in images}), 17)

    def test_maps_mcr_to_daocloud_without_changing_target_namespace(self) -> None:
        source = parse_image_reference("mcr.microsoft.com/dotnet/sdk:8.0")
        plan = build_image_plan(
            {"dataset": "/dataset", "images": [{"image": source.canonical}]},
            registry="registry.internal",
            project="public-mirror",
            platform=Platform.parse("linux/amd64"),
            source_prefix_map={
                "mcr.microsoft.com": "m.daocloud.io/mcr.microsoft.com"
            },
        )
        self.assertEqual(
            plan["images"][0]["mirror_source_ref"],
            "m.daocloud.io/mcr.microsoft.com/dotnet/sdk:8.0",
        )
        self.assertEqual(
            plan["images"][0]["target_tag_ref"],
            "registry.internal/public-mirror/mcr.microsoft.com/dotnet/sdk:8.0",
        )

    def test_preserves_ordered_docker_hub_mirror_candidates(self) -> None:
        source = parse_image_reference("nebius/some-image:tag")
        plan = build_image_plan(
            {"dataset": "/dataset", "images": [{"image": source.canonical}]},
            registry="registry.internal",
            project="public-mirror",
            platform=Platform.parse("linux/amd64"),
            source_prefix_map={
                "docker.io": [
                    "docker.m.daocloud.io",
                    "docker.1ms.run",
                    "docker.1panel.live",
                    "docker.xuanyuan.me",
                ]
            },
        )
        self.assertEqual(
            plan["images"][0]["mirror_source_refs"],
            [
                "docker.m.daocloud.io/nebius/some-image:tag",
                "docker.1ms.run/nebius/some-image:tag",
                "docker.1panel.live/nebius/some-image:tag",
                "docker.xuanyuan.me/nebius/some-image:tag",
            ],
        )


class DatasetPreparationTest(unittest.TestCase):
    def test_download_plan_supports_reviewed_root_object(self) -> None:
        report = {
            "kind": "artifact-cache-external-build-input-report",
            "dataset": "/dataset",
            "cache_candidates": [
                {
                    "kind": "http-download",
                    "action": "cache",
                    "url": "https://get.example.test/",
                    "query_present": False,
                    "credentials_present": False,
                }
            ],
        }

        plan = compile_download_gateway_plan(report)

        self.assertEqual(plan["schema_version"], 2)
        self.assertEqual(plan["summary"]["rewrites"], 1)
        self.assertEqual(plan["summary"]["rejected"], 0)
        self.assertEqual(plan["sources"][0]["kind"], "frozen-download")
        self.assertNotIn("allowed_exact_paths", plan["sources"][0])
        self.assertEqual(plan["rewrites"][0]["relative_path"], "root")
        self.assertTrue(plan["rewrites"][0]["gateway_path"].endswith("/root"))

    def test_download_plan_directory_merges_approved_origins(self) -> None:
        base = {
            "sources": [
                {
                    "name": "base",
                    "kind": "generic",
                    "ecosystem": "generic",
                    "base_url": "https://base.example/",
                    "allowed_redirect_origins": [],
                    "allow_query": False,
                }
            ]
        }
        reports = [
            {
                "kind": "artifact-cache-external-build-input-report",
                "dataset": f"/dataset-{index}",
                "cache_candidates": [
                    {
                        "kind": "http-download",
                        "action": "cache",
                        "url": f"https://downloads.example/tool-{index}.tar.gz",
                        "query_present": False,
                        "credentials_present": False,
                    }
                ],
            }
            for index in (1, 2)
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, report in enumerate(reports, start=1):
                (root / f"dataset-{index}.json").write_text(
                    json.dumps(compile_download_gateway_plan(report)), encoding="utf-8"
                )

            merged, loaded = merge_download_plan_directory(base, root)

        self.assertEqual([path.name for path in loaded], ["dataset-1.json", "dataset-2.json"])
        download = next(
            source
            for source in merged["sources"]
            if source.get("ecosystem") == "download"
        )
        self.assertEqual(download["kind"], "frozen-download")
        self.assertNotIn("allowed_exact_paths", download)

    def test_download_plan_uses_reviewed_protocol_redirect_origins(self) -> None:
        report = {
            "kind": "artifact-cache-external-build-input-report",
            "dataset": "/dataset",
            "cache_candidates": [
                {
                    "kind": "http-download",
                    "action": "cache",
                    "url": "https://crates.io/api/v1/crates/regex/1.0.0/download",
                    "query_present": False,
                    "credentials_present": False,
                },
                {
                    "kind": "http-download",
                    "action": "cache",
                    "url": "https://pypi.python.org/packages/source/x/x/x-1.0.tar.gz",
                    "query_present": False,
                    "credentials_present": False,
                },
                {
                    "kind": "http-download",
                    "action": "cache",
                    "url": "https://sourceforge.net/projects/example/files/tool-1.0.tar.gz/download",
                    "query_present": False,
                    "credentials_present": False,
                },
            ],
        }

        plan = compile_download_gateway_plan(report)
        by_origin = {source["base_url"]: source for source in plan["sources"]}

        self.assertEqual(
            by_origin["https://crates.io/"]["allowed_redirect_origins"],
            ["https://static.crates.io"],
        )
        self.assertEqual(
            by_origin["https://pypi.python.org/"]["allowed_redirect_origins"],
            ["https://pypi.org", "https://files.pythonhosted.org"],
        )
        self.assertEqual(
            by_origin["https://sourceforge.net/"]["allowed_redirect_origins"],
            [
                "https://downloads.sourceforge.net",
                "https://onboardcloud.dl.sourceforge.net",
                "https://twds.dl.sourceforge.net",
            ],
        )

    def test_download_refresh_policy_is_conservative(self) -> None:
        self.assertEqual(
            classify_refresh_policy(
                "https://github.com/example/tool/releases/download/v1.2.3/tool.tar.gz"
            ),
            "immutable",
        )
        self.assertEqual(
            classify_refresh_policy(
                "https://raw.githubusercontent.com/example/tool/main/install.sh"
            ),
            "manual",
        )
        self.assertEqual(
            classify_refresh_policy("https://example.test/download/tool.tar.gz"),
            "manual",
        )

    def test_download_plan_uses_frozen_origin_and_fixed_rustup_route(self) -> None:
        report = {
            "schema_version": 1,
            "kind": "artifact-cache-external-build-input-report",
            "dataset": "/dataset",
            "cache_candidates": [
                {
                    "kind": "http-download",
                    "action": "cache",
                    "url": "https://github.com/example/tool/releases/download/v1.2.3/tool.tar.gz",
                    "query_present": False,
                },
                {
                    "kind": "http-download",
                    "action": "cache",
                    "url": "https://sh.rustup.rs/",
                    "query_present": False,
                },
                {
                    "kind": "git-repository",
                    "action": "mirror",
                    "url": "https://github.com/example/tool.git",
                    "query_present": False,
                },
            ],
        }

        plan = compile_download_gateway_plan(report)

        self.assertEqual(plan["summary"]["rewrites"], 2)
        self.assertEqual(plan["summary"]["immutable"], 1)
        self.assertEqual(plan["summary"]["manual"], 1)
        source = plan["sources"][0]
        self.assertEqual(source["ecosystem"], "download")
        self.assertEqual(source["kind"], "frozen-download")
        self.assertRegex(source["name"], r"^frozen-download-github-com-[0-9a-f]{16}$")
        self.assertRegex(source["config_updated_at"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(source["config_update_policy"], "manual")
        self.assertNotIn("allowed_exact_paths", source)
        rustup = next(
            row for row in plan["rewrites"] if row["upstream_url"] == "https://sh.rustup.rs/"
        )
        self.assertEqual(rustup["gateway_path"], "/v1/cache/rustup-init/rustup-init.sh")

    def test_download_plan_rejects_query_credentials_and_private_origins(self) -> None:
        inputs = []
        for command in (
            ["curl", "https://example.test/tool.tar.gz?token=secret"],
            ["wget", "https://user:password@example.test/private.tar.gz"],
            ["curl", "http://127.0.0.1/tool.tar.gz"],
        ):
            inputs.extend(external_build_inputs(command))

        report = {
            "kind": "artifact-cache-external-build-input-report",
            "dataset": "/dataset",
            "cache_candidates": [
                {
                    "kind": item.kind,
                    "action": item.action,
                    "url": item.url,
                    "query_present": item.query_present,
                    "credentials_present": item.credentials_present,
                }
                for item in inputs
            ]
            + [
                {
                    "kind": "http-download",
                    "action": "cache",
                    "url": "https://example.test/forced-private.tar.gz",
                    "query_present": False,
                    "credentials_present": True,
                }
            ],
        }
        plan = compile_download_gateway_plan(report)

        self.assertTrue(all(item.action == "review" for item in inputs))
        self.assertEqual(plan["summary"]["rewrites"], 0)
        self.assertEqual(plan["summary"]["rejected"], 1)
        serialized = json.dumps(
            [item.__dict__ for item in inputs], ensure_ascii=False
        )
        self.assertNotIn("secret", serialized)
        self.assertNotIn("password", serialized)

    def test_external_build_input_parser_distinguishes_downloads_and_git(self) -> None:
        download = list(
            external_build_inputs(
                [
                    "curl",
                    "-LsSf",
                    "https://releases.example.test/tool/v1.2.3/tool.tar.gz?token=secret",
                ]
            )
        )
        clone = list(
            external_build_inputs(
                [
                    "git",
                    "clone",
                    "--depth",
                    "1",
                    "--branch=v2.0.0",
                    "https://github.com/example/tool.git",
                    "/src/tool",
                ]
            )
        )
        ssh_clone = list(
            external_build_inputs(
                ["git", "clone", "git@github.com:example/private.git"]
            )
        )
        curl_targets = list(
            external_build_inputs(
                [
                    "curl",
                    "--proxy",
                    "http://proxy.example.test:7890",
                    "--referer=https://referer.example.test/",
                    "--url",
                    "https://downloads.example.test/tool.tar.gz",
                ]
            )
        )
        wget_targets = list(
            external_build_inputs(
                [
                    "wget",
                    "--referer",
                    "https://referer.example.test/",
                    "https://downloads.example.test/tool.zip",
                ]
            )
        )
        local_clone = ["git", "clone", "/home/user/repo.git", "/src/repo"]
        dynamic_clone = ["git", "clone", "$REPOSITORY_URL", "/src/repo"]

        self.assertEqual(len(download), 1)
        self.assertEqual(
            download[0].url,
            "https://releases.example.test/tool/v1.2.3/tool.tar.gz",
        )
        self.assertTrue(download[0].query_present)
        self.assertFalse(download[0].credentials_present)
        self.assertEqual(download[0].action, "review")
        self.assertNotIn("secret", download[0].url)
        self.assertEqual(clone[0].kind, "git-repository")
        self.assertEqual(clone[0].action, "mirror")
        self.assertEqual(clone[0].reference, "v2.0.0")
        self.assertEqual(ssh_clone[0].url, "ssh://github.com/example/private.git")
        self.assertEqual(ssh_clone[0].action, "review")
        self.assertEqual(
            [item.url for item in curl_targets],
            ["https://downloads.example.test/tool.tar.gz"],
        )
        self.assertEqual(
            [item.url for item in wget_targets],
            ["https://downloads.example.test/tool.zip"],
        )
        self.assertEqual(
            list(external_build_input_issues(local_clone, [])), []
        )
        self.assertEqual(
            len(list(external_build_input_issues(dynamic_clone, []))), 1
        )

    def test_dynamic_download_urls_require_review(self) -> None:
        inputs = list(
            external_build_inputs(
                [
                    "curl",
                    "-fsSLO",
                    "https://dl.k8s.io/release/$(curl -s "
                    "https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl",
                ]
            )
        )
        variable_inputs = list(
            external_build_inputs(
                [
                    "wget",
                    "https://golang.org/dl/go${GOLANG_VERSION}.linux-amd64.tar.gz",
                ]
            )
        )

        self.assertEqual(len(inputs), 1)
        self.assertEqual(inputs[0].action, "review")
        self.assertIsNone(inputs[0].cache_mode)
        self.assertIn("shell expansion", inputs[0].reason)
        self.assertEqual(variable_inputs[0].action, "review")
        parsed_inputs = [
            item
            for tokens in shell_commands(
                "curl -fsSLO https://dl.k8s.io/release/$(curl -s "
                "https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
            )
            for item in external_build_inputs(tokens)
        ]
        self.assertTrue(parsed_inputs)
        self.assertTrue(all(item.action == "review" for item in parsed_inputs))
        self.assertTrue(all(item.cache_mode is None for item in parsed_inputs))
        report = {
            "kind": "artifact-cache-external-build-input-report",
            "dataset": "/dataset",
            "dependencies": [
                item.__dict__
                for item in [*inputs, *variable_inputs, *parsed_inputs]
            ],
            "cache_candidates": [
                item.__dict__
                for item in parsed_inputs
                if item.action == "cache"
            ],
            "needs_review": [
                item.__dict__
                for item in [*inputs, *variable_inputs, *parsed_inputs]
            ],
            "unresolved": [],
        }
        plan = compile_download_gateway_plan(report)
        self.assertEqual(plan["summary"]["rewrites"], 0)
        self.assertEqual(plan["summary"]["rejected"], 0)

    def test_analyzer_scans_run_heredoc_and_subshell_install_commands(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            environment = dataset / "task" / "environment"
            environment.mkdir(parents=True)
            (dataset / "task" / "task.toml").write_text("", encoding="utf-8")
            (environment / "Dockerfile").write_text(
                "FROM ubuntu:22.04\n"
                "RUN <<-DOCKER_RUN_EOF\n"
                "    set -eux;\n"
                "    git clone https://github.com/example/project /src/project;\n"
                "    (apt-get install -y curl) || true;\n"
                "    (python3 -m pip install 'pytest==8.3.0' '.[test]') || true;\n"
                "    (export PATH=\"$(go env GOPATH)/bin:$PATH\" && "
                "go install github.com/example/tool@v1.2.3) || true;\n"
                "DOCKER_RUN_EOF\n",
                encoding="utf-8",
            )

            analysis = analyze(dataset, "referenced")
            output = root / "output"
            write_outputs(output, analysis)
            download_plan_exists = (output / "download-gateway-plan.json").is_file()
            git_plan_exists = (output / "git-mirror-plan.json").is_file()

        packages = {
            (row["manager"], row["package"]): row
            for row in analysis["packages"]
        }
        self.assertEqual(
            set(packages),
            {
                ("apt", "curl"),
                ("pip", "pytest"),
                ("go", "github.com/example/tool"),
            },
        )
        self.assertEqual(
            analysis["summary"]["install_invocations_by_manager"],
            {"apt": 1, "pip": 1, "go": 1},
        )
        self.assertEqual(analysis["summary"]["external_git_mirror_candidate_urls"], 1)
        self.assertEqual(
            analysis["external_inputs"]["dependencies"][0]["url"],
            "https://github.com/example/project",
        )
        self.assertTrue(download_plan_exists)
        self.assertTrue(git_plan_exists)

    def test_analyzer_scans_run_heredoc_with_explicit_shell_interpreter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            environment = dataset / "task" / "environment"
            environment.mkdir(parents=True)
            (dataset / "task" / "task.toml").write_text("", encoding="utf-8")
            (environment / "Dockerfile").write_text(
                "FROM python:3.10-bookworm\n"
                "RUN <<'BUILD_SCRIPT' /bin/bash -e\n"
                "python -m pip install requests==2.32.0\n"
                "BUILD_SCRIPT\n",
                encoding="utf-8",
            )

            analysis = analyze(dataset, "referenced")

        self.assertEqual(
            [
                (row["manager"], row["package"], row["occurrences"])
                for row in analysis["packages"]
            ],
            [("pip", "requests", 1)],
        )

    def test_quoted_heredoc_marker_does_not_consume_later_dockerfile_runs(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            environment = dataset / "task" / "environment"
            environment.mkdir(parents=True)
            (dataset / "task" / "task.toml").write_text("", encoding="utf-8")
            (environment / "Dockerfile").write_text(
                "FROM ubuntu:22.04\n"
                "RUN echo '<<EOF'\n"
                "RUN apt-get install -y curl\n",
                encoding="utf-8",
            )

            analysis = analyze(dataset, "referenced")

        self.assertEqual(
            [
                (row["manager"], row["package"], row["occurrences"])
                for row in analysis["packages"]
            ],
            [("apt", "curl", 1)],
        )

    def test_analyzer_writes_external_build_input_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            environment = dataset / "task" / "environment"
            environment.mkdir(parents=True)
            (dataset / "task" / "task.toml").write_text(
                "[environment]\nbuild_timeout_sec = 600\n", encoding="utf-8"
            )
            (environment / "Dockerfile").write_text(
                "FROM ubuntu:22.04\n"
                "RUN curl -LsSf https://astral.sh/uv/install.sh | sh\n"
                "RUN wget -q https://astral.sh/uv/install.sh -O /tmp/uv-install.sh\n"
                "RUN wget -q https://example.test/tool-v1.0.0.tar.gz -O /tmp/tool.tar.gz\n"
                "RUN git clone --branch v3.1.4 https://github.com/example/tool.git /src/tool "
                "&& cd /src/tool && git reset --hard 0123456789abcdef0123456789abcdef01234567\n"
                "RUN curl -fsSL \"$TOOL_URL\" -o /tmp/tool\n",
                encoding="utf-8",
            )
            analysis = analyze(dataset, "referenced")
            output = root / "output"
            write_outputs(output, analysis)
            report = (output / "external-build-inputs.json").read_text(
                encoding="utf-8"
            )
            csv_exists = (output / "external-build-inputs.csv").is_file()
            download_plan = json.loads(
                (output / "download-gateway-plan.json").read_text(encoding="utf-8")
            )
            git_plan = json.loads(
                (output / "git-mirror-plan.json").read_text(encoding="utf-8")
            )

        rows = analysis["external_inputs"]["dependencies"]
        self.assertEqual(analysis["summary"]["external_build_input_urls"], 3)
        self.assertEqual(analysis["summary"]["external_cache_candidate_urls"], 3)
        self.assertEqual(
            analysis["summary"]["external_download_cache_candidate_urls"], 2
        )
        self.assertEqual(analysis["summary"]["external_git_mirror_candidate_urls"], 1)
        self.assertEqual(len(rows), 4)
        self.assertEqual(analysis["summary"]["external_unresolved_commands"], 1)
        self.assertEqual(
            {row["kind"] for row in rows}, {"http-download", "git-repository"}
        )
        git_row = next(row for row in rows if row["kind"] == "git-repository")
        self.assertEqual(git_row["reference"], "v3.1.4")
        self.assertEqual(
            git_row["references"],
            ["0123456789abcdef0123456789abcdef01234567"],
        )
        self.assertEqual(git_row["cache_mode"], "git-smart-http")
        self.assertEqual(
            git_plan["repositories"][0]["references"],
            ["0123456789abcdef0123456789abcdef01234567", "v3.1.4"],
        )
        self.assertIn("https://astral.sh/uv/install.sh", report)
        self.assertNotIn("$TOOL_URL", report)
        self.assertTrue(csv_exists)
        uv_rewrite = next(
            row
            for row in download_plan["rewrites"]
            if row["upstream_url"] == "https://astral.sh/uv/install.sh"
        )
        self.assertEqual(uv_rewrite["refresh_policy"], "manual")
        self.assertEqual(
            uv_rewrite["gateway_path"],
            f"/v1/cache/download/v1/https/{'astral.sh'.encode().hex()}/object/"
            f"{'/uv/install.sh'.encode().hex()}",
        )

    def test_analyzer_preserves_pip_requirement_image_contexts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            environment = dataset / "task" / "environment"
            environment.mkdir(parents=True)
            (dataset / "task" / "task.toml").write_text(
                "[environment]\nbuild_timeout_sec = 600\n", encoding="utf-8"
            )
            (environment / "Dockerfile").write_text(
                "FROM ubuntu:22.04\n"
                "RUN python3 -m pip install 'numpy<2' numpy==1.23.5\n"
                "RUN python3.9 -m pip install numpy==1.23.5\n",
                encoding="utf-8",
            )
            analysis = analyze(dataset, "referenced")

        numpy = next(
            row
            for row in analysis["packages"]
            if row["manager"] == "pip" and row["package"] == "numpy"
        )
        self.assertEqual(
            [
                {
                    key: value
                    for key, value in context.items()
                    if key not in {"environment_id", "build_contexts"}
                }
                for context in numpy["contexts"]
            ],
            [
                {"requirement": "numpy<2", "images": ["ubuntu:22.04"]},
                {
                    "requirement": "numpy==1.23.5",
                    "images": ["ubuntu:22.04"],
                },
                {
                    "requirement": "numpy==1.23.5",
                    "images": ["ubuntu:22.04"],
                    "python_version": "3.9",
                },
            ],
        )
        self.assertEqual(analysis["schema_version"], 2)
        self.assertEqual(len(analysis["build_contexts"]), 1)
        self.assertTrue(
            all(context["environment_id"] for context in numpy["contexts"])
        )
        self.assertTrue(
            all(len(context["build_contexts"]) == 1 for context in numpy["contexts"])
        )

    def test_repository_context_follows_dockerfile_command_order(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            environment = dataset / "task" / "environment"
            environment.mkdir(parents=True)
            (dataset / "task" / "task.toml").write_text(
                "[environment]\nbuild_timeout_sec = 600\n", encoding="utf-8"
            )
            (environment / "Dockerfile").write_text(
                "FROM ubuntu:22.04\n"
                "RUN apt-get install -y curl && "
                "echo 'deb https://repo.example/apt stable main' "
                "> /etc/apt/sources.list.d/vendor.list\n"
                "RUN apt-get update && apt-get install -y vendor-package\n",
                encoding="utf-8",
            )
            analysis = analyze(dataset, "referenced")

        packages = {
            str(row["package"]): row for row in analysis["packages"]
        }
        self.assertEqual(packages["curl"]["repository_contexts"], [])
        self.assertEqual(
            [
                {
                    key: value
                    for key, value in context.items()
                    if key not in {"environment_id", "build_contexts"}
                }
                for context in packages["vendor-package"]["repository_contexts"]
            ],
            [
                {
                    "upstream_url": "https://repo.example/apt",
                    "suite": "stable",
                    "components": ["main"],
                    "task": "task",
                    "source": "environment/Dockerfile",
                    "images": ["ubuntu:22.04"],
                }
            ],
        )

    def test_analyzer_separates_multistage_base_image_environments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            environment = dataset / "task" / "environment"
            environment.mkdir(parents=True)
            (dataset / "task" / "task.toml").write_text(
                "[environment]\nbuild_timeout_sec = 600\n", encoding="utf-8"
            )
            (environment / "Dockerfile").write_text(
                "FROM python:3.10-bookworm AS py310\n"
                "RUN pip install torch\n"
                "FROM python:3.11-bookworm AS py311\n"
                "RUN pip install torch\n",
                encoding="utf-8",
            )
            analysis = analyze(dataset, "referenced")

        torch = next(
            row
            for row in analysis["packages"]
            if row["manager"] == "pip" and row["package"] == "torch"
        )
        self.assertEqual(
            {tuple(context["images"]) for context in torch["contexts"]},
            {("python:3.10-bookworm",), ("python:3.11-bookworm",)},
        )
        self.assertEqual(
            len({context["environment_id"] for context in torch["contexts"]}), 2
        )
        self.assertEqual(len(analysis["build_contexts"]), 2)
        self.assertEqual(len(analysis["environments"]), 2)

    def test_npm_resolution_environment_does_not_split_on_base_image(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            dataset = Path(temporary) / "dataset"
            for task, image in (
                ("ubuntu-task", "ubuntu:22.04"),
                ("node-task", "node:24-bookworm-slim"),
            ):
                environment = dataset / task / "environment"
                environment.mkdir(parents=True)
                (dataset / task / "task.toml").write_text(
                    "[environment]\nbuild_timeout_sec = 600\n",
                    encoding="utf-8",
                )
                (environment / "Dockerfile").write_text(
                    f"FROM {image}\nRUN npm install -g pnpm@10.33.1\n",
                    encoding="utf-8",
                )
            analysis = analyze(dataset, "referenced")

        npm_environments = [
            row for row in analysis["environments"] if row["manager"] == "npm"
        ]
        self.assertEqual(len(npm_environments), 1)
        self.assertIsNone(npm_environments[0]["base_image"])
        self.assertEqual(
            set(npm_environments[0]["base_images"]),
            {"ubuntu:22.04", "node:24-bookworm-slim"},
        )
        pnpm = next(
            row
            for row in analysis["packages"]
            if row["manager"] == "npm" and row["package"] == "pnpm"
        )
        self.assertEqual(
            len({context["environment_id"] for context in pnpm["contexts"]}),
            1,
        )

    def test_npm_probe_deduplicates_requirement_across_base_images(self) -> None:
        integrity = "sha512-" + "YQ=="

        def resolver(_command, _timeout, _name):  # noqa: ANN001
            return "ACG\t" + json.dumps(
                {
                    "package": "pnpm",
                    "requirement": "pnpm@10.33.1",
                    "state": "resolved",
                    "version": "10.33.1",
                    "filename": "pnpm-10.33.1.tgz",
                    "url": "https://registry.npmmirror.com/pnpm/-/pnpm-10.33.1.tgz",
                    "integrity": integrity,
                }
            ) + "\n"

        provider = NpmProvider(command_runner=resolver)
        row = {
            "manager": "npm",
            "package": "pnpm",
            "contexts": [
                {
                    "requirement": "pnpm@10.33.1",
                    "images": ["ubuntu:22.04"],
                    "environment_id": "env-ubuntu",
                    "build_contexts": ["ctx-ubuntu"],
                },
                {
                    "requirement": "pnpm@10.33.1",
                    "images": ["node:24-bookworm-slim"],
                    "environment_id": "env-node",
                    "build_contexts": ["ctx-node"],
                },
            ],
        }
        sampled = ProbeResult(
            "npm",
            "pnpm",
            "npm:registry-v1",
            "fast",
            "controlled",
            requirement="pnpm@10.33.1",
            version="10.33.1",
            integrity=integrity,
        )
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            provider, "_sample", return_value=sampled
        ) as sample:
            results = provider.probe(
                [row], ProbeSettings(), work_dir=Path(temporary)
            )

        sample.assert_called_once()
        self.assertEqual(len(results), 1)
        self.assertEqual(
            results[0].consumer_environment_ids,
            ("env-node", "env-ubuntu"),
        )
        self.assertEqual(results[0].build_contexts, ("ctx-node", "ctx-ubuntu"))

    def test_npm_probe_rejects_unknown_explicit_registry(self) -> None:
        provider = NpmProvider(
            command_runner=lambda *_args: self.fail("resolver must not run")
        )
        with tempfile.TemporaryDirectory() as temporary:
            results = provider.probe(
                [
                    {
                        "manager": "npm",
                        "package": "private-package",
                        "contexts": [
                            {
                                "requirement": "private-package@1",
                                "resolution_options": {
                                    "registry": "https://packages.example.test/"
                                },
                                "environment_id": "env-private",
                                "build_contexts": ["ctx-private"],
                            }
                        ],
                    }
                ],
                ProbeSettings(),
                work_dir=Path(temporary),
            )

        self.assertEqual([row.status for row in results], ["unsupported"])
        self.assertIn("private or unknown", results[0].reason)

    def test_npm_warm_pins_probed_version_and_preserves_consumers(self) -> None:
        provider = NpmProvider()
        record = {
            "package": "pnpm",
            "requirement": "pnpm@10.33.1",
            "state": "resolved",
            "version": "10.33.1",
            "url": "https://registry.npmjs.org/pnpm/-/pnpm-10.33.1.tgz",
            "integrity": "sha512-YQ==",
        }
        warmed = WarmResult(
            "npm",
            "pnpm",
            "npm:registry-v1",
            "cached",
            "controlled",
        )
        row = {
            "manager": "npm",
            "package": "pnpm",
            "requirement": "pnpm@10",
            "version": "10.33.1",
            "environment_id": "env-npm",
            "consumer_environment_ids": ["env-a", "env-b"],
            "build_contexts": ["ctx-a", "ctx-b"],
        }
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            provider, "_resolve", return_value={("pnpm", "pnpm@10.33.1"): record}
        ) as resolve, patch.object(
            provider, "_fetch_to_cache", return_value=warmed
        ):
            results = provider.warm(
                [row],
                gateway_url="http://127.0.0.1:8080/v1/cache",
                timeout_seconds=10,
                work_dir=Path(temporary),
            )

        self.assertEqual(resolve.call_args.args[0], [("pnpm", "pnpm@10.33.1")])
        self.assertEqual(results[0].requirement, "pnpm@10")
        self.assertEqual(results[0].consumer_environment_ids, ("env-a", "env-b"))
        self.assertEqual(results[0].build_contexts, ("ctx-a", "ctx-b"))

    def test_npm_warm_accepts_only_its_gateway_tarball_route(self) -> None:
        gateway = "http://127.0.0.1:8080/v1/cache"
        self.assertEqual(
            _tarball_relative_path(
                "@scope/package",
                gateway
                + "/npm-registry/@scope/package/-/package-1.2.3.tgz",
                gateway_url=gateway,
            ),
            "@scope/package/-/package-1.2.3.tgz",
        )
        self.assertIsNone(
            _tarball_relative_path(
                "@scope/package",
                gateway + "/pypi-files/@scope/package/-/package-1.2.3.tgz",
                gateway_url=gateway,
            )
        )

    def test_referenced_script_inherits_only_its_referencing_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            environment = dataset / "task" / "environment"
            environment.mkdir(parents=True)
            (dataset / "task" / "task.toml").write_text("", encoding="utf-8")
            (environment / "install.sh").write_text(
                "apt-get install -y jq\n", encoding="utf-8"
            )
            (environment / "Dockerfile").write_text(
                "FROM ubuntu:22.04 AS build\n"
                "RUN echo build\n"
                "FROM debian:bookworm AS runtime\n"
                "COPY install.sh /install.sh\n"
                "RUN /install.sh\n",
                encoding="utf-8",
            )
            analysis = analyze(dataset, "referenced")

        jq = next(
            row
            for row in analysis["packages"]
            if row["manager"] == "apt" and row["package"] == "jq"
        )
        self.assertEqual(jq["images"], ["debian:bookworm"])
        self.assertEqual(jq["contexts"][0]["images"], ["debian:bookworm"])
        context_id = jq["contexts"][0]["build_contexts"][0]
        context = next(
            row for row in analysis["build_contexts"] if row["id"] == context_id
        )
        self.assertEqual(context["stage_name"], "runtime")

    def test_same_apt_package_is_separate_for_ubuntu_and_debian(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            environment = dataset / "task" / "environment"
            environment.mkdir(parents=True)
            (dataset / "task" / "task.toml").write_text("", encoding="utf-8")
            (environment / "Dockerfile").write_text(
                "FROM ubuntu:22.04\nRUN apt-get install -y curl\n"
                "FROM debian:bookworm\nRUN apt-get install -y curl\n",
                encoding="utf-8",
            )
            analysis = analyze(dataset, "referenced")

        curl = next(
            row
            for row in analysis["packages"]
            if row["manager"] == "apt" and row["package"] == "curl"
        )
        self.assertEqual(
            {tuple(context["images"]) for context in curl["contexts"]},
            {("ubuntu:22.04",), ("debian:bookworm",)},
        )
        self.assertEqual(
            len({context["environment_id"] for context in curl["contexts"]}), 2
        )

    def test_pip_resolution_options_are_sanitized_and_part_of_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = root / "dataset"
            environment = dataset / "task" / "environment"
            environment.mkdir(parents=True)
            (dataset / "task" / "task.toml").write_text("", encoding="utf-8")
            (environment / "Dockerfile").write_text(
                "FROM python:3.10-bookworm\n"
                "RUN pip install --index-url "
                "https://fixture-user@example.test/simple numpy 2>/dev/null\n"
                "RUN pip install --index-url https://mirror.example.test/simple numpy\n",
                encoding="utf-8",
            )
            analysis = analyze(dataset, "referenced")

        numpy = next(
            row
            for row in analysis["packages"]
            if row["manager"] == "pip" and row["package"] == "numpy"
        )
        self.assertEqual(len(numpy["contexts"]), 2)
        self.assertEqual(
            {
                context["resolution_options"]["index_url"]
                for context in numpy["contexts"]
            },
            {
                "https://example.test/simple",
                "https://mirror.example.test/simple",
            },
        )
        self.assertEqual(
            len({context["environment_id"] for context in numpy["contexts"]}), 2
        )
        self.assertNotIn("fixture-user", json.dumps(analysis))
        self.assertFalse(
            any(
                row["manager"] == "pip" and row["package"] == "2"
                for row in analysis["packages"]
            )
        )

    def test_ppa_repository_context_keeps_runtime_codename(self) -> None:
        self.assertEqual(
            apt_repository_declarations(
                "add-apt-repository -y ppa:deadsnakes/ppa"
            ),
            [
                {
                    "upstream_url": (
                        "https://ppa.launchpadcontent.net/deadsnakes/ppa/ubuntu"
                    ),
                    "suite": "$CODENAME",
                    "components": ["main"],
                }
            ],
        )

    def test_repository_context_normalizes_runtime_codename_expressions(self) -> None:
        for expression in (
            "$(lsb_release -cs)-pgdg",
            "$(lsb_release -sc)-pgdg",
            "$VERSION_CODENAME-pgdg",
            "${VERSION_CODENAME}-pgdg",
        ):
            with self.subTest(expression=expression):
                self.assertEqual(
                    apt_repository_declarations(
                        "echo 'deb [signed-by=/usr/share/keyrings/pgdg.asc] "
                        f"https://apt.postgresql.org/pub/repos/apt {expression} main'"
                    ),
                    [
                        {
                            "upstream_url": "https://apt.postgresql.org/pub/repos/apt",
                            "suite": "$CODENAME-pgdg",
                            "components": ["main"],
                        }
                    ],
                )

    def test_package_probe_checks_every_apt_package_and_reports_unsupported_managers(self) -> None:
        analysis = {
            "dataset": "/dataset",
            "packages": [
                {"manager": "apt", "package": "curl", "images": ["ubuntu:22.04"]},
                {"manager": "apt", "package": "git", "images": ["ubuntu:22.04"]},
                {"manager": "pip", "package": "pytest", "images": ["ubuntu:22.04"]},
            ],
        }

        def resolver(_command, _timeout, _name):  # noqa: ANN001
            return (
                "ACG\tcurl\tresolved\t1.0\tpool/c/curl.deb\t\t10\thttps://mirror.example/ubuntu\n"
                "ACG\tgit\tresolved\t2.0\tpool/g/git.deb\t\t20\thttps://mirror.example/ubuntu\n"
            )

        provider = AptProvider(command_runner=resolver)

        def sample(package, environment, record, settings):  # noqa: ANN001
            del record, settings
            return ProbeResult(
                "apt",
                package,
                environment.identity,
                "slow" if package == "git" else "fast",
                "controlled",
            )

        with tempfile.TemporaryDirectory() as temporary, patch.object(
            provider, "_sample", side_effect=sample
        ):
            report = probe_packages(
                analysis,
                settings=ProbeSettings(),
                work_dir=Path(temporary),
                providers=[provider],
            )
        self.assertEqual(report["summary"]["results"], 3)
        self.assertEqual(report["summary"]["problem_results"], 1)
        self.assertEqual(report["summary"]["unsupported_results"], 1)
        self.assertEqual(report["summary"]["probed_results"], 2)
        self.assertAlmostEqual(report["summary"]["probe_coverage_percent"], 66.67)
        self.assertEqual(
            {row["package"] for row in report["results"]},
            {"curl", "git", "pytest"},
        )

    def test_apt_probe_does_not_merge_analysis_environments(self) -> None:
        analysis = {
            "dataset": "/dataset",
            "packages": [
                {
                    "manager": "apt",
                    "package": "vendor-package",
                    "images": ["ubuntu:22.04"],
                    "contexts": [
                        {
                            "requirement": "vendor-package",
                            "images": ["ubuntu:22.04"],
                            "environment_id": "env-repo-a",
                            "build_contexts": ["ctx-a"],
                        },
                        {
                            "requirement": "vendor-package",
                            "images": ["ubuntu:22.04"],
                            "environment_id": "env-repo-b",
                            "build_contexts": ["ctx-b"],
                        },
                    ],
                    "repository_contexts": [
                        {
                            "upstream_url": "https://repo-a.example/apt",
                            "suite": "stable",
                            "components": ["main"],
                            "images": ["ubuntu:22.04"],
                            "environment_id": "env-repo-a",
                        },
                        {
                            "upstream_url": "https://repo-b.example/apt",
                            "suite": "stable",
                            "components": ["main"],
                            "images": ["ubuntu:22.04"],
                            "environment_id": "env-repo-b",
                        },
                    ],
                }
            ],
        }
        calls = []

        def resolver(_command, _timeout, _name):  # noqa: ANN001
            calls.append(_name)
            return (
                "ACG\tvendor-package\tresolved\t1.0\tpool/v/vendor.deb\t\t10\t"
                "https://mirror.example/ubuntu\n"
            )

        provider = AptProvider(command_runner=resolver)

        def sample(package, environment, record, settings):  # noqa: ANN001
            del record, settings
            return ProbeResult(
                "apt", package, environment.identity, "fast", "controlled"
            )

        with tempfile.TemporaryDirectory() as temporary, patch.object(
            provider, "_sample", side_effect=sample
        ):
            report = probe_packages(
                analysis,
                settings=ProbeSettings(),
                work_dir=Path(temporary),
                providers=[provider],
            )

        self.assertEqual(len(calls), 2)
        self.assertEqual(
            {
                (
                    row["environment_id"],
                    tuple(row["build_contexts"]),
                    row["repository_contexts"][0]["upstream_url"],
                )
                for row in report["results"]
            },
            {
                ("env-repo-a", ("ctx-a",), "https://repo-a.example/apt"),
                ("env-repo-b", ("ctx-b",), "https://repo-b.example/apt"),
            },
        )

    def test_probe_records_resolver_timeouts_as_unavailable(self) -> None:
        def timeout_resolver(_command, timeout, _name):  # noqa: ANN001
            raise subprocess.TimeoutExpired("docker", timeout)

        analysis = {
            "dataset": "/dataset",
            "packages": [
                {
                    "manager": "apt",
                    "package": "curl",
                    "images": ["debian:bookworm"],
                    "contexts": [
                        {
                            "requirement": "curl",
                            "images": ["debian:bookworm"],
                            "environment_id": "env-apt-timeout",
                            "build_contexts": ["ctx-apt-timeout"],
                        }
                    ],
                },
                {
                    "manager": "pip",
                    "package": "numpy",
                    "images": ["python:3.10-bookworm"],
                    "contexts": [
                        {
                            "requirement": "numpy",
                            "images": ["python:3.10-bookworm"],
                            "environment_id": "env-pip-timeout",
                            "build_contexts": ["ctx-pip-timeout"],
                        }
                    ],
                },
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            report = probe_packages(
                analysis,
                settings=ProbeSettings(),
                work_dir=Path(temporary),
                providers=[
                    AptProvider(command_runner=timeout_resolver),
                    PipProvider(command_runner=timeout_resolver),
                ],
            )

        self.assertEqual(report["summary"]["by_status"], {"unavailable": 2})
        self.assertEqual(
            {
                (row["manager"], row["environment_id"], row["reason"])
                for row in report["problems"]
            },
            {
                (
                    "apt",
                    "env-apt-timeout",
                    "domestic distribution metadata resolver timed out",
                ),
                (
                    "pip",
                    "env-pip-timeout",
                    "domestic PyPI resolver timed out",
                ),
            },
        )

    def test_unsupported_provider_reports_each_analysis_environment(self) -> None:
        analysis = {
            "dataset": "/dataset",
            "packages": [
                {
                    "manager": "cargo",
                    "package": "cargo-edit",
                    "contexts": [
                        {
                            "requirement": "cargo-edit@0.13.0",
                            "environment_id": "env-rust-a",
                            "build_contexts": ["ctx-rust-a"],
                        },
                        {
                            "requirement": "cargo-edit@0.13.0",
                            "environment_id": "env-rust-b",
                            "build_contexts": ["ctx-rust-b"],
                        },
                    ],
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            report = probe_packages(
                analysis,
                settings=ProbeSettings(),
                work_dir=Path(temporary),
                providers=[],
            )

        self.assertEqual(report["schema_version"], 2)
        self.assertEqual(report["summary"]["unsupported_results"], 2)
        self.assertEqual(
            {
                (
                    row["environment_id"],
                    tuple(row["build_contexts"]),
                    row["requirement"],
                )
                for row in report["unsupported"]
            },
            {
                ("env-rust-a", ("ctx-rust-a",), "cargo-edit@0.13.0"),
                ("env-rust-b", ("ctx-rust-b",), "cargo-edit@0.13.0"),
            },
        )

    def test_pip_probe_keeps_requirement_and_python_environment_distinct(self) -> None:
        analysis = {
            "dataset": "/dataset",
            "packages": [
                {
                    "manager": "pip",
                    "package": "numpy",
                    "specs": {"numpy<2": 1, "numpy==1.23.5": 1},
                    "images": ["ubuntu:22.04"],
                    "contexts": [
                        {
                            "requirement": "numpy<2",
                            "images": ["ubuntu:22.04"],
                            "environment_id": "env-py310",
                            "build_contexts": ["ctx-py310"],
                        },
                        {
                            "requirement": "numpy==1.23.5",
                            "images": ["ubuntu:22.04"],
                            "python_version": "3.9",
                            "environment_id": "env-py39",
                            "build_contexts": ["ctx-py39"],
                        },
                    ],
                }
            ],
        }

        def resolver(_command, _timeout, _name):  # noqa: ANN001
            return "\n".join(
                [
                    'ACG\t{"filename":"numpy-1.26.4.whl","package":"numpy","requirement":"numpy<2","sha256":"","state":"resolved","url":"https://mirror.example/numpy-1.26.4.whl","version":"1.26.4"}',
                    'ACG\t{"filename":"numpy-1.23.5.whl","package":"numpy","requirement":"numpy==1.23.5","sha256":"","state":"resolved","url":"https://mirror.example/numpy-1.23.5.whl","version":"1.23.5"}',
                ]
            )

        provider = PipProvider(command_runner=resolver)

        def sample(package, requirement, environment, record, settings):  # noqa: ANN001
            del record, settings
            return ProbeResult(
                "pip",
                package,
                environment.identity,
                "fast",
                "controlled",
                requirement=requirement,
                python_version=environment.python_version,
                implementation=environment.implementation,
                abi=environment.abi,
                platform=environment.platform,
            )

        with tempfile.TemporaryDirectory() as temporary, patch.object(
            provider, "_sample", side_effect=sample
        ):
            report = probe_packages(
                analysis,
                settings=ProbeSettings(),
                work_dir=Path(temporary),
                providers=[provider],
            )
        rows = report["results"]
        self.assertEqual(report["schema_version"], 2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            {(row["requirement"], row["python_version"]) for row in rows},
            {("numpy<2", "3.10"), ("numpy==1.23.5", "3.9")},
        )
        self.assertEqual(
            {row["abi"] for row in rows}, {"cp310", "cp39"}
        )
        self.assertEqual(
            {
                (row["environment_id"], tuple(row["build_contexts"]))
                for row in rows
            },
            {("env-py310", ("ctx-py310",)), ("env-py39", ("ctx-py39",))},
        )

    def test_old_v1_probe_report_remains_warmable(self) -> None:
        class ControlledProvider:
            manager = "pip"

            def probe(self, *_args, **_kwargs):  # noqa: ANN001
                raise AssertionError("probe should not run")

            def warm(self, rows, **_kwargs):  # noqa: ANN001
                self.rows = rows
                return [
                    WarmResult(
                        "pip",
                        str(row["package"]),
                        str(row.get("environment")),
                        "cached",
                        "controlled",
                    )
                    for row in rows
                ]

        provider = ControlledProvider()
        report = {
            "schema_version": 1,
            "dataset": "/legacy",
            "problems": [
                {
                    "manager": "pip",
                    "package": "torch",
                    "environment": "cpython:3.10:cp310:manylinux_2_35_x86_64:amd64",
                    "status": "slow",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            result = warm_packages(
                report,
                gateway_url="http://127.0.0.1:8080/v1/cache",
                timeout_seconds=10,
                work_dir=Path(temporary),
                providers=[provider],
            )
        self.assertEqual(provider.rows[0]["package"], "torch")
        self.assertEqual(result["schema_version"], 2)

    def test_pip_warm_pins_probed_version_before_official_resolution(self) -> None:
        provider = PipProvider()
        row = {
            "manager": "pip",
            "package": "torch",
            "requirement": "torch",
            "environment": "cpython:3.10:cp310:manylinux_2_35_x86_64:amd64",
            "status": "slow",
            "version": "2.7.1",
        }
        resolved = {
            ("torch", "torch==2.7.1"): {
                "package": "torch",
                "requirement": "torch==2.7.1",
                "state": "resolved",
                "url": "https://files.pythonhosted.org/packages/aa/bb/torch.whl",
            }
        }
        warmed = WarmResult(
            "pip",
            "torch",
            row["environment"],
            "cached",
            "controlled",
            requirement="torch",
        )
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            provider, "_resolve", return_value=resolved
        ) as resolver, patch.object(
            provider, "_fetch_to_cache", return_value=warmed
        ):
            result = provider.warm(
                [row],
                gateway_url="http://127.0.0.1:8080/v1/cache",
                timeout_seconds=10,
                work_dir=Path(temporary),
            )
        self.assertEqual(result, [warmed])
        self.assertEqual(
            resolver.call_args.args[1], [("torch", "torch==2.7.1")]
        )
        self.assertEqual(
            resolver.call_args.kwargs["index_url"],
            "http://127.0.0.1:8080/v1/cache/pypi-simple",
        )
        self.assertTrue(resolver.call_args.kwargs["prefer_fallback"])

    def test_pip_warm_deduplicates_wheel_and_preserves_consumers(self) -> None:
        provider = PipProvider()
        environment = "cpython:3.10:cp310:manylinux_2_35_x86_64:amd64"
        rows = [
            {
                "manager": "pip",
                "package": "torch",
                "requirement": "torch==2.7.1",
                "environment": environment,
                "environment_id": environment_id,
                "build_contexts": [context_id],
                "status": "slow",
                "version": "2.7.1",
            }
            for environment_id, context_id in (
                ("env-py-a", "ctx-py-a"),
                ("env-py-b", "ctx-py-b"),
            )
        ]
        resolved = {
            ("torch", "torch==2.7.1"): {
                "package": "torch",
                "requirement": "torch==2.7.1",
                "state": "resolved",
                "url": "https://files.pythonhosted.org/packages/aa/bb/torch.whl",
                "sha256": "a" * 64,
            }
        }
        warmed = WarmResult(
            "pip",
            "torch",
            environment,
            "cached",
            "controlled",
            requirement="torch==2.7.1",
        )
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            provider, "_resolve", return_value=resolved
        ) as resolver, patch.object(
            provider, "_fetch_to_cache", return_value=warmed
        ) as fetch:
            results = provider.warm(
                rows,
                gateway_url="http://127.0.0.1:8080/v1/cache",
                timeout_seconds=10,
                work_dir=Path(temporary),
            )

        self.assertEqual(resolver.call_count, 2)
        fetch.assert_called_once()
        self.assertEqual(
            {(row.environment_id, row.build_contexts) for row in results},
            {
                ("env-py-a", ("ctx-py-a",)),
                ("env-py-b", ("ctx-py-b",)),
            },
        )

    def test_package_warm_only_consumes_problem_rows(self) -> None:
        class ControlledProvider:
            manager = "apt"

            def probe(self, *_args, **_kwargs):  # noqa: ANN001
                raise AssertionError("probe should not run")

            def warm(self, rows, **_kwargs):  # noqa: ANN001
                self.rows = rows
                return [
                    WarmResult(
                        "apt",
                        str(row["package"]),
                        str(row.get("environment")),
                        "cached",
                        "controlled",
                    )
                    for row in rows
                ]

        provider = ControlledProvider()
        report = {
            "dataset": "/dataset",
            "problems": [
                {"manager": "apt", "package": "slow", "status": "slow", "environment": "ubuntu:jammy:amd64"},
                {"manager": "apt", "package": "missing", "status": "unavailable", "environment": "ubuntu:jammy:amd64"},
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            result = warm_packages(
                report,
                gateway_url="http://127.0.0.1:8080/v1/cache",
                timeout_seconds=10,
                work_dir=Path(temporary),
                providers=[provider],
            )
        self.assertEqual([row["package"] for row in provider.rows], ["slow", "missing"])
        self.assertEqual(result["summary"]["by_status"], {"cached": 2})

    def test_apt_warm_resolves_only_exact_declared_repositories(self) -> None:
        provider = AptProvider()
        contexts = [
            {
                "upstream_url": "https://repo.example/apt",
                "suite": "stable",
                "components": ["main"],
                "task": "task-1",
                "source": "environment/Dockerfile",
            }
        ]
        resolved = {
            "state": "resolved",
            "version": "1.0",
            "filename": "pool/v/vendor-package_1.0_amd64.deb",
            "sha256": "a" * 64,
            "size": 10,
            "site": (
                "http://127.0.0.1:8080/v1/cache/"
                + repository_source_name("https://repo.example/apt")
            ),
        }
        warmed = WarmResult(
            "apt",
            "",
            None,
            "cached",
            "controlled",
            first_cache_state="MISS",
            verification_cache_state="HIT",
            size=10,
        )
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            provider, "_resolve_repository_context", return_value=resolved
        ) as resolver, patch.object(
            provider, "_fetch_to_cache", return_value=warmed
        ) as fetch:
            results = provider.warm(
                [
                    {
                        "manager": "apt",
                        "package": "vendor-package",
                        "status": "unavailable",
                        "environment": "ubuntu:jammy:amd64",
                        "repository_contexts": contexts,
                    }
                ],
                gateway_url="http://127.0.0.1:8080/v1/cache",
                timeout_seconds=10,
                work_dir=Path(temporary),
            )

        resolver.assert_called_once()
        self.assertEqual(resolver.call_args.args[2], contexts[0])
        self.assertEqual(
            fetch.call_args.args[1],
            repository_source_name("https://repo.example/apt"),
        )
        self.assertEqual([row.status for row in results], ["cached"])

    def test_apt_warm_deduplicates_artifact_and_preserves_consumers(self) -> None:
        provider = AptProvider()
        rows = [
            {
                "manager": "apt",
                "package": "curl",
                "status": "slow",
                "environment": "ubuntu:jammy:amd64",
                "environment_id": environment_id,
                "build_contexts": [context_id],
                "filename": "pool/c/curl_1.0_amd64.deb",
                "site": "https://mirror.example/ubuntu",
                "sha256": "a" * 64,
            }
            for environment_id, context_id in (
                ("env-a", "ctx-a"),
                ("env-b", "ctx-b"),
            )
        ]
        warmed = WarmResult(
            "apt",
            "",
            None,
            "cached",
            "controlled",
            first_cache_state="MISS",
            verification_cache_state="HIT",
            size=10,
        )
        with tempfile.TemporaryDirectory() as temporary, patch.object(
            provider, "_fetch_to_cache", return_value=warmed
        ) as fetch:
            results = provider.warm(
                rows,
                gateway_url="http://127.0.0.1:8080/v1/cache",
                timeout_seconds=10,
                work_dir=Path(temporary),
            )

        fetch.assert_called_once()
        self.assertEqual(
            {(row.environment_id, row.build_contexts) for row in results},
            {("env-a", ("ctx-a",)), ("env-b", ("ctx-b",))},
        )

    def test_exact_repository_resolver_tries_gzip_after_xz_upstream_error(
        self,
    ) -> None:
        provider = AptProvider()
        package_record = gzip.compress(
            b"Package: neo4j\nVersion: 4.4.0\n"
            b"Filename: pool/4/neo4j_4.4.0_all.deb\n"
            + f"SHA256: {'a' * 64}\nSize: 10\n\n".encode()
        )

        def gateway_read(url, _timeout):  # noqa: ANN001
            if url.endswith("/InRelease"):
                return b"signed metadata"
            if url.endswith("/Packages.xz"):
                raise HTTPError(url, 502, "upstream error", {}, None)
            if url.endswith("/Packages.gz"):
                return package_record
            raise AssertionError(url)

        with patch.object(provider, "_gateway_read", side_effect=gateway_read):
            record = provider._resolve_repository_context(
                "neo4j",
                AptEnvironment("ubuntu", "jammy", "amd64", "image"),
                {
                    "upstream_url": "https://debian.neo4j.com",
                    "suite": "stable",
                    "components": ["4.4"],
                },
                gateway_url="http://127.0.0.1:8080/v1/cache",
                timeout_seconds=10,
            )

        self.assertIsNotNone(record)
        self.assertEqual(record["filename"], "pool/4/neo4j_4.4.0_all.deb")

    def test_exact_repository_resolver_resolves_virtual_package_provider(
        self,
    ) -> None:
        provider = AptProvider()
        package_record = gzip.compress(
            b"Package: postgresql-16\nVersion: 16.1\n"
            b"Provides: postgresql-16-jit-llvm (= 15), postgresql-contrib-16\n"
            b"Filename: pool/main/p/postgresql-16/postgresql-16_16.1_amd64.deb\n"
            + f"SHA256: {'b' * 64}\nSize: 20\n\n".encode()
        )

        def gateway_read(url, _timeout):  # noqa: ANN001
            if url.endswith("/InRelease"):
                return b"signed metadata"
            if url.endswith("/Packages.xz"):
                raise HTTPError(url, 404, "not found", {}, None)
            if url.endswith("/Packages.gz"):
                return package_record
            raise AssertionError(url)

        with patch.object(provider, "_gateway_read", side_effect=gateway_read):
            record = provider._resolve_repository_context(
                "postgresql-contrib-16",
                AptEnvironment("ubuntu", "jammy", "amd64", "image"),
                {
                    "upstream_url": "https://apt.postgresql.org/pub/repos/apt",
                    "suite": "$CODENAME-pgdg",
                    "components": ["main"],
                },
                gateway_url="http://127.0.0.1:8080/v1/cache",
                timeout_seconds=10,
            )

        self.assertIsNotNone(record)
        self.assertEqual(
            record["filename"],
            "pool/main/p/postgresql-16/postgresql-16_16.1_amd64.deb",
        )

    def test_nodesource_setup_expands_runtime_apt_dependencies(self) -> None:
        dependencies = list(
            apt_dependencies(
                "curl -fsSL https://deb.nodesource.com/setup_18.x | bash -"
            )
        )
        self.assertEqual(
            {(row.kind, row.url) for row in dependencies},
            {
                ("apt-bootstrap", "https://deb.nodesource.com/setup_18.x"),
                (
                    "signing-key",
                    "https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key",
                ),
                ("repository", "https://deb.nodesource.com/node_18.x"),
            },
        )

    def test_launchpad_ppa_expands_to_fixed_repository_origin(self) -> None:
        dependencies = list(
            apt_dependencies("add-apt-repository -y ppa:deadsnakes/ppa")
        )
        self.assertEqual(
            [(row.kind, row.action, row.url) for row in dependencies],
            [
                (
                    "repository",
                    "cache",
                    "https://ppa.launchpadcontent.net/deadsnakes/ppa/ubuntu",
                )
            ],
        )

    def test_apt_candidates_compile_to_safe_gateway_sources(self) -> None:
        report = {
            "kind": "artifact-cache-apt-candidate-report",
            "dataset": "/dataset",
            "cache_candidates": [
                {
                    "url": "https://repo.example/apt/ubuntu",
                    "kind": "repository",
                    "query_present": False,
                },
                {
                    "url": "https://keys.example/vendor.asc",
                    "kind": "signing-key",
                    "query_present": False,
                },
                {
                    "url": "https://downloads.example/package.deb",
                    "kind": "deb-artifact",
                    "query_present": False,
                },
            ],
            "packages_needing_resolution": [{"package": "curl"}],
        }
        plan = compile_apt_gateway_plan(report)
        self.assertEqual(len(plan["sources"]), 3)
        repository = next(
            source for source in plan["sources"] if source["kind"] == "apt-repository"
        )
        self.assertEqual(repository["name"], "apt-repo-repo-example-apt-ubuntu")
        self.assertNotRegex(repository["name"], r"-[0-9a-f]{8}$")
        self.assertRegex(repository["config_updated_at"], r"^\d{4}-\d{2}-\d{2}$")
        self.assertEqual(repository["config_update_policy"], "manual")
        self.assertEqual(repository["allowed_path_prefixes"], ["dists/", "pool/"])
        self.assertEqual(repository["mutable_path_prefixes"], ["dists/"])
        static = [
            source for source in plan["sources"] if source["kind"] == "static-objects"
        ]
        self.assertTrue(all("allowed_exact_paths" in source for source in static))
        for source in static:
            self.assertNotRegex(source["name"], r"-[0-9a-f]{8}$")
        merged = merge_gateway_config(
            {"sources": [{"name": "base", "base_url": "https://base.example/"}]},
            plan,
        )
        self.assertEqual(len(merged["sources"]), 4)

    def test_gateway_config_merges_compatible_static_exact_paths(self) -> None:
        base = {
            "sources": [
                {
                    "name": "apt-objects-example",
                    "kind": "static-objects",
                    "ecosystem": "apt",
                    "base_url": "https://objects.example/",
                    "allowed_redirect_origins": [],
                    "allowed_exact_paths": ["setup_18.x"],
                    "allow_query": False,
                    "config_updated_at": "2026-09-01",
                    "config_update_policy": "manual",
                }
            ]
        }
        plan = {
            "sources": [
                {
                    "name": "apt-objects-example",
                    "kind": "static-objects",
                    "ecosystem": "apt",
                    "base_url": "https://objects.example/",
                    "allowed_redirect_origins": [],
                    "allowed_exact_paths": ["setup_22.x"],
                    "allow_query": False,
                    "config_updated_at": "2026-09-07",
                    "config_update_policy": "manual",
                }
            ]
        }
        merged = merge_gateway_config(base, plan)
        self.assertEqual(
            merged["sources"][0]["allowed_exact_paths"],
            ["setup_18.x", "setup_22.x"],
        )
        self.assertEqual(merged["sources"][0]["config_updated_at"], "2026-09-07")

        conflicting = json.loads(json.dumps(plan))
        conflicting["sources"][0]["base_url"] = "https://other.example/"
        with self.assertRaisesRegex(ValueError, "conflicting gateway source"):
            merge_gateway_config(base, conflicting)

    def test_apt_monitor_selects_dependencies_not_covered_by_domestic_mirror(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = make_apt_dataset(root)
            analysis = analyze(dataset, "referenced")
            output = root / "output"
            write_outputs(output, analysis)
            apt_json = (output / "apt-cache-candidates.json").read_text(
                encoding="utf-8"
            )
            apt_csv_exists = (output / "apt-cache-candidates.csv").is_file()

        candidates = analysis["apt"]["cache_candidates"]
        by_url = {row["url"]: row for row in candidates}
        self.assertEqual(analysis["summary"]["scanned_apt_source_files"], 1)
        self.assertEqual(analysis["summary"]["apt_dependency_urls"], 5)
        self.assertEqual(analysis["summary"]["apt_cache_candidate_urls"], 4)
        self.assertEqual(
            by_url["https://dl.google.com/linux/linux_signing_key.pub"]["kind"],
            "signing-key",
        )
        self.assertEqual(
            by_url["https://dl.google.com/linux/chrome/deb/"]["kind"],
            "repository",
        )
        self.assertEqual(
            by_url["https://dl.google.com/linux/chrome/deb/"]["cache_mode"],
            "apt-repository-proxy",
        )
        self.assertEqual(
            by_url[
                "https://developer.download.nvidia.com/compute/cuda/repos/"
                "ubuntu2204/x86_64/cuda-keyring_1.1-1_all.deb"
            ]["kind"],
            "deb-artifact",
        )
        self.assertIn(
            "google-chrome-stable",
            by_url["https://dl.google.com/linux/chrome/deb/"]["packages_in_same_tasks"],
        )
        official = next(
            row
            for row in analysis["apt"]["dependencies"]
            if row["url"] == "http://archive.ubuntu.com/ubuntu"
        )
        self.assertEqual(official["action"], "rewrite")
        self.assertEqual(analysis["summary"]["apt_packages_needing_resolution"], 2)
        self.assertNotIn("must-not-leak", apt_json)
        self.assertTrue(apt_csv_exists)

    def test_run_command_terminates_skopeo_when_interrupted(self) -> None:
        process = MagicMock()
        process.stdout = io.StringIO("")
        process.stderr = io.StringIO("")
        process.wait.side_effect = [KeyboardInterrupt(), 0]
        with patch(
            "artifact_cache_gateway.gateway.services.image.subprocess.Popen",
            return_value=process,
        ):
            with self.assertRaises(KeyboardInterrupt):
                run_command(["skopeo", "copy"], {}, 60.0)
        process.terminate.assert_called_once_with()

    def test_run_command_hides_inspect_stderr_until_failure_summary(self) -> None:
        process = MagicMock()
        process.stdout = io.StringIO("")
        process.stderr = io.StringIO("level=fatal expected missing target\n")
        process.wait.return_value = 1
        terminal = io.StringIO()
        with patch(
            "artifact_cache_gateway.gateway.services.image.subprocess.Popen",
            return_value=process,
        ), redirect_stderr(terminal), self.assertRaises(MirrorError):
            run_command(["skopeo", "inspect"], {}, 60.0)
        self.assertEqual(terminal.getvalue(), "")

    def test_analyze_and_build_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            dataset = make_dataset(Path(temporary))
            analysis = analyze(dataset, "referenced")
            plan = build_image_plan(
                analysis,
                registry="https://harbor.example.internal/",
                project="public-mirror",
                platform=Platform.parse("linux/amd64"),
                source_prefix_map=parse_source_prefix_map(
                    '{"docker.io":"m.daocloud.io/docker.io"}'
                ),
            )
        self.assertEqual(analysis["summary"]["tasks"], 1)
        self.assertEqual(analysis["summary"]["unique_images"], 1)
        self.assertEqual(
            plan["images"][0]["target_repository"],
            "harbor.example.internal/public-mirror/library/ubuntu",
        )
        self.assertEqual(
            plan["images"][0]["mirror_source_ref"],
            "m.daocloud.io/docker.io/library/ubuntu:22.04",
        )
        self.assertEqual(
            plan["images"][0]["target_tag_ref"],
            "harbor.example.internal/public-mirror/library/ubuntu:22.04",
        )

    def test_prepare_cli_is_plan_only_without_execute(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = make_dataset(root)
            output = root / "output"
            env_file = root / "config.local.env"
            env_file.write_text(
                'ARTIFACT_MIRROR_REGISTRY="harbor.example.internal"\n'
                'ARTIFACT_MIRROR_PROJECT="public-mirror"\n',
                encoding="utf-8",
            )
            exit_code = prepare_main(
                [
                    "analyze",
                    str(dataset),
                    "--output-dir",
                    str(output),
                    "--registry",
                    "harbor.example.internal",
                    "--env-file",
                    str(env_file),
                    "--no-probe-domestic-packages",
                ]
            )
            self.assertEqual(exit_code, 0)
            self.assertTrue((output / "summary.json").is_file())
            self.assertTrue((output / "image-mirror-plan.json").is_file())
            self.assertFalse((output / "image-mirror-result.json").exists())
            probe_report = json.loads(
                (output / "package-probe-report.json").read_text(encoding="utf-8")
            )
            self.assertEqual(probe_report["schema_version"], 2)
            self.assertFalse(probe_report["enabled"])

    def test_prepare_cli_runs_analysis_filter_and_upload_in_one_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = make_dataset(root)
            output = root / "output"
            env_file = root / "config.local.env"
            env_file.write_text(
                'YICLOUD_HARBOR_HOST="harbor.example.internal"\n'
                'YICLOUD_HARBOR_USERNAME="robot"\n'
                'YICLOUD_HARBOR_PASSWORD="secret"\n'
                'ARTIFACT_MIRROR_PROJECT="public-mirror"\n'
                'ARTIFACT_MIRROR_SOURCE_PREFIX_MAP_JSON='
                "'{\"docker.io\":\"m.daocloud.io/docker.io\"}'\n"
                'ARTIFACT_MIRROR_UPSTREAM_PROXY="http://proxy.example:7890"\n'
                'ARTIFACT_MIRROR_DIRECT_UPSTREAM_WITH_PROXY="true"\n',
                encoding="utf-8",
            )

            def fake_mirror(
                plan,
                *,
                output_path,
                selection_path,
                authfile,
                upstream_proxy,
                direct_upstream_with_proxy,
                **_,
            ):  # noqa: ANN001
                self.assertTrue(authfile.is_file())
                self.assertEqual(upstream_proxy, "http://proxy.example:7890")
                self.assertTrue(direct_upstream_with_proxy)
                row = {**plan["images"][0], "status": "uploaded"}
                selection = {
                    "kind": "artifact-cache-image-mirror-selection",
                    "existing": [],
                    "missing": [plan["images"][0]],
                }
                result = {
                    "kind": "artifact-cache-image-mirror-result",
                    "images": [row],
                }
                selection_path.write_text(json.dumps(selection), encoding="utf-8")
                output_path.write_text(json.dumps(result), encoding="utf-8")
                return result

            # mirror_images moved to preparer/commands.py along with mirror_command, so the
            # patch target must point there too
            with patch(
                "artifact_cache_gateway.harbor_tasks.preparer.commands.mirror_images",
                side_effect=fake_mirror,
            ):
                exit_code = prepare_main(
                    [
                        "prepare",
                        str(dataset),
                        "--output-dir",
                        str(output),
                        "--env-file",
                        str(env_file),
                        "--no-probe-domestic-packages",
                        "--execute",
                    ]
                )

            self.assertEqual(exit_code, 0)
            self.assertTrue((output / "report.md").is_file())
            self.assertTrue((output / "summary.json").is_file())
            self.assertTrue((output / "image-mirror-plan.json").is_file())
            self.assertTrue((output / "image-mirror-selection.json").is_file())
            self.assertTrue((output / "image-mirror-result.json").is_file())

    def test_execute_records_source_and_target_digests(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = make_dataset(root)
            analysis = analyze(dataset, "referenced")
            plan = build_image_plan(
                analysis,
                registry="harbor.example.internal",
                project="public-mirror",
                platform=Platform.parse("linux/amd64"),
                source_prefix_map={
                    "docker.io": "m.daocloud.io/docker.io"
                },
            )
            commands: list[list[str]] = []
            environments: list[dict[str, str]] = []
            target_inspects = 0

            def fake_runner(command, environment, timeout):  # noqa: ANN001
                nonlocal target_inspects
                commands.append(list(command))
                environments.append(dict(environment))
                if "copy" in command:
                    self.assertEqual(timeout, 7200.0)
                    return ""
                if "harbor.example.internal" in command[-1]:
                    target_inspects += 1
                    if target_inspects == 1:
                        raise MirrorError("manifest unknown: status code 404")
                    return TARGET_DIGEST + "\n"
                return SOURCE_DIGEST + "\n"

            result_path = root / "result.json"
            selection_path = root / "selection.json"
            result = mirror_images(
                plan,
                output_path=result_path,
                selection_path=selection_path,
                upstream_proxy="http://proxy.example:7890",
                command_runner=fake_runner,
            )
            saved = json.loads(result_path.read_text(encoding="utf-8"))
            selection = json.loads(selection_path.read_text(encoding="utf-8"))

        self.assertEqual(len(commands), 4)
        self.assertIn("inspect", commands[0])
        self.assertIn("inspect", commands[1])
        self.assertIn("copy", commands[2])
        self.assertIn("inspect", commands[3])
        self.assertEqual(
            commands[2][-2],
            "docker://m.daocloud.io/docker.io/library/ubuntu:22.04",
        )
        self.assertEqual(
            commands[2][-1],
            "docker://harbor.example.internal/public-mirror/library/ubuntu:22.04",
        )
        self.assertEqual(result["images"][0]["source_digest"], SOURCE_DIGEST)
        self.assertEqual(saved["images"][0]["target_digest"], TARGET_DIGEST)
        self.assertEqual(len(selection["missing"]), 1)
        self.assertIn("harbor.example.internal", environments[0]["NO_PROXY"])
        self.assertNotIn("HTTPS_PROXY", environments[0])
        self.assertEqual(environments[1]["HTTPS_PROXY"], "http://proxy.example:7890")

    def test_original_registry_uses_proxy_only_after_mirrors_fail(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            analysis = analyze(make_dataset(root), "referenced")
            plan = build_image_plan(
                analysis,
                registry="harbor.example.internal",
                project="public-mirror",
                platform=Platform.parse("linux/amd64"),
                source_prefix_map={"docker.io": ["docker.1ms.run"]},
            )
            calls: list[tuple[list[str], dict[str, str]]] = []
            target_inspects = 0

            def fallback_runner(command, environment, timeout):  # noqa: ANN001
                nonlocal target_inspects
                calls.append((list(command), dict(environment)))
                reference = command[-1]
                if "harbor.example.internal" in reference:
                    target_inspects += 1
                    if target_inspects == 1:
                        raise MirrorError("manifest unknown: status code 404")
                    return TARGET_DIGEST + "\n"
                if "docker.1ms.run" in reference:
                    raise MirrorError("upstream returned 429")
                if "copy" in command:
                    return ""
                return SOURCE_DIGEST + "\n"

            result = mirror_images(
                plan,
                output_path=root / "result.json",
                upstream_proxy="http://proxy.example:7890",
                direct_upstream_with_proxy=True,
                command_runner=fallback_runner,
            )

        row = result["images"][0]
        self.assertEqual(row["transfer_source_ref"], "docker.io/library/ubuntu:22.04")
        self.assertEqual(row["transfer_source_mode"], "original-proxy")
        mirror_call = next(call for call in calls if "docker.1ms.run" in call[0][-1])
        direct_call = next(
            call for call in calls if call[0][-1] == "docker://docker.io/library/ubuntu:22.04"
        )
        self.assertNotIn("HTTPS_PROXY", mirror_call[1])
        self.assertEqual(direct_call[1]["HTTPS_PROXY"], "http://proxy.example:7890")

    def test_execute_persists_failure_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = make_dataset(root)
            plan = build_image_plan(
                analyze(dataset, "referenced"),
                registry="harbor.example.internal",
                project="public-mirror",
                platform=Platform.parse("linux/amd64"),
            )

            calls = 0

            def failed_runner(command, environment, timeout):  # noqa: ANN001
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise MirrorError("manifest unknown: status code 404")
                raise MirrorError("controlled source inspect failure")

            result_path = root / "result.json"
            with self.assertRaises(MirrorError):
                mirror_images(
                    plan,
                    output_path=result_path,
                    command_runner=failed_runner,
                )
            result = json.loads(result_path.read_text(encoding="utf-8"))

        self.assertEqual(result["images"][0]["status"], "failed")
        self.assertEqual(result["images"][0]["error_type"], "MirrorError")

    def test_existing_target_is_not_copied(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dataset = make_dataset(root)
            plan = build_image_plan(
                analyze(dataset, "referenced"),
                registry="harbor.example.internal",
                project="public-mirror",
                platform=Platform.parse("linux/amd64"),
            )
            commands: list[list[str]] = []

            def existing_runner(command, environment, timeout):  # noqa: ANN001
                commands.append(list(command))
                return TARGET_DIGEST + "\n"

            result = mirror_images(
                plan,
                output_path=root / "result.json",
                selection_path=root / "selection.json",
                command_runner=existing_runner,
            )

        self.assertEqual(len(commands), 1)
        self.assertEqual(result["images"][0]["status"], "already-present")

    def test_missing_images_are_copied_concurrently(self) -> None:
        analysis = {
            "dataset": "/dataset",
            "images": [
                {"image": "ubuntu:22.04"},
                {"image": "node:20-bookworm"},
            ],
        }
        plan = build_image_plan(
            analysis,
            registry="harbor.example.internal",
            project="public-mirror",
            platform=Platform.parse("linux/amd64"),
        )
        target_inspects: dict[str, int] = {}
        active_copies = 0
        maximum_active_copies = 0
        lock = threading.Lock()
        both_started = threading.Barrier(2)

        def concurrent_runner(command, environment, timeout):  # noqa: ANN001
            nonlocal active_copies, maximum_active_copies
            image_ref = command[-1].removeprefix("docker://")
            if "copy" in command:
                with lock:
                    active_copies += 1
                    maximum_active_copies = max(
                        maximum_active_copies, active_copies
                    )
                both_started.wait(timeout=2.0)
                time.sleep(0.02)
                with lock:
                    active_copies -= 1
                return ""
            if image_ref.startswith("harbor.example.internal/"):
                with lock:
                    target_inspects[image_ref] = target_inspects.get(image_ref, 0) + 1
                    inspect_number = target_inspects[image_ref]
                if inspect_number == 1:
                    raise MirrorError("manifest unknown: status code 404")
                return TARGET_DIGEST + "\n"
            return SOURCE_DIGEST + "\n"

        with tempfile.TemporaryDirectory() as temporary:
            result = mirror_images(
                plan,
                output_path=Path(temporary) / "result.json",
                concurrency=2,
                command_runner=concurrent_runner,
            )

        self.assertEqual(maximum_active_copies, 2)
        self.assertEqual(result["concurrency"], 2)
        self.assertEqual(
            [row["status"] for row in result["images"]],
            ["uploaded", "uploaded"],
        )

    def test_rejects_out_of_range_concurrency(self) -> None:
        plan = build_image_plan(
            {"dataset": "/dataset", "images": []},
            registry="harbor.example.internal",
            project="public-mirror",
            platform=Platform.parse("linux/amd64"),
        )
        with tempfile.TemporaryDirectory() as temporary, self.assertRaises(MirrorError):
            mirror_images(
                plan,
                output_path=Path(temporary) / "result.json",
                concurrency=33,
                command_runner=lambda *_: "",
            )

    def test_project_local_env_parser_preserves_quoted_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.local.env"
            path.write_text(
                'YICLOUD_HARBOR_USERNAME="robot$user"\n'
                'YICLOUD_HARBOR_PASSWORD="value#with$dollar"\n',
                encoding="utf-8",
            )
            values = parse_local_env(path)
        self.assertEqual(values["YICLOUD_HARBOR_USERNAME"], "robot$user")
        self.assertEqual(values["YICLOUD_HARBOR_PASSWORD"], "value#with$dollar")


if __name__ == "__main__":
    unittest.main()

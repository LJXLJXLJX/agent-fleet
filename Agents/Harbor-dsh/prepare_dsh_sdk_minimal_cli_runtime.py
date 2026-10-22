"""Build and cache the pinned official DSH CLI runtime."""

from __future__ import annotations

import argparse
import os
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from dsh_runtime_utils import atomic_output, atomic_write_text, sha256, tarball_ready

DEFAULT_VERSION = "0.1.3-alpha.1"
DEFAULT_SOURCE_REF = "dsh-v0.1.3-alpha.1"
DEFAULT_SOURCE_SHA = "d347e703908d0406b7a7ef80e3a0e594d86b2215"
DEFAULT_SOURCE_ARCHIVE_SHA256 = (
    "0f5eda71ba80543f70c679a67e54f48bd1be9c704df6bc1c767ee598d04d666f"
)
DEFAULT_PNPM_VERSION = "11.7.0"


def _value(environ: Mapping[str, str], name: str, default: str) -> str:
    return environ.get(name) or default


def _target() -> str:
    architectures = {"x86_64": "x64", "amd64": "x64"}
    try:
        architecture = architectures[platform.machine().lower()]
    except KeyError as error:
        raise RuntimeError(
            f"DSH CLI source builds do not support architecture {platform.machine()!r}"
        ) from error
    if platform.system() != "Linux":
        raise RuntimeError("DSH CLI source builds currently require Linux")
    return f"node24-linux-{architecture}"


@dataclass(frozen=True)
class Config:
    wheel_dir: Path
    version: str
    source_ref: str
    source_sha: str
    source_archive: Path
    source_archive_url: str
    source_archive_sha256: str
    pnpm_version: str
    target: str
    runtime_tarball: Path
    version_file: Path
    node_runtime_tarball: Path
    npm_registry_url: str
    npm_cache_dir: Path

    @property
    def runtime_version(self) -> str:
        return "|".join(
            (
                self.version,
                self.source_ref,
                self.source_sha,
                self.source_archive_sha256,
                f"pnpm={self.pnpm_version}",
                self.target,
            )
        )

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> Config:
        values = os.environ if environ is None else environ
        wheel_dir = Path(_value(values, "WHEEL_DIR", "python-wheels"))
        version = _value(values, "DSH_CLI_VERSION", DEFAULT_VERSION)
        source_ref = _value(values, "DSH_CLI_SOURCE_REF", DEFAULT_SOURCE_REF)
        source_sha = _value(values, "DSH_CLI_SOURCE_SHA", DEFAULT_SOURCE_SHA)
        runtime_basename = _value(
            values,
            "DSH_CLI_RUNTIME_BASENAME",
            f"dsh-sdk-minimal-cli-runtime-{version}.tar.gz",
        )
        archive_basename = f"deepseek-harness-{source_sha}.tar.gz"
        return cls(
            wheel_dir=wheel_dir,
            version=version,
            source_ref=source_ref,
            source_sha=source_sha,
            source_archive=Path(
                _value(
                    values,
                    "DSH_CLI_SOURCE_ARCHIVE",
                    str(wheel_dir / archive_basename),
                )
            ),
            source_archive_url=_value(
                values,
                "DSH_CLI_SOURCE_ARCHIVE_URL",
                (
                    "https://codeload.github.com/deepseek-ai/deepseek-harness/"
                    f"tar.gz/{source_sha}"
                ),
            ),
            source_archive_sha256=_value(
                values,
                "DSH_CLI_SOURCE_ARCHIVE_SHA256",
                DEFAULT_SOURCE_ARCHIVE_SHA256,
            ),
            pnpm_version=_value(values, "DSH_CLI_PNPM_VERSION", DEFAULT_PNPM_VERSION),
            target=_value(values, "DSH_CLI_BUILD_TARGET", _target()),
            runtime_tarball=Path(
                _value(
                    values,
                    "DSH_CLI_RUNTIME_TARBALL",
                    str(wheel_dir / runtime_basename),
                )
            ),
            version_file=Path(
                _value(
                    values,
                    "DSH_CLI_RUNTIME_VERSION_FILE",
                    str(wheel_dir / "dsh-sdk-minimal-cli-runtime.version"),
                )
            ),
            node_runtime_tarball=Path(
                _value(
                    values,
                    "NODE_RUNTIME_TARBALL",
                    str(wheel_dir / "node-runtime.tar.gz"),
                )
            ),
            npm_registry_url=_value(
                values,
                "NPM_REGISTRY_URL",
                _value(values, "NPM_CONFIG_REGISTRY", "https://registry.npmjs.org"),
            ),
            npm_cache_dir=Path(
                _value(
                    values,
                    "DSH_CLI_NPM_CACHE_DIR",
                    str(wheel_dir / "dsh-sdk-minimal-npm-cache"),
                )
            ),
        )


def runtime_tarball_ready(path: Path) -> bool:
    if not tarball_ready(path):
        return False
    with tarfile.open(path) as archive:
        names = {name.lstrip("./") for name in archive.getnames()}
    return {"bin/dsh", "bin/dsh-rg"}.issubset(names)


def source_archive_ready(config: Config) -> bool:
    return (
        tarball_ready(config.source_archive)
        and sha256(config.source_archive) == config.source_archive_sha256
    )


def runtime_ready(config: Config) -> bool:
    if not runtime_tarball_ready(config.runtime_tarball):
        return False
    try:
        recorded = config.version_file.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return recorded == config.runtime_version


def _download_source(config: Config) -> None:
    with atomic_output(config.source_archive) as temporary:
        with (
            urllib.request.urlopen(config.source_archive_url, timeout=120) as response,
            temporary.open("wb") as stream,
        ):
            shutil.copyfileobj(response, stream)
        if sha256(temporary) != config.source_archive_sha256:
            raise RuntimeError("downloaded DSH source archive checksum mismatch")


def _source_root(config: Config, temporary: Path) -> Path:
    source_parent = temporary / "source"
    source_parent.mkdir()
    with tarfile.open(config.source_archive) as archive:
        archive.extractall(source_parent, filter="data")
    roots = [path for path in source_parent.iterdir() if path.is_dir()]
    if len(roots) != 1 or not (roots[0] / "pnpm-lock.yaml").is_file():
        raise RuntimeError("DSH source archive has an unexpected layout")
    return roots[0]


def _run(command: Sequence[str], *, cwd: Path, env: Mapping[str, str]) -> None:
    subprocess.run(command, check=True, cwd=cwd, env=env)


def _version_matches(output: str, expected: str) -> bool:
    return (
        re.search(rf"(?<![0-9A-Za-z.-]){re.escape(expected)}(?![0-9A-Za-z.-])", output)
        is not None
    )


def prepare(config: Config) -> None:
    if config.target != _target():
        raise RuntimeError(
            f"DSH CLI target {config.target!r} does not match this runner's "
            f"supported target {_target()!r}"
        )
    dsh_runtime_ready = runtime_ready(config)
    if dsh_runtime_ready and source_archive_ready(config):
        print(f"[prepare] skip DSH CLI runtime (cached): {config.runtime_tarball}")
        return
    if not source_archive_ready(config):
        _download_source(config)
    if not source_archive_ready(config):
        raise RuntimeError("DSH source archive is invalid after download")
    if dsh_runtime_ready:
        print(f"[prepare] skip DSH CLI runtime (cached): {config.runtime_tarball}")
        return
    if not tarball_ready(config.node_runtime_tarball):
        raise RuntimeError(
            f"DSH source build requires a valid Node archive: {config.node_runtime_tarball}"
        )

    config.wheel_dir.mkdir(parents=True, exist_ok=True)
    config.npm_cache_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="agent-fleet-dsh-cli-", dir="/tmp"
    ) as temporary_name:
        temporary = Path(temporary_name)
        node_root = temporary / "node"
        node_root.mkdir()
        with tarfile.open(config.node_runtime_tarball) as archive:
            archive.extractall(node_root, filter="data")

        node_bin = next(node_root.glob("*/bin/node"), None)
        corepack_bin = next(node_root.glob("*/bin/corepack"), None)
        if node_bin is None or corepack_bin is None:
            raise RuntimeError("Node archive contains no node/corepack binaries")
        completed = subprocess.run(
            [str(node_bin), "-p", "process.versions.node.split('.')[0]"],
            check=True,
            text=True,
            capture_output=True,
        )
        if int(completed.stdout.strip()) < 22:
            raise RuntimeError("DSH requires Node 22 or newer")

        node_bin_dir = node_bin.parent
        env = os.environ.copy()
        env.update(
            {
                "COREPACK_HOME": str(config.npm_cache_dir / "corepack"),
                "DSH_CLIENT_COMMIT_HASH": config.source_sha,
                "NPM_CONFIG_CACHE": str(config.npm_cache_dir / "npm"),
                "NPM_CONFIG_REGISTRY": config.npm_registry_url,
                "PATH": f"{node_bin_dir}{os.pathsep}{env.get('PATH', '')}",
            }
        )
        _run(
            [str(corepack_bin), "enable", "--install-directory", str(node_bin_dir)],
            cwd=temporary,
            env=env,
        )
        pnpm_bin = node_bin_dir / "pnpm"
        source_root = _source_root(config, temporary)
        completed = subprocess.run(
            [str(pnpm_bin), "--version"],
            check=True,
            text=True,
            capture_output=True,
            cwd=source_root,
            env=env,
        )
        if completed.stdout.strip() != config.pnpm_version:
            raise RuntimeError(
                "DSH pnpm version mismatch: "
                f"expected {config.pnpm_version!r}, got {completed.stdout.strip()!r}"
            )
        _run(
            [
                str(pnpm_bin),
                "install",
                "--frozen-lockfile",
                "--store-dir",
                str(config.npm_cache_dir / "pnpm-store"),
            ],
            cwd=source_root,
            env=env,
        )
        _run(
            [
                str(pnpm_bin),
                "exec",
                "tsx",
                "scripts/build-exe-for-python-sdk.ts",
                f"--targets={config.target}",
            ],
            cwd=source_root,
            env=env,
        )

        platform_name, architecture = config.target.split("-")[1:]
        product = (
            source_root
            / "dist-exe"
            / f"deepseek-harness-sdk-runtime-{platform_name}-{architecture}"
        )
        sidecar = product.with_name(f"{product.name}-rg")
        if not product.is_file() or not sidecar.is_file():
            raise RuntimeError(
                "official DSH build did not produce its CLI and rg sidecar"
            )
        completed = subprocess.run(
            [str(product), "--version"],
            check=True,
            text=True,
            capture_output=True,
            env=env,
        )
        if not _version_matches(completed.stdout, config.version):
            raise RuntimeError(
                "prepared DSH runtime version mismatch: "
                f"expected {config.version!r}, got {completed.stdout.strip()!r}"
            )

        runtime_prefix = temporary / "runtime" / "bin"
        runtime_prefix.mkdir(parents=True)
        shutil.copy2(product, runtime_prefix / "dsh")
        shutil.copy2(sidecar, runtime_prefix / "dsh-rg")
        with atomic_output(config.runtime_tarball) as temporary_tar:
            with tarfile.open(temporary_tar, "w:gz") as archive:
                archive.add(runtime_prefix.parent, arcname="")
            if not runtime_tarball_ready(temporary_tar):
                raise RuntimeError("generated DSH runtime archive is invalid")

    atomic_write_text(config.version_file, f"{config.runtime_version}\n")
    print(f"[prepare] built DSH CLI runtime: {config.runtime_tarball}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--print-runtime-version", action="store_true")
    args = parser.parse_args(argv)
    config = Config.from_environment()
    if args.print_runtime_version:
        print(config.runtime_version)
    else:
        prepare(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

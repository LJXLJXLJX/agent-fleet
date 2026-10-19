"""YiCloud services: extracted from the legacy Harbor provider without behavior changes."""

from __future__ import annotations

import asyncio
import base64
import json
import re
import shlex
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
from harbor.environments.base import ExecResult

from .client import (
    SANDBOX_STATUS_REQUEST_ATTEMPTS,
    TERMINAL_FAILURE_STATES,
    YiCloudSandboxReadyTimeoutError,
    _access_token_of,
    _command_url_of,
    _internal_address_of,
    _state_of,
    _status_reason_of,
    _validate_sandbox_binding,
    _yicloud_image_ref,
)
from .config import _validate_managed_image_ref
from .execution import EXIT_MARKER, _parse_sse

HOSTS_BLOCK_BEGIN = "# HARBOR COMPOSE BEGIN"


HOSTS_BLOCK_END = "# HARBOR COMPOSE END"


class ServiceRuntime:
    def __init__(self, name: str, spec: dict[str, Any]) -> None:
        self.name = name
        self.spec = spec
        self.sandbox_id = ""
        self.sandbox_name = ""
        self.command_url = ""
        self.access_token = ""
        self.internal_address = ""
        self.state = "CREATING"


class YiCloudServicesMixin:
    """Internal services methods sharing the adapter instance state."""

    def _load_bundle(self) -> None:
        if self._bundle is not None:
            return
        if not self._bundle_manifest_path:
            return
        path = Path(self._bundle_manifest_path)
        try:
            bundle = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"cannot read OpenSandbox Bundle Manifest {path}: {exc}"
            ) from exc
        if not isinstance(bundle, dict):
            raise TypeError("OpenSandbox Bundle Manifest must be a JSON object")
        schema = bundle.get("schema_version")
        main = bundle.get("main") if schema == 2 else bundle.get("main_service")
        services = bundle.get("services")
        if (
            schema not in {1, 2}
            or not isinstance(main, str)
            or not isinstance(services, dict)
            or main not in services
        ):
            raise RuntimeError(
                "OpenSandbox Bundle Manifest has an invalid schema or main service"
            )
        resolved: dict[str, ServiceRuntime] = {}
        for name, spec in services.items():
            if not isinstance(name, str) or not isinstance(spec, dict):
                raise TypeError(
                    "OpenSandbox Bundle Manifest has an invalid service entry"
                )
            image = spec.get("image") or {}
            ref = image.get("digest_ref") if schema == 2 else image.get("sandbox_ref")
            if not isinstance(ref, str) or not ref:
                raise RuntimeError(
                    f"Bundle service {name!r} has no immutable image reference"
                )
            _validate_managed_image_ref(ref, f"Bundle service {name!r} image")
            resolved[name] = ServiceRuntime(name=name, spec=spec)
        self._bundle = bundle
        self._services = resolved
        self._main_service = main

    def _capability_gate(self) -> None:
        if self._bundle is None:
            return
        requirements = self._bundle.get("requirements") or {}
        unsupported = []
        mapping = {
            "shared_volumes": "shared_volumes",
            "fixed_ip": "fixed_ip",
            "multiple_networks": "multiple_networks",
            "net_admin": "net_admin",
            "sys_admin": "sys_admin",
            "privileged": "privileged",
        }
        for field, label in mapping.items():
            if requirements.get(field):
                unsupported.append(label)
        unsupported.extend(requirements.get("unsupported_features") or [])
        # TODO: Consider a routing environment that falls back to Docker when
        # OpenSandbox cannot satisfy a task's required capabilities.
        if requirements.get("multi_service"):
            for name, runtime in self._services.items():
                if name == self._main_service:
                    continue
                contract = runtime.spec.get("runtime")
                ports = (
                    contract.get("internal_ports")
                    if isinstance(contract, dict)
                    else None
                )
                if not isinstance(ports, list) or not ports:
                    unsupported.append(f"service {name} lacks runtime.internal_ports")
        if unsupported:
            raise RuntimeError(
                "OpenSandbox capability gate rejected Bundle before create: "
                + ", ".join(sorted(str(item) for item in unsupported))
            )

    @staticmethod
    def _service_ports(spec: dict[str, Any]) -> list[int]:
        runtime = spec.get("runtime")
        if isinstance(runtime, dict) and "internal_ports" in runtime:
            ports: list[int] = []
            for item in runtime.get("internal_ports") or []:
                if not isinstance(item, dict):
                    raise TypeError(
                        "Bundle runtime.internal_ports entries must be objects"
                    )
                value = item.get("port")
                protocol = str(item.get("protocol", "tcp")).lower()
                if isinstance(value, int) and protocol == "tcp" and 1 <= value <= 65535:
                    ports.append(value)
                else:
                    raise RuntimeError(
                        "Bundle runtime.internal_ports has an invalid TCP port"
                    )
            return list(dict.fromkeys(ports))

        # Schema v1 compatibility only. Newly materialized schema v2 Bundles
        # must carry runtime.internal_ports from Compose expose/ports or OCI
        # image config and never rediscover them from Dockerfile text here.
        ports: list[int] = []
        for item in spec.get("ports") or []:
            value: Any = item.get("target") if isinstance(item, dict) else item
            if isinstance(value, int):
                ports.append(value)
                continue
            if isinstance(value, str):
                # Compose short syntax: host:container/proto and container/proto.
                candidate = value.split("/", 1)[0].rsplit(":", 1)[-1]
                if candidate.isdigit():
                    ports.append(int(candidate))
        return list(dict.fromkeys(port for port in ports if 1 <= port <= 65535))

    async def _wait_service_running(self, runtime: ServiceRuntime, created: Any) -> Any:
        sandbox = self._sandbox_service
        assert sandbox is not None
        deadline = time.monotonic() + self._ready_timeout_sec
        current = created
        consecutive_status_errors = 0
        while time.monotonic() < deadline:
            try:
                current = await asyncio.to_thread(
                    sandbox.get_sandbox,
                    None,
                    sandbox.models.GetSandboxReq(
                        ProjectName=self._project_name, SandboxId=runtime.sandbox_id
                    ),
                )
            except Exception as error:
                consecutive_status_errors += 1
                if consecutive_status_errors >= SANDBOX_STATUS_REQUEST_ATTEMPTS:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                delay = min(2 ** (consecutive_status_errors - 1), 5, remaining)
                self.logger.warning(
                    "YiCloud Sandbox status request failed transiently; "
                    "retrying in %ss (attempt %s/%s) sandbox_id=%s: %s",
                    delay,
                    consecutive_status_errors,
                    SANDBOX_STATUS_REQUEST_ATTEMPTS,
                    runtime.sandbox_id,
                    error,
                )
                await asyncio.sleep(delay)
                continue
            consecutive_status_errors = 0
            state = _state_of(current) or "unknown"
            if state == "running":
                return current
            if state in TERMINAL_FAILURE_STATES:
                raise RuntimeError(
                    f"OpenSandbox service {runtime.name!r} entered terminal state={state}; "
                    f"reason={_status_reason_of(current) or '<none>'}"
                )
            await asyncio.sleep(3)
        raise YiCloudSandboxReadyTimeoutError(
            f"OpenSandbox service {runtime.name!r} scheduling timed out after "
            f"{self._ready_timeout_sec}s"
        )

    def _service_image_source_ref(self, runtime: ServiceRuntime) -> str:
        image = runtime.spec.get("image") or {}
        return str(image.get("digest_ref") or image.get("sandbox_ref") or "").strip()

    def _service_image_ref(self, runtime: ServiceRuntime) -> str:
        return _yicloud_image_ref(self._service_image_source_ref(runtime))

    @staticmethod
    def _service_start_command(spec: dict[str, Any]) -> list[str] | None:
        """Map Compose entrypoint/command directly to Create.Entrypoint.

        This is not a hold/bootstrap wrapper or a process supervisor: when
        Compose supplied a start command, the platform starts that exact
        command. The common list forms preserve their argv boundaries.
        """
        runtime = spec.get("runtime")
        if isinstance(runtime, dict):
            argv = runtime.get("start_argv")
            if (
                not isinstance(argv, list)
                or not argv
                or not all(isinstance(item, str) for item in argv)
            ):
                raise RuntimeError(
                    "Bundle runtime.start_argv must be a non-empty string list"
                )
            if (
                runtime.get("start_argv_source") == "image-config.cmd"
                and len(argv) == 1
                and Path(argv[0]).name in {"bash", "sh"}
            ):
                # Interactive task images commonly retain a shell-only default
                # CMD. OpenSandbox is already held by ``sleep infinity`` and
                # Harbor launches task commands through execd, so releasing
                # that shell without a TTY would only make it exit immediately.
                return None
            return list(argv)

        entrypoint = spec.get("entrypoint")
        command = spec.get("command")
        if entrypoint is None and command is None:
            return None
        if entrypoint is None:
            if isinstance(command, list) and all(
                isinstance(item, str) for item in command
            ):
                return list(command)
            if isinstance(command, str):
                return ["sh", "-c", command]
            raise RuntimeError("Compose command must be a string or string list")
        if isinstance(entrypoint, str):
            base = ["sh", "-c", entrypoint]
            if command is not None:
                raise RuntimeError(
                    "string Compose entrypoint with command has no lossless "
                    "OpenSandbox argv mapping"
                )
            return base
        if not isinstance(entrypoint, list) or not all(
            isinstance(item, str) for item in entrypoint
        ):
            raise RuntimeError("Compose entrypoint must be a string or string list")
        if command is None:
            return list(entrypoint)
        if isinstance(command, list) and all(isinstance(item, str) for item in command):
            return [*entrypoint, *command]
        if isinstance(command, str):
            return [*entrypoint, command]
        raise RuntimeError("Compose command must be a string or string list")

    @staticmethod
    def _hosts_update_command(entries: list[str]) -> str:
        block = "\n".join(entries)
        encoded = base64.b64encode(block.encode("utf-8")).decode("ascii")
        return (
            "tmp=$(mktemp); "
            f"printf %s {shlex.quote(encoded)} | base64 -d >$tmp; "
            f"sed '/^{HOSTS_BLOCK_BEGIN}$/,/^{HOSTS_BLOCK_END}$/d' /etc/hosts >$tmp.hosts; "
            f"{{ cat $tmp.hosts; printf '{HOSTS_BLOCK_BEGIN}\\n'; cat $tmp; printf '\\n{HOSTS_BLOCK_END}\\n'; }} >/etc/hosts; "
            "rm -f $tmp $tmp.hosts"
        )

    def _service_entrypoint(self, runtime: ServiceRuntime) -> list[str] | None:
        # YiCloud only schedules custom-image Sandboxes reliably with this
        # neutral long-lived entrypoint. Start the Bundle's real command over
        # execd after every service reaches Running and aliases are wired.
        return ["sleep", "infinity"]

    def _make_service_sandbox_name(self, service: str) -> str:
        base = f"{self._make_sandbox_name()}-{service}".lower()
        return re.sub(r"[^a-z0-9-]+", "-", base).strip("-")[:63].rstrip("-")

    async def _delete_service(self, runtime: ServiceRuntime) -> None:
        """Delete one composite service and invalidate its runtime connection."""
        sandbox_id = runtime.sandbox_id
        try:
            if not sandbox_id:
                runtime.state = "DELETED"
                return
            self.logger.info(
                "YiCloud composite service cleanup requested service=%s sandbox_id=%s",
                runtime.name,
                sandbox_id,
            )
            deleted = await self._delete_single_sandbox(sandbox_id)
            runtime.state = "DELETED" if deleted else "DELETE_UNCONFIRMED"
            if deleted:
                runtime.sandbox_id = ""
                self.logger.info(
                    "YiCloud composite service cleanup confirmed service=%s sandbox_id=%s",
                    runtime.name,
                    sandbox_id,
                )
            else:
                self.logger.warning(
                    "YiCloud composite service deletion was not confirmed within %ss: "
                    "service=%s sandbox_id=%s",
                    self._cleanup_wait_sec,
                    runtime.name,
                    sandbox_id,
                )
        finally:
            runtime.command_url = ""
            runtime.access_token = ""
            runtime.internal_address = ""
            if runtime.name == self._main_service:
                self._detach_sandbox()

    async def _delete_service_group(self) -> None:
        runtimes = [
            runtime for runtime in self._services.values() if runtime.sandbox_id
        ]
        try:
            sandbox_ids = {runtime.sandbox_id for runtime in runtimes}
            if len(sandbox_ids) == 1:
                deleted = await self._delete_single_sandbox(next(iter(sandbox_ids)))
            else:
                deleted = await self._batch_delete_sandboxes(sandbox_ids)
            for runtime in runtimes:
                runtime.state = "DELETED" if deleted else "DELETE_UNCONFIRMED"
            if not deleted:
                self.logger.warning(
                    "YiCloud composite Sandbox deletion was not confirmed within %ss: %s",
                    self._cleanup_wait_sec,
                    [runtime.sandbox_id for runtime in runtimes],
                )
        finally:
            self._detach_sandbox()

    async def _run_service_command(
        self,
        runtime: ServiceRuntime,
        command: str,
        *,
        cwd: str = "/",
        timeout_sec: int = 60,
    ) -> ExecResult:
        if not runtime.command_url or not runtime.access_token:
            raise RuntimeError(f"OpenSandbox service {runtime.name!r} is not running")
        wrapped = (
            "set +e\n(\n" + command + "\n)\nharbor_rc=$?\n"
            f'printf \'\\n{EXIT_MARKER}%s\\n\' "$harbor_rc"\nexit "$harbor_rc"\n'
        )
        body = json.dumps(
            {
                "command": wrapped,
                "background": False,
                "timeout": timeout_sec * 1000,
                "cwd": cwd,
            },
            separators=(",", ":"),
        )
        now = time.localtime()
        headers = {
            "X-OGW-PUBLIC-KEY": self._base_client.crede.public_key,
            "X-OGW-TICK": str(int(time.mktime(now) * 1000)),
            "X-OGW-SIGN": self._base_client.crede.sign(
                now, urlsplit(runtime.command_url).query, body
            ),
            "X-Sandbox-Access-Token": runtime.access_token,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

        def run() -> ExecResult:
            session = requests.Session()
            session.trust_env = False
            response = session.post(
                runtime.command_url,
                headers=headers,
                data=body,
                timeout=timeout_sec + 60,
            )
            response.raise_for_status()
            events = _parse_sse(response.text)
            stdout = "".join(
                str(event.get("text", ""))
                for event in events
                if event.get("type") == "stdout"
            )
            stderr = "".join(
                str(event.get("text", ""))
                for event in events
                if event.get("type") == "stderr"
            )
            matches = re.findall(rf"{re.escape(EXIT_MARKER)}(\d+)", stdout)
            return ExecResult(
                stdout=re.sub(rf"\n?{re.escape(EXIT_MARKER)}\d+\n?", "", stdout),
                stderr=stderr,
                return_code=int(matches[-1]) if matches else 1,
            )

        return await asyncio.to_thread(run)

    async def _wire_service_aliases(
        self, runtimes: list[ServiceRuntime] | None = None
    ) -> None:
        entries: list[str] = []
        for target in self._services.values():
            aliases = [target.name, *(target.spec.get("aliases") or [])]
            aliases = list(dict.fromkeys(str(item) for item in aliases if str(item)))
            if not target.internal_address:
                continue
            entries.append(f"{target.internal_address} {' '.join(aliases)}")
        command = self._hosts_update_command(entries)
        targets = runtimes if runtimes is not None else list(self._services.values())
        for runtime in targets:
            result = await self._run_service_command(runtime, command, timeout_sec=60)
            if result.return_code != 0:
                raise RuntimeError(
                    f"failed to inject service aliases into {runtime.name!r}: {result.stderr or result.stdout}"
                )

    async def _release_service_entrypoint(self, runtime: ServiceRuntime) -> None:
        start_command = self._service_start_command(runtime.spec)
        if start_command is None:
            return
        contract = runtime.spec.get("runtime") or {}
        workdir = contract.get("workdir") if isinstance(contract, dict) else None
        if workdir is not None and (
            not isinstance(workdir, str) or not workdir.startswith("/")
        ):
            raise RuntimeError(
                f"OpenSandbox service {runtime.name!r} has invalid runtime workdir"
            )
        service_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", runtime.name)
        log_path = shlex.quote(f"/tmp/harbor-service-{service_name}.log")
        pid_path = shlex.quote(f"/tmp/harbor-service-{service_name}.pid")
        argv = " ".join(shlex.quote(item) for item in start_command)
        result = await self._run_service_command(
            runtime,
            # The wrapper's exit code comes from the liveness check, not the
            # backgrounded service, so an instantly-crashing start command
            # still surfaces here instead of leaving the sandbox Running.
            f"nohup {argv} >{log_path} 2>&1 </dev/null & pid=$!; "
            f"echo $pid >{pid_path}; "
            "sleep 1; "
            f"if ! kill -0 $pid 2>/dev/null; then "
            f"tail -n 50 {log_path} >&2; exit 1; fi",
            cwd=workdir or "/",
            timeout_sec=60,
        )
        if result.return_code != 0:
            raise RuntimeError(
                f"failed to start OpenSandbox service {runtime.name!r}: "
                f"{result.stderr or result.stdout}"
            )

    @staticmethod
    def _service_healthcheck_command(runtime: ServiceRuntime) -> str | None:
        contract = runtime.spec.get("runtime") or {}
        readiness = contract.get("readiness") if isinstance(contract, dict) else None
        if not isinstance(readiness, dict) or readiness.get("type") != "healthcheck":
            return None
        healthcheck = readiness.get("healthcheck")
        if not isinstance(healthcheck, dict):
            raise TypeError(
                f"OpenSandbox healthcheck readiness is invalid for {runtime.name!r}"
            )
        test = healthcheck.get("test")
        if isinstance(test, list):
            if test and test[0] == "CMD":
                return " ".join(shlex.quote(str(item)) for item in test[1:])
            return " ".join(str(item) for item in test[1:])
        if isinstance(test, str):
            return test
        return None

    def _service_start_order(self) -> list[ServiceRuntime]:
        ordered: list[ServiceRuntime] = []
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(name: str) -> None:
            if name in visited:
                return
            if name in visiting:
                raise RuntimeError(
                    f"OpenSandbox service dependencies contain a cycle at {name!r}"
                )
            runtime = self._services.get(name)
            if runtime is None:
                raise RuntimeError(
                    f"OpenSandbox service dependency is missing: {name!r}"
                )
            dependencies = runtime.spec.get("depends_on") or {}
            if not isinstance(dependencies, dict):
                raise TypeError(f"OpenSandbox depends_on is invalid for {name!r}")
            visiting.add(name)
            for dependency_name, config in dependencies.items():
                if not isinstance(config, dict):
                    raise TypeError(
                        "OpenSandbox dependency config is invalid: "
                        f"{name!r} -> {dependency_name!r}"
                    )
                condition = str(config.get("condition") or "service_started")
                if condition not in {"service_started", "service_healthy"}:
                    raise RuntimeError(
                        f"OpenSandbox dependency condition is unsupported: "
                        f"{name!r} -> {dependency_name!r} uses {condition!r}"
                    )
                if not config.get("required", True):
                    raise RuntimeError(
                        f"OpenSandbox optional service dependency is unsupported: "
                        f"{name!r} -> {dependency_name!r}"
                    )
                dependency = self._services.get(str(dependency_name))
                if dependency is None:
                    raise RuntimeError(
                        f"OpenSandbox service dependency is missing: "
                        f"{name!r} -> {dependency_name!r}"
                    )
                if (
                    condition == "service_healthy"
                    and self._service_healthcheck_command(dependency) is None
                ):
                    raise RuntimeError(
                        f"OpenSandbox service_healthy dependency has no healthcheck: "
                        f"{name!r} -> {dependency_name!r}"
                    )
                visit(str(dependency_name))
            visiting.remove(name)
            visited.add(name)
            ordered.append(runtime)

        for service_name in self._services:
            visit(service_name)
        return ordered

    async def _wait_service_healthcheck(
        self, runtime: ServiceRuntime, deadline: float
    ) -> None:
        command = self._service_healthcheck_command(runtime)
        if command is None:
            return
        last_error = ""
        while time.monotonic() < deadline:
            result = await self._run_service_command(
                runtime,
                command,
                timeout_sec=60,
            )
            if result.return_code == 0:
                return
            last_error = result.stderr or result.stdout
            now = time.monotonic()
            if now < deadline:
                await asyncio.sleep(min(2, deadline - now))
        raise RuntimeError(
            f"OpenSandbox healthcheck failed for {runtime.name!r} "
            f"within {self._ready_timeout_sec}s: {last_error}"
        )

    async def _wait_bundle_ready(self, already_healthy: set[str] | None = None) -> None:
        healthy = already_healthy or set()
        healthcheck_deadline = time.monotonic() + self._ready_timeout_sec
        for runtime in self._services.values():
            if runtime.name in healthy:
                continue
            await self._wait_service_healthcheck(runtime, healthcheck_deadline)
        # The runtime contract distinguishes address-discovery ports from
        # readiness.  Only an explicit TCP readiness declaration is probed;
        # merely exposing a port does not promise that the service is ready.
        main = self._services[self._main_service]
        deadline = time.monotonic() + self._ready_timeout_sec
        for runtime in self._services.values():
            contract = runtime.spec.get("runtime") or {}
            readiness = (
                contract.get("readiness") if isinstance(contract, dict) else None
            )
            if not isinstance(readiness, dict) or readiness.get("type") != "tcp":
                continue
            port = readiness.get("port")
            if not isinstance(port, int) or port not in self._service_ports(
                runtime.spec
            ):
                raise RuntimeError(
                    f"OpenSandbox TCP readiness is invalid for {runtime.name!r}"
                )
            probe = (
                'if command -v nc >/dev/null 2>&1; then nc -z -w 2 "$@"; '
                "elif command -v bash >/dev/null 2>&1; then timeout 3 bash -c "
                "'cat < /dev/null > /dev/tcp/$1/$2' _ \"$@\"; "
                "elif command -v python3 >/dev/null 2>&1; then python3 -c "
                "'import socket,sys;s=socket.create_connection((sys.argv[1],int(sys.argv[2])),2);s.close()' \"$@\"; "
                "else exit 69; fi"
            )
            while True:
                result = await self._run_service_command(
                    main,
                    f"sh -c {shlex.quote(probe)} _ {shlex.quote(runtime.name)} {port}",
                    timeout_sec=15,
                )
                if result.return_code == 0:
                    break
                if result.return_code == 69:
                    raise RuntimeError(
                        "OpenSandbox cannot probe required service port: "
                        "main has none of nc, bash, or python3"
                    )
                if time.monotonic() >= deadline:
                    raise RuntimeError(
                        f"OpenSandbox required port was not ready: "
                        f"service={runtime.name} port={port}; "
                        f"stderr={result.stderr or result.stdout}"
                    )
                await asyncio.sleep(2)

    async def _start_composite(self) -> None:
        sandbox = self._sandbox_service
        assert sandbox is not None
        start_order = self._service_start_order()
        healthy: set[str] = set()
        for runtime in start_order:
            runtime.sandbox_name = self._make_service_sandbox_name(runtime.name)
            image_ref = self._service_image_source_ref(runtime)
            request: dict[str, Any] = {
                "ProjectName": self._project_name,
                "EnvironmentId": self._environment_id,
                "Name": runtime.sandbox_name,
                "Image": self._create_image_input(sandbox, image_ref),
                "Env": {
                    **self._persistent_env,
                    **{
                        str(key): str(value)
                        for key, value in (
                            runtime.spec.get("environment") or {}
                        ).items()
                        if value is not None
                    },
                },
                "Resources": sandbox.models.CreateSandboxReqResources(
                    Cpu=self._request_cpu, Memory=self._request_memory
                ),
                "LifecycleMinutes": self._lifecycle_minutes,
                "RequestTimeoutSeconds": self._request_timeout_sec,
            }
            start_command = self._service_entrypoint(runtime)
            if start_command is not None:
                request["Entrypoint"] = start_command
            ports = self._service_ports(runtime.spec)
            if ports:
                request["Ports"] = [
                    sandbox.models.CreateSandboxReqPort(
                        ContainerPort=port,
                        Name=f"svc-{port}",
                        Purpose=f"Harbor {runtime.name}",
                    )
                    for port in ports
                ]
            created = await self._retry_control_plane_auth(
                f"create composite service {runtime.name}",
                sandbox.create_sandbox,
                None,
                sandbox.models.CreateSandboxReq(**request),
            )
            runtime.sandbox_id = str(getattr(created, "Id", "") or "")
            if not runtime.sandbox_id:
                raise RuntimeError(
                    f"OpenSandbox create returned no sandbox ID for service {runtime.name!r}"
                )
            current = await self._wait_service_running(runtime, created)
            _validate_sandbox_binding(
                current, self._environment_id, self._service_image_ref(runtime)
            )
            runtime.access_token = _access_token_of(created) or _access_token_of(
                current
            )
            runtime.command_url = _command_url_of(current)
            runtime.internal_address = _internal_address_of(current)
            runtime.state = "WIRING"
            if not runtime.access_token:
                raise RuntimeError(
                    f"OpenSandbox service {runtime.name!r} returned no access token"
                )
            await self._wait_until_execd_ready(runtime)
        await self._wire_service_aliases()
        for runtime in start_order:
            dependencies = runtime.spec.get("depends_on") or {}
            for dependency_name, config in dependencies.items():
                if (
                    config.get("condition") == "service_healthy"
                    and dependency_name not in healthy
                ):
                    await self._wait_service_healthcheck(
                        self._services[dependency_name],
                        time.monotonic() + self._ready_timeout_sec,
                    )
                    healthy.add(dependency_name)
            await self._release_service_entrypoint(runtime)
            runtime.state = "STARTING"
        await self._wait_bundle_ready(healthy)
        main = self._services[self._main_service]
        self._sandbox_id, self._sandbox_name = main.sandbox_id, main.sandbox_name
        self._access_token, self._command_url = main.access_token, main.command_url
        for runtime in self._services.values():
            runtime.state = "READY"

    async def service_exec(
        self,
        service: str | None,
        command: str,
        cwd: str | None = None,
        timeout_sec: int | None = None,
    ) -> ExecResult:
        """Execute on a named sidecar; None and main retain Harbor routing."""
        name = service or self._main_service
        if not self._services or name == self._main_service:
            return await self.exec(command, cwd=cwd, timeout_sec=timeout_sec)
        runtime = self._services.get(name)
        if runtime is None:
            raise ValueError(f"unknown OpenSandbox service: {name!r}")
        return await self._run_service_command(
            runtime, command, cwd=cwd or "/", timeout_sec=timeout_sec or 3600
        )

    async def stop_service(self, service: str) -> None:
        runtime = self._services.get(service)
        if runtime is None:
            raise ValueError(f"unknown OpenSandbox service: {service!r}")
        await self._delete_service(runtime)

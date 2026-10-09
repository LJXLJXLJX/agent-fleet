"""YiCloud client: extracted from the legacy Harbor provider without behavior changes."""

from __future__ import annotations

import asyncio
import os
import time
from importlib.metadata import PackageNotFoundError, version
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests

EXPECTED_YICLOUD_SDK_VERSION = "0.4.1"

TERMINAL_FAILURE_STATES = {
    "failed",
    "error",
    "stopped",
    "terminated",
    "killed",
    "expired",
}


SANDBOX_STATUS_REQUEST_ATTEMPTS = 5


CONTROL_PLANE_AUTH_ATTEMPTS = 5


class YiCloudSandboxReadyTimeoutError(RuntimeError):
    """The YiCloud control plane did not schedule a Sandbox before our deadline."""


def _require_yicloud_sdk() -> None:
    try:
        installed = version("yicloud-sdk-python")
    except PackageNotFoundError as exc:
        raise RuntimeError("yicloud-sdk-python is not installed") from exc
    if installed != EXPECTED_YICLOUD_SDK_VERSION:
        raise RuntimeError(
            f"expected yicloud-sdk-python=={EXPECTED_YICLOUD_SDK_VERSION}, "
            f"found {installed}"
        )


def _bypass_local_proxy_for_api_host() -> None:
    api_host = os.environ.get("YICLOUD_API_HOST", "https://gate.yicloud.com.cn")
    hostname = urlsplit(api_host).hostname
    if not hostname:
        raise ValueError(f"unable to parse YICLOUD_API_HOST hostname: {api_host!r}")
    for key in ("NO_PROXY", "no_proxy"):
        entries = [item.strip() for item in os.environ.get(key, "").split(",")]
        if hostname not in entries:
            os.environ[key] = ",".join(item for item in [*entries, hostname] if item)


def _state_of(data: Any) -> str:
    status = getattr(data, "Status", None)
    state = getattr(status, "State", "") or getattr(data, "RunState", "")
    # yicloud-sdk-python 0.3.1 declares lowercase state values, while older
    # gateways returned title-case values. Normalize both representations so a
    # running sandbox is not mistaken for a pending one until ready timeout.
    return str(state or "").strip().lower()


def _status_reason_of(data: Any) -> str:
    status = getattr(data, "Status", None)
    return str(getattr(status, "Reason", "") or "").strip()


def _yicloud_image_ref(ref: str) -> str:
    """Translate a full OCI reference to YiCloud's ``Image.Ref`` format.

    The Bundle retains the complete OCI digest reference used for Registry
    publication and verification.  YiCloud's CreateSandbox SDK explicitly
    requires ``Image.Ref`` without a registry host, so this provider boundary
    keeps the immutable repository-and-digest portion only.
    """
    value = str(ref or "").strip()
    repository, marker, digest = value.partition("@")
    first, separator, remainder = repository.partition("/")
    if separator and ("." in first or ":" in first or first == "localhost"):
        return f"{remainder}{marker}{digest}" if marker else remainder
    return value


def _access_token_of(data: Any) -> str:
    endpoints = getattr(data, "Endpoints", None)
    return str(
        getattr(data, "AccessToken", "") or getattr(endpoints, "AccessToken", "") or ""
    )


def _image_ref_of(data: Any) -> str:
    image = getattr(data, "Image", None)
    return str(getattr(image, "Ref", "") or getattr(image, "Uri", "") or "").strip()


def _image_ref_aliases(image_ref: str) -> set[str]:
    value = str(image_ref or "").strip()
    if not value:
        return set()
    aliases = {value}
    if "://" in value:
        path = urlsplit(value).path.lstrip("/")
        if path:
            aliases.add(path)
    else:
        normalized = _yicloud_image_ref(value)
        if normalized != value:
            aliases.add(normalized)
    return aliases


def _is_external_image_ref(image_ref: str) -> bool:
    value = str(image_ref or "").strip()
    return "://" in value or _yicloud_image_ref(value) != value


def _host_routable_proxy_url(proxy_url: str) -> str:
    origin = os.environ.get("YICLOUD_SANDBOX_PROXY_ORIGIN", "").strip()
    if not origin:
        return proxy_url
    source = urlsplit(proxy_url)
    target = urlsplit(origin)
    if target.scheme not in {"http", "https"} or not target.netloc:
        raise ValueError("YICLOUD_SANDBOX_PROXY_ORIGIN must be an HTTP(S) origin")
    path = f"{target.path.rstrip('/')}/{source.path.lstrip('/')}"
    return urlunsplit((target.scheme, target.netloc, path, source.query, ""))


def _environment_id_by_exact_name(
    sandbox: Any, project_name: str, environment_name: str
) -> str:
    listed = sandbox.list_sandbox_environments(
        None,
        sandbox.models.ListSandboxEnvironmentsReq(
            ProjectName=project_name,
            Keyword=environment_name,
            Limit=100,
            Offset=0,
        ),
    )
    matches = [
        item
        for item in (getattr(listed, "Items", None) or [])
        if str(getattr(item, "Name", "") or "").strip() == environment_name
    ]
    if not matches:
        raise RuntimeError(
            f"YiCloud Sandbox environment not found by exact name: {environment_name!r}"
        )
    if len(matches) != 1:
        ids = sorted(str(getattr(item, "Id", "") or "") for item in matches)
        raise RuntimeError(
            f"YiCloud Sandbox environment name is ambiguous: "
            f"{environment_name!r}; ids={ids}"
        )
    environment_id = str(getattr(matches[0], "Id", "") or "").strip()
    if not environment_id:
        raise RuntimeError(
            f"YiCloud Sandbox environment has no ID: {environment_name!r}"
        )
    return environment_id


def _validate_sandbox_binding(
    data: Any, expected_environment_id: str, expected_image_ref: str
) -> None:
    actual_environment_id = str(getattr(data, "EnvironmentId", "") or "").strip()
    actual_image_ref = _image_ref_of(data)
    if actual_environment_id != expected_environment_id:
        raise RuntimeError(
            "YiCloud Sandbox environment binding mismatch: "
            f"expected={expected_environment_id!r}, "
            f"actual={actual_environment_id!r}"
        )
    aliases_match = _image_ref_aliases(actual_image_ref).intersection(
        _image_ref_aliases(expected_image_ref)
    )
    registry_mismatch = (
        actual_image_ref != expected_image_ref
        and _is_external_image_ref(actual_image_ref)
        and _is_external_image_ref(expected_image_ref)
    )
    if registry_mismatch or not aliases_match:
        raise RuntimeError(
            "YiCloud Sandbox image binding mismatch: "
            f"expected={expected_image_ref!r}, actual={actual_image_ref!r}"
        )


def _command_url_of(data: Any) -> str:
    endpoints = getattr(data, "Endpoints", None)
    for endpoint in (getattr(endpoints, "Endpoints", None) or {}).values():
        proxy_url = str(getattr(endpoint, "ProxyUrl", "") or "")
        if not proxy_url.startswith(("http://", "https://")):
            continue
        proxy_url = _host_routable_proxy_url(proxy_url)
        parsed = urlsplit(proxy_url)
        path = parsed.path.rstrip("/")
        path = path.removesuffix("/ping")
        path += "/command"
        return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))
    raise RuntimeError("running YiCloud Sandbox returned no execd proxy endpoint")


def _internal_address_of(data: Any) -> str:
    endpoints = getattr(data, "Endpoints", None)
    for endpoint in (getattr(endpoints, "Endpoints", None) or {}).values():
        value = str(getattr(endpoint, "InternalUrl", "") or "")
        if not value:
            continue
        parsed = urlsplit(value if "://" in value else f"tcp://{value}")
        if parsed.hostname:
            return parsed.hostname
    raise RuntimeError("running YiCloud Sandbox returned no internal endpoint address")


class YiCloudClientMixin:
    """Internal client methods sharing the adapter instance state."""

    def _initialize_service(self) -> None:
        _require_yicloud_sdk()
        _bypass_local_proxy_for_api_host()
        if not self._project_name:
            raise RuntimeError("YICLOUD_PROJECT_NAME is unset")

        from yicloud import base
        from yicloud.services import sandbox

        self._base_client = base.new_client()
        sandbox.use_client(self._base_client)
        self._sandbox_service = sandbox

    def _resolve_environment_id(self) -> str:
        sandbox = self._sandbox_service
        if sandbox is None:
            raise RuntimeError("YiCloud Sandbox service is not initialized")
        if not self._environment_id and not self._environment_name:
            raise RuntimeError(
                "YiCloud Sandbox environment is unset; configure "
                "YICLOUD_SANDBOX_ENVIRONMENT_ID or "
                "YICLOUD_SANDBOX_ENVIRONMENT_NAME"
            )
        if self._environment_name:
            resolved = _environment_id_by_exact_name(
                sandbox, self._project_name, self._environment_name
            )
            if self._environment_id and self._environment_id != resolved:
                raise RuntimeError(
                    "YiCloud Sandbox environment ID/name mismatch: "
                    f"id={self._environment_id!r}, "
                    f"name={self._environment_name!r}, "
                    f"resolved_id={resolved!r}"
                )
            self._environment_id = resolved
        else:
            environment = sandbox.get_sandbox_environment(
                None,
                sandbox.models.GetSandboxEnvironmentReq(
                    ProjectName=self._project_name,
                    EnvironmentId=self._environment_id,
                ),
            )
            actual_id = str(getattr(environment, "Id", "") or "").strip()
            if actual_id != self._environment_id:
                raise RuntimeError(
                    "YiCloud Sandbox environment lookup mismatch: "
                    f"expected={self._environment_id!r}, actual={actual_id!r}"
                )
            self._environment_name = str(getattr(environment, "Name", "") or "").strip()
        return self._environment_id

    async def _retry_control_plane_auth(
        self,
        operation: str,
        function: Any,
        *args: Any,
    ) -> Any:
        for attempt in range(1, CONTROL_PLANE_AUTH_ATTEMPTS + 1):
            try:
                return await asyncio.to_thread(function, *args)
            except Exception as error:
                if (
                    getattr(error, "code", None) != 101
                    or attempt == CONTROL_PLANE_AUTH_ATTEMPTS
                ):
                    raise
                delay = min(2 ** (attempt - 1), 5)
                self.logger.warning(
                    "YiCloud control-plane authentication failed "
                    "transiently during %s; retrying in %ss "
                    "(attempt %s/%s): %s",
                    operation,
                    delay,
                    attempt,
                    CONTROL_PLANE_AUTH_ATTEMPTS,
                    error,
                )
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    def _ping_execd_sync(
        self,
        command_url: str | None = None,
        access_token: str | None = None,
    ) -> None:
        url = self._execd_url("/ping", command_url=command_url)
        session = requests.Session()
        session.trust_env = False
        response = session.get(
            url,
            headers=self._signed_headers(
                "",
                url=url,
                access_token=access_token,
                accept="application/json",
            ),
            timeout=(10, 20),
        )
        response.raise_for_status()

    @staticmethod
    def _create_image_input(sandbox: Any, source_ref: str) -> Any:
        """Use URI for a fully-qualified OCI reference to pin its registry."""
        normalized = _yicloud_image_ref(source_ref)
        if normalized != source_ref:
            return sandbox.models.CreateSandboxReqImageInput(Uri=source_ref)
        return sandbox.models.CreateSandboxReqImageInput(Ref=source_ref)

    def _signed_headers(
        self,
        body: str | bytes,
        *,
        url: str | None = None,
        access_token: str | None = None,
        content_type: str = "application/json",
        accept: str = "text/event-stream",
    ) -> dict[str, str]:
        if self._base_client is None:
            raise RuntimeError("YiCloud client is not initialized")
        now = time.localtime()
        timestamp = str(int(time.mktime(now) * 1000))
        query = urlsplit(url or self._command_url).query
        signature = self._base_client.crede.sign(now, query, body)
        return {
            "X-OGW-PUBLIC-KEY": self._base_client.crede.public_key,
            "X-OGW-TICK": timestamp,
            "X-OGW-SIGN": signature,
            "X-Sandbox-Access-Token": (
                self._access_token if access_token is None else access_token
            ),
            "Content-Type": content_type,
            "Accept": accept,
        }

    def _execd_url(self, suffix: str, *, command_url: str | None = None) -> str:
        source_url = self._command_url if command_url is None else command_url
        if not source_url:
            raise RuntimeError("YiCloud Sandbox is not running")
        parsed = urlsplit(source_url)
        base_path = parsed.path.rsplit("/", 1)[0]
        return urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                f"{base_path}/{suffix.lstrip('/')}",
                parsed.query,
                "",
            )
        )

"""Access the DSec Sandbox Controller only through its public v1alpha1 API.

Infrastructure implementation details remain behind the Controller. The caller
supplies stable request IDs for every mutating request; transport failure does
not authorize a retry under a new resource identity.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx

from .runtime_preflight import parse_controller_headers

API_VERSION = "sandbox-lifecycle/v1alpha1"
MAX_JSON_RESPONSE_BYTES = 16 << 20
MAX_OUTPUT_READ_BYTES = 1 << 20


class DsecControllerError(RuntimeError):
    """Stable Controller protocol error without request bodies or execution output."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(f"DSec Controller request failed: {code}: {message}")
        self.code = code
        self.retryable = retryable


class DsecControllerClient:
    """Own the Controller origin, bounded HTTP deadlines, and response limits."""

    def __init__(
        self,
        endpoint: str,
        *,
        allow_insecure_http: bool = False,
        request_timeout_sec: int = 15,
        headers: str | Mapping[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError(
                "DSEC_SANDBOX_CONTROLLER_ENDPOINT must be an HTTP(S) origin"
            )
        if parsed.scheme == "http" and not allow_insecure_http:
            raise ValueError("plain HTTP requires DSEC_SANDBOX_ALLOW_INSECURE_HTTP=1")
        if not 1 <= request_timeout_sec <= 60:
            raise ValueError("request_timeout_sec must be between 1 and 60")
        self.endpoint = f"{parsed.scheme}://{parsed.netloc}"
        self.request_timeout_sec = request_timeout_sec
        self.headers = parse_controller_headers(headers)
        self.client = client or httpx.AsyncClient(
            follow_redirects=False,
            timeout=httpx.Timeout(request_timeout_sec),
            trust_env=False,
            headers=self.headers,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )

    async def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        raw: bool = False,
    ) -> Any:
        async with self.client.stream(
            method,
            self.endpoint + path,
            json=body,
            headers=self.headers,
        ) as response:
            limit = (
                MAX_OUTPUT_READ_BYTES
                if raw and response.is_success
                else MAX_JSON_RESPONSE_BYTES
            )
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes(64 << 10):
                size += len(chunk)
                if size > limit:
                    raise RuntimeError(
                        "DSec Controller response exceeds protocol limit"
                    )
                chunks.append(chunk)
            payload = b"".join(chunks)
            if raw and response.is_success:
                return payload
            try:
                envelope = json.loads(payload)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise RuntimeError("DSec Controller returned invalid JSON") from error
            if (
                not isinstance(envelope, dict)
                or envelope.get("api_version") != API_VERSION
            ):
                raise RuntimeError(
                    "DSec Controller returned an unsupported API version"
                )
            detail = envelope.get("error")
            if not response.is_success or detail is not None:
                if not isinstance(detail, dict):
                    raise RuntimeError(
                        f"DSec Controller HTTP request failed: {response.status_code}"
                    )
                raise DsecControllerError(
                    str(detail.get("code") or response.status_code),
                    str(detail.get("message") or "request rejected"),
                    retryable=detail.get("retryable") is True,
                )
            result = envelope.get("result")
            if not isinstance(result, dict):
                raise TypeError("DSec Controller response is missing result")
            return result

    async def close(self) -> None:
        """Release this trial's asynchronous Controller connection pool."""
        await self.client.aclose()

    async def create_sandbox(self, spec: dict[str, Any]) -> dict[str, Any]:
        return (await self.request("POST", "/v1alpha1/sandboxes", spec))["sandbox"]

    async def sandbox_status(self, sandbox_id: str) -> dict[str, Any]:
        return (await self.request("GET", f"/v1alpha1/sandboxes/{sandbox_id}"))[
            "sandbox"
        ]

    async def close_sandbox(self, sandbox_id: str) -> dict[str, Any]:
        return (await self.request("POST", f"/v1alpha1/sandboxes/{sandbox_id}/close"))[
            "sandbox"
        ]

    async def create_execution(
        self, sandbox_id: str, spec: dict[str, Any]
    ) -> dict[str, Any]:
        return (
            await self.request(
                "POST", f"/v1alpha1/sandboxes/{sandbox_id}/executions", spec
            )
        )["execution"]

    async def execution_status(
        self, sandbox_id: str, execution_id: str
    ) -> dict[str, Any]:
        return (
            await self.request(
                "GET",
                f"/v1alpha1/sandboxes/{sandbox_id}/executions/{execution_id}",
            )
        )["execution"]

    async def execution_output(
        self,
        sandbox_id: str,
        execution_id: str,
        stream: str,
        offset: int,
        length: int,
    ) -> bytes:
        query = urlencode({"offset": offset, "length": length})
        return await self.request(
            "GET",
            f"/v1alpha1/sandboxes/{sandbox_id}/executions/{execution_id}"
            f"/output/{stream}?{query}",
            raw=True,
        )

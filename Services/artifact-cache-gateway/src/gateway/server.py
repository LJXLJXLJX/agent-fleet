from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from ..core.config import ConfigError
from ..core.ecosystems import handler_for_kind
from ..core.ecosystems.cargo import rewrite_rustup_init
from ..storage.base import StorageError
from ..ui.webui import WebUIAsset, webui_asset
from .engine import CacheResult, Gateway
from .fetcher import FetchError
from .git_http import (
    _MAX_GIT_REQUEST_BYTES,
    GitRequestBodyError,
    _read_git_request_exact,
    decode_git_request_body,
    read_chunked_git_request_body,
)
from .rewrites import rewrite_index

_MAX_REWRITTEN_HTML_BYTES = 16 * 1024 * 1024
_MAX_REWRITTEN_RUSTUP_INIT_BYTES = 1024 * 1024
from .services.git import GitMirrorError, GitMirrorStore, read_cgi_headers
from .status import status_document

_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


_SAFE_HOST = re.compile(r"^[A-Za-z0-9.:[\]-]{1,255}$")
_GIT_ROUTE = re.compile(
    r"^/v1/git/github/(?P<owner>[^/]+)/(?P<repository>[^/]+)(?P<suffix>/.*)?$"
)


def parse_range(value: str, size: int) -> tuple[int, int]:
    match = _RANGE.fullmatch(value.strip())
    if not match or size <= 0:
        raise ValueError("unsupported range")
    first, last = match.groups()
    if not first:
        suffix = int(last)
        if suffix <= 0:
            raise ValueError("invalid suffix range")
        return max(0, size - suffix), size - 1
    start = int(first)
    end = int(last) if last else size - 1
    if start >= size or end < start:
        raise ValueError("range outside object")
    return start, min(end, size - 1)


class GatewayHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        gateway: Gateway,
        git_mirror: GitMirrorStore | None = None,
    ):
        super().__init__(address, GatewayRequestHandler)
        self.gateway = gateway
        self.git_mirror = git_mirror


class GatewayRequestHandler(BaseHTTPRequestHandler):
    server: GatewayHTTPServer
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self._handle(send_body=True)

    def do_HEAD(self) -> None:
        self._handle(send_body=False)

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path.startswith("/v1/git/"):
            self._serve_git(parsed.path, parsed.query)
            return
        self._error(405, "method not allowed", True)

    def _handle(self, send_body: bool) -> None:
        parsed = urlsplit(self.path)
        if parsed.path in {"/", "/ui"}:
            self._redirect("/ui/")
            return
        asset = webui_asset(parsed.path)
        if asset is not None:
            self._webui_asset(asset, send_body)
            return
        if parsed.path == "/healthz":
            self._json(200, {"status": "ok"}, send_body)
            return
        if parsed.path == "/v1/status":
            self._json(
                200,
                status_document(self.server.gateway, self.server.git_mirror),
                send_body,
            )
            return
        if parsed.path == "/v1/objects":
            try:
                self._inventory(parsed.query, send_body)
            except ConfigError as exc:
                self._error(400, str(exc), send_body)
            except StorageError as exc:
                self._error(502, str(exc), send_body)
            return

        if parsed.path.startswith("/v1/git/"):
            if not send_body:
                self._error(405, "HEAD is not supported for Git Smart HTTP", False)
                return
            self._serve_git(parsed.path, parsed.query)
            return

        prefix = "/v1/cache/"
        if not parsed.path.startswith(prefix):
            self._error(404, "route not found", send_body)
            return
        remainder = parsed.path[len(prefix) :]
        source_name, separator, relative_path = remainder.partition("/")
        if not source_name or not separator:
            self._error(404, "source path is required", send_body)
            return
        try:
            prefer_fallback = self.headers.get(
                "X-Artifact-Cache-Prefer-Fallback", ""
            ).strip() == "1"
            decoded_source = unquote(source_name)
            if decoded_source == "apt":
                result = self.server.gateway.resolve_apt_route(
                    relative_path,
                    parsed.query,
                    prefer_fallback=prefer_fallback,
                )
            elif decoded_source == "download":
                result = self.server.gateway.resolve_download_route(
                    relative_path,
                    parsed.query,
                    prefer_fallback=prefer_fallback,
                )
            else:
                result = self.server.gateway.resolve(
                    decoded_source,
                    relative_path,
                    parsed.query,
                    prefer_fallback=prefer_fallback,
                )
            self._serve_result(result, send_body)
        except ConfigError as exc:
            self._error(400, str(exc), send_body)
        except FetchError as exc:
            self._json(
                exc.status,
                {
                    "error": str(exc),
                    "stage": exc.stage,
                    "attempts": [attempt.document() for attempt in exc.attempts],
                },
                send_body,
            )
        except StorageError as exc:
            self._error(502, str(exc), send_body)
        except Exception:
            logging.exception("unexpected request failure")
            self._error(500, "internal server error", send_body)

    def _serve_git(self, path: str, query: str) -> None:
        mirror = self.server.git_mirror
        if mirror is None:
            self._error(404, "Git mirror is not enabled", True)
            return
        match = _GIT_ROUTE.fullmatch(path)
        if match is None:
            self._error(404, "Git repository route is invalid", True)
            return
        suffix = match.group("suffix") or ""
        if "git-receive-pack" in suffix or "git-receive-pack" in query:
            self._error(403, "Git mirror is read-only", True)
            return
        request_body = b""
        chunked_request = False
        if self.command == "GET":
            if suffix != "/info/refs" or query != "service=git-upload-pack":
                self._error(404, "unsupported Git Smart HTTP request", True)
                return
            content_length = 0
        elif self.command == "POST":
            self.close_connection = True
            if suffix != "/git-upload-pack" or query:
                self._error(404, "unsupported Git Smart HTTP request", True)
                return
            length_headers = self.headers.get_all("Content-Length", [])
            transfer_headers = self.headers.get_all("Transfer-Encoding", [])
            transfer_codings = [
                coding.strip().lower()
                for value in transfer_headers
                for coding in value.split(",")
                if coding.strip()
            ]
            if length_headers and transfer_codings:
                self._error(
                    400,
                    "Git request cannot combine Content-Length and Transfer-Encoding",
                    True,
                )
                return
            if transfer_codings:
                if transfer_codings != ["chunked"]:
                    self._error(400, "unsupported Git request transfer encoding", True)
                    return
                content_length = 0
                chunked_request = True
            else:
                if len(length_headers) != 1:
                    self._error(411, "Git request requires Content-Length", True)
                    return
                try:
                    content_length = int(length_headers[0])
                except ValueError:
                    self._error(411, "Git request requires Content-Length", True)
                    return
                if not 0 <= content_length <= _MAX_GIT_REQUEST_BYTES:
                    self._error(413, "Git request body is too large", True)
                    return
        else:
            self._error(405, "method not allowed", True)
            return

        raw_repository = (
            f"{unquote(match.group('owner'))}/{unquote(match.group('repository'))}"
        )
        try:
            repository = mirror.canonical_repository(raw_repository)
        except GitMirrorError as exc:
            self._error(404, str(exc), True)
            return

        if self.command == "POST":
            try:
                raw_body = (
                    read_chunked_git_request_body(self.rfile)
                    if chunked_request
                    else _read_git_request_exact(self.rfile, content_length)
                )
            except GitRequestBodyError as exc:
                self.close_connection = True
                self._error(exc.status, str(exc), True)
                return
            try:
                request_body = decode_git_request_body(
                    raw_body, self.headers.get("Content-Encoding", "")
                )
            except GitMirrorError as exc:
                self._error(415, str(exc), True)
                return
            content_length = len(request_body)

        result = mirror.ensure(raw_repository)
        if result.state == "ERROR":
            logging.error(
                "git_mirror_fill_failed repository=%s reason=%s",
                result.repository,
                result.error,
            )
            self._error(502, result.error or "Git mirror fill failed", True)
            return

        owner, name = repository.split("/", 1)
        environment = os.environ.copy()
        for key in (
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"
        ):
            environment.pop(key, None)
        environment.update(
            {
                "GIT_PROJECT_ROOT": str(mirror.root),
                "GIT_HTTP_EXPORT_ALL": "1",
                "PATH_INFO": f"/{owner}/{name}.git{suffix}",
                "REQUEST_METHOD": self.command,
                "QUERY_STRING": query,
                "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                "CONTENT_LENGTH": str(content_length),
                "REMOTE_ADDR": self.client_address[0],
                "SERVER_PROTOCOL": self.protocol_version,
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        git_protocol = self.headers.get("Git-Protocol", "").strip()
        if git_protocol:
            if len(git_protocol) > 100 or any(
                character in git_protocol for character in "\r\n\x00"
            ):
                self._error(400, "invalid Git-Protocol header", True)
                return
            environment["HTTP_GIT_PROTOCOL"] = git_protocol
        try:
            process = subprocess.Popen(
                ["git", "http-backend"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
            )
        except OSError:
            logging.exception("Git HTTP backend is unavailable")
            self._error(500, "Git HTTP backend is unavailable", True)
            return
        response_started = False
        try:
            assert process.stdin is not None
            if request_body:
                process.stdin.write(request_body)
            process.stdin.close()
            assert process.stdout is not None
            status, headers = read_cgi_headers(process.stdout)
            self.send_response(status)
            for name_header, value in headers:
                self.send_header(name_header, value)
            self.send_header("X-Artifact-Git-Mirror", result.state)
            self.send_header("Connection", "close")
            self.end_headers()
            response_started = True
            self.close_connection = True
            while True:
                chunk = process.stdout.read(1024 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
            return_code = process.wait(timeout=60)
            if return_code:
                assert process.stderr is not None
                detail = process.stderr.read(4096).decode("utf-8", "replace").strip()
                logging.error(
                    "git_http_backend_failed repository=%s status=%s detail=%s",
                    repository,
                    return_code,
                    detail[:500],
                )
        except (BrokenPipeError, ConnectionResetError):
            process.kill()
            process.wait()
        except Exception:
            process.kill()
            process.wait()
            logging.exception("Git Smart HTTP request failed")
            if not response_started:
                self._error(502, "Git Smart HTTP request failed", True)
            elif not self.wfile.closed:
                self.close_connection = True
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()

    def _serve_result(self, result: CacheResult, send_body: bool) -> None:
        try:
            self._serve_result_body(result, send_body)
        finally:
            self.server.gateway.release_result(result)

    def _serve_result_body(self, result: CacheResult, send_body: bool) -> None:
        entry = result.entry
        is_html = entry.content_type.lower().startswith("text/html")
        # The original four npm/dart/julia/cargo is_* checks are unified into the
        # kind handler's rewrite_matches (each source has exactly one kind, so the
        # four were mutually exclusive anyway).
        handler = handler_for_kind(result.source.kind)
        is_kind_rewrite = (
            handler is not None and handler.rewrite_matches(result, entry)
        )
        is_rustup_init = result.source.name == "rustup-init"
        range_header = self.headers.get("Range")

        if entry.size == 0:
            self._send_blob_headers(result, 200, 0, -1, 0)
            return

        rewrite_limit = _MAX_REWRITTEN_HTML_BYTES
        if is_kind_rewrite:
            rewrite_limit = handler.rewrite_size_limit
        elif is_rustup_init:
            rewrite_limit = _MAX_REWRITTEN_RUSTUP_INIT_BYTES
        if (
            is_html or is_kind_rewrite or is_rustup_init
        ) and entry.size <= rewrite_limit:
            with self.server.gateway.open_result_blob(
                result, start=0, end=entry.size - 1
            ) as stream:
                content = stream.read(entry.size + 1)
            if len(content) != entry.size:
                raise StorageError("blob response length is incomplete")
            if is_html:
                content = rewrite_index(content, result)
            elif is_kind_rewrite:
                content = handler.rewrite(
                    content,
                    result,
                    gateway_origin=self._gateway_request_origin(),
                    archive_source=(
                        self.server.gateway.config.source("dart-pub-archives")
                        if result.source.kind == "dart-pub"
                        else None
                    ),
                )
            elif is_rustup_init:
                content = rewrite_rustup_init(content)
            self.send_response(200)
            self._cache_headers(result, include_etag=False)
            self.send_header("Content-Type", entry.content_type)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            if send_body:
                self.wfile.write(content)
            return

        start, end = 0, entry.size - 1
        status = 200
        if range_header:
            try:
                start, end = parse_range(range_header, entry.size)
            except (ValueError, OverflowError):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{entry.size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206

        length = max(0, end - start + 1)
        if not send_body or not length:
            self._send_blob_headers(result, status, start, end, length)
            return

        with self.server.gateway.open_result_blob(
            result, start=start, end=end
        ) as stream:
            self._send_blob_headers(result, status, start, end, length)
            remaining = length
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise StorageError("blob response length is incomplete")
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _gateway_request_origin(self) -> str:
        host = self.headers.get("Host", "").strip()
        if not _SAFE_HOST.fullmatch(host):
            address, port = self.server.server_address[:2]
            host = f"{address}:{port}"
        forwarded = self.headers.get("X-Forwarded-Proto", "").strip().lower()
        scheme = forwarded if forwarded in {"http", "https"} else "http"
        return f"{scheme}://{host}"

    def _send_blob_headers(
        self,
        result: CacheResult,
        status: int,
        start: int,
        end: int,
        length: int,
    ) -> None:
        self.send_response(status)
        self._cache_headers(result)
        self.send_header("Content-Type", result.entry.content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if status == 206:
            self.send_header(
                "Content-Range", f"bytes {start}-{end}/{result.entry.size}"
            )
        self.end_headers()

    def _cache_headers(self, result: CacheResult, include_etag: bool = True) -> None:
        self.send_header("X-Artifact-Cache", result.state)
        self.send_header("X-Content-SHA256", result.entry.digest)
        if include_etag:
            self.send_header("ETag", f'"sha256:{result.entry.digest}"')
        if result.state == "STALE":
            self.send_header("Warning", '110 - "Response is stale"')

    def _json(self, status: int, value: dict[str, object], send_body: bool) -> None:
        body = (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if send_body:
            self.wfile.write(body)

    def _inventory(self, query: str, send_body: bool) -> None:
        parameters = parse_qs(query, keep_blank_values=True)
        allowed = {"ecosystem", "source", "package", "version", "cursor", "limit"}
        unknown = set(parameters) - allowed
        if unknown:
            raise ConfigError("inventory query contains unknown fields")
        for name, values in parameters.items():
            if len(values) != 1:
                raise ConfigError(f"inventory query field is duplicated: {name}")

        def optional(name: str) -> str | None:
            values = parameters.get(name)
            if not values:
                return None
            value = values[0]
            if not value:
                raise ConfigError(f"inventory query field cannot be empty: {name}")
            return value

        raw_limit = optional("limit")
        try:
            limit = int(raw_limit) if raw_limit is not None else 50
        except ValueError as exc:
            raise ConfigError("inventory limit must be an integer") from exc
        document = self.server.gateway.inventory_document(
            ecosystem=optional("ecosystem"),
            source_name=optional("source"),
            package=optional("package"),
            version=optional("version"),
            cursor=optional("cursor"),
            limit=limit,
        )
        self._json(200, document, send_body)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _webui_asset(self, asset: WebUIAsset, send_body: bool) -> None:
        self.send_response(200)
        self.send_header("Content-Type", asset.content_type)
        self.send_header("Content-Length", str(len(asset.body)))
        self.send_header("Cache-Control", asset.cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; connect-src 'self'; img-src 'self'; "
            "script-src 'self'; style-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'",
        )
        self.end_headers()
        if send_body:
            self.wfile.write(asset.body)

    def _error(self, status: int, message: str, send_body: bool) -> None:
        self._json(status, {"error": message}, send_body)

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        logging.info(
            "client=%s method=%s path=%s status=%s size=%s",
            self.client_address[0],
            self.command,
            urlsplit(self.path).path,
            code,
            size,
        )

    def log_message(self, format: str, *args: object) -> None:
        logging.info("http server event client=%s", self.client_address[0])

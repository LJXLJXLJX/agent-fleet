"""YiCloud transfer: extracted from the legacy Harbor provider without behavior changes."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import os
import re
import shlex
import stat
import subprocess
import tarfile
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import requests
from artifact_collection import artifact_download_slot
from opensandbox_packing import pack_directory, pack_directory_async
from opensandbox_s3_upload import S3UploadArtifact, S3UploadStore

from .config import _positive_int
from .execution import _execd_request_attempts, _retryable_execd_error

UPLOAD_CHUNK_BYTES = 512 * 1024


FAST_UPLOAD_CHUNK_BYTES = 512 * 1024


DOWNLOAD_CHUNK_BYTES = 512 * 1024


S3_HTTP_BOOTSTRAP_PATH = "/tmp/.harbor-s3-http-get-v1.sh"


S3_HTTP_BOOTSTRAP = r"""#!/usr/bin/env bash
set -euo pipefail

url=${HARBOR_S3_URL:?HARBOR_S3_URL is required}
output=${1:?output path is required}
case "$url" in
  http://*) rest=${url#http://} ;;
  *) echo "minimal S3 downloader only supports plain HTTP URLs" >&2; exit 64 ;;
esac
authority=${rest%%/*}
if [[ "$rest" == */* ]]; then
  request_path=/${rest#*/}
else
  request_path=/
fi
if [[ "$authority" == *:* ]]; then
  host=${authority%%:*}
  port=${authority##*:}
else
  host=$authority
  port=80
fi
[[ -n "$host" && -n "$port" ]] || {
  echo "invalid S3 download URL" >&2
  exit 64
}

exec 3<>"/dev/tcp/$host/$port"
printf 'GET %s HTTP/1.1\r\nHost: %s\r\nAccept: */*\r\nConnection: close\r\n\r\n' \
  "$request_path" "$authority" >&3
IFS= read -r status <&3
status=${status%$'\r'}
[[ "$status" == HTTP/1.0\ 200\ * || "$status" == HTTP/1.1\ 200\ * ]] || {
  echo "S3 download failed: $status" >&2
  exit 65
}
while IFS= read -r header <&3; do
  header=${header%$'\r'}
  [[ -z "$header" ]] && break
  [[ "${header,,}" != transfer-encoding:*chunked* ]] || {
    echo "chunked S3 response is unsupported by minimal downloader" >&2
    exit 66
  }
done
cat <&3 >"$output"
"""


class YiCloudTransferMixin:
    """Internal transfer methods sharing the adapter instance state."""

    def _upload_chunk_sync(
        self,
        content: bytes,
        target_path: str,
        filename: str,
    ) -> None:
        upload_url = self._execd_url("/files/upload")
        encoded_content = base64.b64encode(content)
        metadata = json.dumps(
            {
                "path": target_path,
                # Execd parses this value as octal text, so pass 600 rather
                # than Python's decimal representation of 0o600 (384).
                "mode": 600,
            },
            separators=(",", ":"),
        )
        attempts = _execd_request_attempts()
        for attempt in range(attempts):
            session = requests.Session()
            session.trust_env = False
            request = requests.Request(
                "POST",
                upload_url,
                files=[
                    (
                        "metadata",
                        (
                            "metadata.json",
                            io.BytesIO(metadata.encode()),
                            "application/json",
                        ),
                    ),
                    (
                        "file",
                        (
                            f"{filename}.b64",
                            io.BytesIO(encoded_content),
                            "text/plain",
                        ),
                    ),
                ],
            )
            prepared = session.prepare_request(request)
            if not isinstance(prepared.body, bytes):
                raise TypeError("execd multipart upload did not produce a binary body")
            content_type = prepared.headers.get("Content-Type", "")
            prepared.headers.update(
                self._signed_headers(
                    prepared.body.decode("ascii"),
                    url=upload_url,
                    content_type=content_type,
                    accept="application/json",
                )
            )
            try:
                response = session.send(
                    prepared,
                    timeout=self._request_timeout_sec + 300,
                )
                if response.ok:
                    return
                raise requests.HTTPError(
                    f"YiCloud execd file upload returned {response.status_code}",
                    response=response,
                )
            except requests.RequestException as error:
                if not _retryable_execd_error(error) or attempt + 1 == attempts:
                    response = getattr(error, "response", None)
                    if response is not None:
                        raise RuntimeError(
                            "YiCloud execd file upload failed "
                            f"status={response.status_code} "
                            f"body={response.text[:1000]!r}"
                        ) from error
                    raise
                delay = min(2**attempt, 5)
                self.logger.warning(
                    "YiCloud execd file upload failed transiently; "
                    "retrying in %ss (attempt %s/%s): %s",
                    delay,
                    attempt + 1,
                    attempts,
                    error,
                )
                time.sleep(delay)

    def _fast_upload_url(self) -> str:
        if not self._sandbox_id:
            raise RuntimeError("YiCloud Sandbox is not running")
        origin = os.environ.get("YICLOUD_SANDBOX_FAST_UPLOAD_ORIGIN", "").strip()
        if origin:
            parsed = urlsplit(origin)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError(
                    "YICLOUD_SANDBOX_FAST_UPLOAD_ORIGIN must be an HTTP(S) origin"
                )
            return (
                f"{origin.rstrip('/')}/sandboxes/{self._sandbox_id}"
                "/proxy/44772/files/upload"
            )

        command_url = getattr(self, "_command_url", "")
        if not command_url:
            return ""
        parsed = urlsplit(command_url)
        command_suffix = "/command"
        if not parsed.path.endswith(command_suffix):
            raise RuntimeError(
                "YiCloud execd command endpoint has an unexpected path: "
                f"{parsed.path!r}"
            )
        path = parsed.path[: -len(command_suffix)] + "/files/upload"
        return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))

    def _upload_file_fast_sync(
        self,
        source: Path,
        target_path: str,
        upload_url: str,
    ) -> None:
        timeout_sec = _positive_int(
            os.environ.get("YICLOUD_SANDBOX_UPLOAD_TIMEOUT_SEC", "1800"),
            "YICLOUD_SANDBOX_UPLOAD_TIMEOUT_SEC",
        )
        metadata_payload = {
            "path": target_path,
            "owner": "root",
            "group": "root",
            "mode": int(f"{stat.S_IMODE(source.stat().st_mode):o}"),
        }
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="yicloud-fast-upload-") as tmp:
            tmp_dir = Path(tmp)
            metadata = tmp_dir / "metadata.json"
            headers = tmp_dir / "headers.txt"
            response_body = tmp_dir / "response.txt"
            metadata.write_text(
                json.dumps(metadata_payload, separators=(",", ":")),
                encoding="utf-8",
            )
            headers.write_text(
                f"X-Sandbox-Access-Token: {self._access_token}\n",
                encoding="utf-8",
            )
            headers.chmod(0o600)
            completed = subprocess.run(
                [
                    "curl",
                    "--noproxy",
                    "*",
                    "--silent",
                    "--show-error",
                    "--connect-timeout",
                    "20",
                    "--max-time",
                    str(timeout_sec),
                    "--request",
                    "POST",
                    upload_url,
                    "--header",
                    f"@{headers}",
                    "--form",
                    (
                        f"metadata=@{metadata};type=application/json;"
                        "filename=metadata.json"
                    ),
                    "--form",
                    f"file=@{source};filename={source.name}",
                    "--output",
                    str(response_body),
                    "--write-out",
                    "%{http_code}",
                ],
                capture_output=True,
                text=True,
                timeout=timeout_sec + 30,
                check=False,
            )
            status_text = completed.stdout.strip()
            status = int(status_text) if status_text.isdigit() else 0
            if completed.returncode != 0 or not 200 <= status < 300:
                body = (
                    response_body.read_text(encoding="utf-8", errors="replace")[:1000]
                    if response_body.exists()
                    else ""
                )
                raise RuntimeError(
                    "YiCloud fast file upload failed "
                    f"curl_rc={completed.returncode} status={status} "
                    f"stderr={completed.stderr.strip()[:1000]!r} body={body!r}"
                )
        elapsed = max(time.monotonic() - started, 0.001)
        size = source.stat().st_size
        self.logger.info(
            "YiCloud fast upload complete path=%s size_bytes=%s "
            "elapsed_seconds=%.3f speed_mib_per_second=%.2f",
            target_path,
            size,
            elapsed,
            size / elapsed / 1024 / 1024,
        )

    async def _upload_file_fast(
        self,
        source: Path,
        target_path: str,
        upload_url: str,
        mode: str,
    ) -> None:
        use_signed_execd = not os.environ.get(
            "YICLOUD_SANDBOX_FAST_UPLOAD_ORIGIN", ""
        ).strip()
        if not use_signed_execd and source.stat().st_size <= FAST_UPLOAD_CHUNK_BYTES:
            await asyncio.to_thread(
                self._upload_file_fast_sync,
                source,
                target_path,
                upload_url,
            )
            return

        remote_prefix = f"/tmp/harbor-fast-upload-{time.time_ns()}"
        remote_parts: list[str] = []
        handle = None
        try:
            handle = await asyncio.to_thread(source.open, "rb")
            with tempfile.TemporaryDirectory(
                prefix="yicloud-fast-upload-parts-"
            ) as tmp:
                tmp_dir = Path(tmp)
                index = 0
                while chunk := await asyncio.to_thread(
                    handle.read, FAST_UPLOAD_CHUNK_BYTES
                ):
                    remote_part = f"{remote_prefix}-{index:04d}"
                    if use_signed_execd:
                        await asyncio.to_thread(
                            self._upload_chunk_sync,
                            chunk,
                            remote_part,
                            source.name,
                        )
                    else:
                        local_part = tmp_dir / f"part-{index:04d}"
                        await asyncio.to_thread(local_part.write_bytes, chunk)
                        await asyncio.to_thread(
                            self._upload_file_fast_sync,
                            local_part,
                            remote_part,
                            upload_url,
                        )
                    remote_parts.append(remote_part)
                    index += 1

            quoted_target = shlex.quote(target_path)
            if use_signed_execd:
                assemble = f": > {quoted_target}"
                for part in remote_parts:
                    assemble += f" && base64 -d {shlex.quote(part)} >> {quoted_target}"
                assemble += f" && chmod {mode} {quoted_target}"
            else:
                quoted_parts = " ".join(shlex.quote(part) for part in remote_parts)
                assemble = (
                    f"cat {quoted_parts} > {quoted_target} && "
                    f"chmod {mode} {quoted_target}"
                )
            assembled = await self.exec(
                assemble,
                cwd="/",
                timeout_sec=300,
                user="root",
            )
            if assembled.return_code != 0:
                raise RuntimeError(
                    f"failed to assemble fast upload at {target_path!r}: "
                    f"{assembled.stderr or assembled.stdout}"
                )
        finally:
            if handle is not None:
                await asyncio.to_thread(handle.close)
            if remote_parts:
                await self.exec(
                    "rm -f " + " ".join(shlex.quote(part) for part in remote_parts),
                    cwd="/",
                    timeout_sec=60,
                    user="root",
                )

    def _uses_s3_upload(self) -> bool:
        return getattr(self, "_upload_backend", "http") in {"s3", "auto"}

    def _allows_http_upload_fallback(self) -> bool:
        return getattr(self, "_upload_backend", "http") == "auto"

    def _s3_store(self) -> S3UploadStore:
        store = getattr(self, "_s3_upload_store", None)
        if store is None:
            setup_error = getattr(self, "_s3_upload_setup_error", None)
            if setup_error is not None:
                raise RuntimeError(
                    f"YiCloud S3 upload backend setup failed: {setup_error}"
                ) from setup_error
            raise RuntimeError("YiCloud S3 upload backend is not configured")
        return store

    async def _ensure_s3_downloader(self, download_url: str) -> None:
        if getattr(self, "_s3_downloader_ready", False):
            return
        lock = getattr(self, "_s3_downloader_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._s3_downloader_lock = lock
        async with lock:
            if getattr(self, "_s3_downloader_ready", False):
                return
            probe = await self.exec(
                "if command -v curl >/dev/null 2>&1 || "
                "command -v wget >/dev/null 2>&1 || "
                "command -v python3 >/dev/null 2>&1; then "
                "printf native; "
                "elif command -v bash >/dev/null 2>&1; then "
                "printf bootstrap; "
                "else printf missing; exit 45; fi",
                cwd="/",
                timeout_sec=30,
                user="root",
            )
            mode = str(getattr(probe, "stdout", "") or "").strip()
            if probe.return_code == 0 and mode == "native":
                self._s3_downloader_ready = True
                return
            if probe.return_code != 0 or mode != "bootstrap":
                raise RuntimeError(
                    "task image cannot download S3 artifacts: install curl, "
                    "wget, python3, or bash"
                )
            if urlsplit(download_url).scheme != "http":
                raise RuntimeError(
                    "task image only has bash, but the minimal S3 downloader "
                    "requires an HTTP object URL"
                )
            upload_url = self._fast_upload_url()
            if not upload_url:
                raise RuntimeError(
                    "task image needs the minimal S3 downloader, but "
                    "YICLOUD_SANDBOX_FAST_UPLOAD_ORIGIN is disabled"
                )
            with tempfile.TemporaryDirectory(prefix="yicloud-s3-bootstrap-") as tmp:
                source = Path(tmp) / "http-get.sh"
                source.write_text(S3_HTTP_BOOTSTRAP, encoding="utf-8")
                await self._upload_file_fast(
                    source,
                    S3_HTTP_BOOTSTRAP_PATH,
                    upload_url,
                    "700",
                )
            prepared = await self.exec(
                f"chmod 700 {shlex.quote(S3_HTTP_BOOTSTRAP_PATH)}",
                cwd="/",
                timeout_sec=30,
                user="root",
            )
            if prepared.return_code != 0:
                raise RuntimeError(
                    "failed to prepare minimal S3 downloader: "
                    f"return_code={prepared.return_code} "
                    f"stderr={getattr(prepared, 'stderr', '')!r}"
                )
            self._s3_downloader_ready = True
            self.logger.info(
                "YiCloud S3 downloader bootstrap uploaded once path=%s size_bytes=%s",
                S3_HTTP_BOOTSTRAP_PATH,
                len(S3_HTTP_BOOTSTRAP.encode("utf-8")),
            )

    def _s3_download_command(self, artifact: S3UploadArtifact, temporary: str) -> str:
        target = shlex.quote(temporary)
        timeout = self._s3_download_timeout_sec
        return (
            f"rm -f {target}; "
            "if command -v curl >/dev/null 2>&1; then "
            f"curl --noproxy '*' --fail --silent --show-error --location "
            f"--retry 3 --connect-timeout 30 --max-time {timeout} "
            f'--output {target} "$HARBOR_S3_URL" || exit 48; '
            "elif command -v wget >/dev/null 2>&1; then "
            f'wget --no-proxy -q -O {target} "$HARBOR_S3_URL" || exit 48; '
            "elif command -v python3 >/dev/null 2>&1; then "
            "python3 -c 'import shutil,sys,urllib.request; "
            "opener=urllib.request.build_opener(urllib.request.ProxyHandler({})); "
            "response=opener.open(sys.argv[1]); "
            'output=open(sys.argv[2],"wb"); '
            "shutil.copyfileobj(response,output); output.close(); response.close()' "
            f'"$HARBOR_S3_URL" {target} || exit 48; '
            f"elif [ -x {shlex.quote(S3_HTTP_BOOTSTRAP_PATH)} ]; then "
            f"bash {shlex.quote(S3_HTTP_BOOTSTRAP_PATH)} {target} || exit 48; "
            "else exit 45; fi; "
            f"actual_size=$(wc -c < {target} | tr -d ' '); "
            f'[ "$actual_size" = {artifact.payload_size} ] || '
            f"{{ rm -f {target}; exit 46; }}; "
            f"actual_digest=$(sha256sum {target} | awk '{{print $1}}'); "
            f'[ "$actual_digest" = '
            f"{shlex.quote(artifact.payload_digest)} ] || "
            f"{{ rm -f {target}; exit 47; }}"
        )

    async def _materialize_s3_file(
        self,
        artifact: S3UploadArtifact,
        target_path: str,
        mode: str,
    ) -> None:
        await self._ensure_s3_downloader(artifact.download_url)
        parent = str(Path(target_path).parent)
        temporary = f"/tmp/harbor-s3-file-{time.time_ns()}"
        command = (
            "harbor_rc=1; "
            "for harbor_attempt in 1 2 3; do "
            f"if ({self._s3_download_command(artifact, temporary)}) && "
            f"chmod {mode} {shlex.quote(temporary)} && "
            f"mkdir -p {shlex.quote(parent)} && "
            f"mv -f {shlex.quote(temporary)} {shlex.quote(target_path)}; "
            "then harbor_rc=0; break; "
            f"else harbor_rc=$?; rm -f {shlex.quote(temporary)}; "
            'sleep "$harbor_attempt"; fi; done; exit "$harbor_rc"'
        )
        result = await self.exec(
            command,
            cwd="/",
            env={"HARBOR_S3_URL": artifact.download_url},
            timeout_sec=self._s3_download_timeout_sec + 60,
            user="root",
        )
        if result.return_code != 0:
            raise RuntimeError(
                f"failed to materialize S3 file to {target_path!r}: "
                f"return_code={result.return_code} "
                f"stdout={getattr(result, 'stdout', '')!r} "
                f"stderr={getattr(result, 'stderr', '')!r}"
            )

    async def _materialize_s3_directory(
        self, artifact: S3UploadArtifact, target_dir: str
    ) -> None:
        await self._ensure_s3_downloader(artifact.download_url)
        temporary = f"/tmp/harbor-s3-{time.time_ns()}.tar"
        command = (
            "harbor_rc=1; "
            "for harbor_attempt in 1 2 3; do "
            f"if ({self._s3_download_command(artifact, temporary)}) && "
            f"mkdir -p {shlex.quote(target_dir)} && "
            f"tar xf {shlex.quote(temporary)} "
            f"-C {shlex.quote(target_dir)} && "
            f"rm -f {shlex.quote(temporary)}; "
            "then harbor_rc=0; break; "
            f"else harbor_rc=$?; rm -f {shlex.quote(temporary)}; "
            'sleep "$harbor_attempt"; fi; done; exit "$harbor_rc"'
        )
        result = await self.exec(
            command,
            cwd="/",
            env={"HARBOR_S3_URL": artifact.download_url},
            timeout_sec=self._s3_download_timeout_sec + 300,
            user="root",
        )
        if result.return_code != 0:
            raise RuntimeError(
                f"failed to materialize S3 directory to {target_dir!r}: "
                f"return_code={result.return_code} "
                f"stdout={getattr(result, 'stdout', '')!r} "
                f"stderr={getattr(result, 'stderr', '')!r}"
            )

    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        source = Path(source_path)
        if not source.is_file():
            raise RuntimeError(f"upload source is not a file: {source}")
        mode = f"{stat.S_IMODE(source.stat().st_mode):o}"
        if self._uses_s3_upload():
            started = time.monotonic()
            phase = "stage"
            try:
                artifact = await asyncio.to_thread(self._s3_store().stage_file, source)
                phase = "materialize"
                await self._materialize_s3_file(artifact, target_path, mode)
            except Exception as exc:
                if not self._allows_http_upload_fallback():
                    raise
                self.logger.warning(
                    "YiCloud upload fallback backend=auto "
                    "attempted_backend=s3 fallback_backend=http kind=file "
                    "phase=%s target=%s error=%s",
                    phase,
                    target_path,
                    exc,
                )
            else:
                elapsed = max(time.monotonic() - started, 0.001)
                self.logger.info(
                    "YiCloud upload complete backend=s3 kind=file "
                    "target=%s digest=%s size_bytes=%s elapsed_seconds=%.3f",
                    target_path,
                    artifact.logical_digest,
                    artifact.payload_size,
                    elapsed,
                )
                return
        await self._upload_file_http(source, target_path, mode)

    async def _upload_file_http(
        self, source: Path, target_path: str, mode: str
    ) -> None:
        parent = str(Path(target_path).parent)
        fast_upload_url = self._fast_upload_url()
        remote_chunk = f"/tmp/harbor-upload-{time.time_ns()}.chunk"
        prepare = await self.exec(
            f"mkdir -p {shlex.quote(parent)} && : > {shlex.quote(target_path)}",
            cwd="/",
            timeout_sec=60,
            user="root",
        )
        if prepare.return_code != 0:
            raise RuntimeError(f"failed to prepare upload target {target_path!r}")
        if fast_upload_url:
            await self._upload_file_fast(
                source,
                target_path,
                fast_upload_url,
                mode,
            )
            return
        handle = None
        try:
            handle = await asyncio.to_thread(source.open, "rb")
            while chunk := await asyncio.to_thread(handle.read, UPLOAD_CHUNK_BYTES):
                await asyncio.to_thread(
                    self._upload_chunk_sync,
                    chunk,
                    remote_chunk,
                    source.name,
                )
                append = await self.exec(
                    f"base64 -d {shlex.quote(remote_chunk)} >> "
                    f"{shlex.quote(target_path)} && "
                    f"rm -f {shlex.quote(remote_chunk)}",
                    cwd="/",
                    timeout_sec=120,
                    user="root",
                )
                if append.return_code != 0:
                    raise RuntimeError(
                        f"failed to append upload chunk to {target_path!r}: "
                        f"{append.stderr}"
                    )
        finally:
            if handle is not None:
                await asyncio.to_thread(handle.close)
            await self.exec(
                f"rm -f {shlex.quote(remote_chunk)}",
                cwd="/",
                timeout_sec=60,
                user="root",
            )
        finalize = await self.exec(
            f"chmod {mode} {shlex.quote(target_path)}",
            cwd="/",
            timeout_sec=60,
            user="root",
        )
        if finalize.return_code != 0:
            raise RuntimeError(
                f"failed to preserve upload mode for {target_path!r}: {finalize.stderr}"
            )

    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        source = Path(source_dir)
        if not source.is_dir():
            raise RuntimeError(f"upload source is not a directory: {source}")
        if (
            target_dir.rstrip("/") == "/logs/agent"
            and (
                getattr(self, "_oracle_skip_agent_log_reupload", False)
                or getattr(self, "_oracle_skip_agent_log_transfer", False)
            )
        ):
            self.logger.info(
                "YiCloud upload skipped purpose=oracle-agent-log-reupload "
                "target=%s",
                target_dir,
            )
            return
        if (
            target_dir.rstrip("/") == "/solution"
            and getattr(
                self,
                "_oracle_solution_upload_backend",
                "inherit",
            )
            == "inline"
        ):
            started = time.monotonic()
            file_paths = [path for path in source.rglob("*") if path.is_file()]
            logical_bytes = sum(path.stat().st_size for path in file_paths)
            archive_buffer = io.BytesIO()
            with tarfile.open(fileobj=archive_buffer, mode="w:gz") as archive:
                archive.add(source, arcname=".")
            payload = archive_buffer.getvalue()
            maximum = getattr(self, "_oracle_inline_max_bytes", 256 * 1024)
            if len(payload) > maximum:
                raise RuntimeError(
                    "Oracle inline solution archive exceeds configured limit: "
                    f"archive_bytes={len(payload)} limit_bytes={maximum}"
                )
            self._oracle_inline_solution_payload = base64.b64encode(payload).decode()
            self._oracle_inline_solution_archive_bytes = len(payload)
            self._oracle_inline_solution_logical_bytes = logical_bytes
            self._oracle_inline_solution_file_count = len(file_paths)
            elapsed = max(time.monotonic() - started, 0.001)
            self.logger.info(
                "YiCloud Oracle solution staged backend=inline "
                "target=%s file_count=%s logical_bytes=%s archive_bytes=%s "
                "local_elapsed_seconds=%.3f",
                target_dir,
                len(file_paths),
                logical_bytes,
                len(payload),
                elapsed,
            )
            return
        if (
            target_dir.rstrip("/") == "/solution"
            and getattr(
                self,
                "_oracle_solution_upload_backend",
                "inherit",
            )
            == "execd"
        ):
            started = time.monotonic()
            await self._upload_dir_http(source, target_dir)
            elapsed = max(time.monotonic() - started, 0.001)
            self.logger.info(
                "YiCloud upload complete backend=execd kind=directory "
                "purpose=oracle-solution target=%s elapsed_seconds=%.3f",
                target_dir,
                elapsed,
            )
            return
        if self._uses_s3_upload():
            started = time.monotonic()
            phase = "stage"
            try:
                artifact = await asyncio.to_thread(
                    self._s3_store().stage_directory, source
                )
                phase = "materialize"
                await self._materialize_s3_directory(artifact, target_dir)
            except Exception as exc:
                if not self._allows_http_upload_fallback():
                    raise
                self.logger.warning(
                    "YiCloud upload fallback backend=auto "
                    "attempted_backend=s3 fallback_backend=http "
                    "kind=directory phase=%s target=%s error=%s",
                    phase,
                    target_dir,
                    exc,
                )
            else:
                elapsed = max(time.monotonic() - started, 0.001)
                self.logger.info(
                    "YiCloud upload complete backend=s3 kind=directory "
                    "target=%s digest=%s payload_bytes=%s compression=%s "
                    "elapsed_seconds=%.3f",
                    target_dir,
                    artifact.logical_digest,
                    artifact.payload_size,
                    artifact.compression,
                    elapsed,
                )
                return
        await self._upload_dir_http(source, target_dir)

    async def _upload_dir_http(self, source: Path, target_dir: str) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            archive = Path(tmp_dir) / "upload.tar.gz"
            if (
                os.environ.get("HARBOR_NATIVE_CONCURRENCY") == "1"
                and os.environ.get("HARBOR_OPENSANDBOX_COMMAND_MODE", "sync")
                == "async"
            ):
                await pack_directory_async(source, archive)
            else:
                pack_directory(source, archive)
            remote_archive = f"/tmp/harbor-upload-{time.time_ns()}.tar.gz"
            mode = f"{stat.S_IMODE(archive.stat().st_mode):o}"
            await self._upload_file_http(archive, remote_archive, mode)
            result = await self.exec(
                f"mkdir -p {shlex.quote(target_dir)} && "
                f"tar xzf {shlex.quote(remote_archive)} -C {shlex.quote(target_dir)} && "
                f"rm -f {shlex.quote(remote_archive)}",
                cwd="/",
                timeout_sec=180,
                user="root",
            )
            if result.return_code != 0:
                raise RuntimeError(f"failed to upload directory to {target_dir!r}")

    async def _materialize_read_only_mounts(self) -> None:
        """Copy immutable host bind mounts into a non-mounted Sandbox."""
        for mount in getattr(self, "_mounts", []):
            if mount.get("type") != "bind" or not mount.get("read_only"):
                continue

            source_value = str(mount.get("source") or "").strip()
            target = str(mount.get("target") or "").strip()
            if not source_value or not target:
                raise RuntimeError(
                    "read-only OpenSandbox bind mounts require source and target"
                )
            if not target.startswith("/"):
                raise RuntimeError(
                    f"OpenSandbox mount target must be absolute: {target!r}"
                )

            source = Path(source_value)
            if not source.exists():
                raise RuntimeError(f"OpenSandbox mount source does not exist: {source}")

            self.logger.info(
                "Materializing read-only mount source=%s target=%s",
                source,
                target,
            )
            if source.is_dir():
                await self.upload_dir(source, target)
            elif source.is_file():
                await self.upload_file(source, target)
            else:
                raise RuntimeError(
                    f"unsupported OpenSandbox mount source type: {source}"
                )

    async def _download_file_chunks(
        self, source_path: str, target_path: Path | str
    ) -> None:
        source = shlex.quote(source_path)
        metadata = await self.exec(
            f"set -euo pipefail; size=$(stat -L -c %s -- {source}); "
            f"printf '%s ' \"$size\"; sha256sum < {source}",
            cwd="/",
            timeout_sec=180,
            user="root",
        )
        if metadata.return_code != 0:
            raise RuntimeError(
                f"failed to inspect download {source_path!r}: {metadata.stderr}"
            )
        fields = (metadata.stdout or "").split()
        if len(fields) != 3 or not re.fullmatch(r"[0-9a-f]{64}", fields[1]):
            raise RuntimeError("invalid download size/checksum response")
        size = int(fields[0])
        if size < 0:
            raise RuntimeError("invalid download size")
        digest = hashlib.sha256()
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            temporary = target.parent / f".harbor-download-{uuid.uuid4().hex}.part"
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
            with os.fdopen(descriptor, "wb") as output:
                if target.exists():
                    os.fchmod(output.fileno(), stat.S_IMODE(target.stat().st_mode))
                offset = 0
                while offset < size:
                    result = await self.exec(
                        "set -o pipefail; "
                        f"dd if={source} bs={DOWNLOAD_CHUNK_BYTES} "
                        f"skip={offset // DOWNLOAD_CHUNK_BYTES} count=1 status=none | base64 -w0",
                        cwd="/",
                        timeout_sec=180,
                        user="root",
                    )
                    if result.return_code != 0:
                        raise RuntimeError(
                            f"failed to download {source_path!r}: {result.stderr}"
                        )
                    data = base64.b64decode(result.stdout or "", validate=True)
                    expected = min(DOWNLOAD_CHUNK_BYTES, size - offset)
                    if len(data) != expected:
                        raise RuntimeError(
                            "download changed or returned a short/oversized chunk"
                        )
                    output.write(data)
                    digest.update(data)
                    offset += len(data)
                if digest.hexdigest() != fields[1]:
                    raise RuntimeError(
                        "download checksum mismatch; source may have changed"
                    )
            # A failed transfer never replaces a previously valid target.
            temporary.replace(target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        if (
            source_path.rstrip("/") == "/logs/agent/oracle.txt"
            and getattr(self, "_oracle_skip_agent_log_transfer", False)
        ):
            self.logger.info(
                "YiCloud download skipped purpose=oracle-agent-log "
                "source=%s",
                source_path,
            )
            return
        async with artifact_download_slot():
            await self._download_file_chunks(source_path, target_path)

    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        if (
            source_dir.rstrip("/") == "/logs/agent"
            and getattr(self, "_oracle_skip_agent_log_transfer", False)
        ):
            self.logger.info(
                "YiCloud download skipped purpose=oracle-agent-log-directory "
                "source=%s",
                source_dir,
            )
            return
        await self._download_dir_archive(source_dir, target_dir, exclude=[])

    async def download_dir_with_exclusions(
        self,
        *,
        source_dir: str,
        target_dir: Path | str,
        exclude: list[str],
    ) -> None:
        await self._download_dir_archive(source_dir, target_dir, exclude=exclude)

    async def _download_dir_archive(
        self,
        source_dir: str,
        target_dir: Path | str,
        *,
        exclude: list[str],
    ) -> None:
        async with artifact_download_slot():
            remote_archive = f"/tmp/harbor-download-{uuid.uuid4().hex}.tar.gz"
            archive_q = shlex.quote(remote_archive)
            exclude_flags = " ".join(
                f"--exclude={shlex.quote(item)}" for item in exclude
            )
            try:
                result = await self.exec(
                    f"tar czf {archive_q} {exclude_flags} -C {shlex.quote(source_dir)} .",
                    cwd="/",
                    timeout_sec=300,
                    user="root",
                )
                if result.return_code != 0:
                    raise RuntimeError(f"failed to archive directory {source_dir!r}")
                target = Path(target_dir)
                target.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory() as tmp_dir:
                    archive = Path(tmp_dir) / "download.tar.gz"
                    await self._download_file_chunks(remote_archive, archive)
                    with tarfile.open(archive, "r:gz") as tar:
                        tar.extractall(target, filter="data")
            finally:
                try:
                    cleanup = await self.exec(
                        f"rm -f -- {archive_q}",
                        cwd="/",
                        timeout_sec=30,
                        user="root",
                    )
                    if cleanup.return_code != 0:
                        self.logger.warning(
                            "Failed to remove temporary download archive"
                        )
                except Exception:
                    self.logger.warning(
                        "Failed to remove temporary download archive", exc_info=True
                    )

"""YiCloud native background commands, with bounded asynchronous transport.

Only command submission is non-idempotent: never replay it after an ambiguous
response. Raw stdout/stderr live in separate files, not lossy execd events.
"""
from __future__ import annotations

import asyncio
import codecs
import io
import json
import os
import random
import re
import shlex
import uuid
import weakref
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx
from async_utils import await_owned

CHUNK_BYTES = 64 * 1024
CLEANUP_SECONDS = 15


class CommandProtocolError(RuntimeError):
    """The command's result or termination could not be established."""


class CommandSubmissionUnknown(CommandProtocolError):
    """Submission may have executed; retire this environment before retrying."""


class _SharedTransport:
    def __init__(self):
        concurrency = max(1, int(os.environ.get("HARBOR_N_CONCURRENT", "32")))
        self.commands = asyncio.Semaphore(concurrency)
        # Separate pools reserve capacity for cancellation while output is read.
        limits = httpx.Limits(max_connections=min(concurrency + 2, 128),
                              max_keepalive_connections=8, keepalive_expiry=5)
        self.control = httpx.AsyncClient(trust_env=False, limits=limits, timeout=60)
        self.files = httpx.AsyncClient(trust_env=False, limits=limits, timeout=60)
        self.users = 0

    async def close(self):
        await self.control.aclose()
        await self.files.aclose()


_TRANSPORTS = weakref.WeakKeyDictionary()


@dataclass
class _Command:
    execution_id: str | None = None
    submitted: bool = False
    terminal: bool = False
    directory: str | None = None
    status_limit: int = CHUNK_BYTES


class AsyncCommandRunner:
    def __init__(self, environment):
        self.env = environment
        self.loop = asyncio.get_running_loop()
        if self.loop not in _TRANSPORTS:
            _TRANSPORTS[self.loop] = _SharedTransport()
        self.transport = _TRANSPORTS[self.loop]
        self.transport.users += 1
        self.tasks = set()
        self.closing = False
        self.poisoned = False
        self.closed = False
        self.close_lock = asyncio.Lock()

    def _parts(self, path, payload=None, query=None, extra_headers=None):
        url = self.env._execd_url(path)
        if query:
            url += ("&" if "?" in url else "?") + urlencode(query)
        body = "" if payload is None else json.dumps(payload, separators=(",", ":"))
        headers = self.env._signed_headers(body, url=url)
        headers.update(extra_headers or {})
        return url, body.encode(), headers

    async def _request(self, method, path, *, query=None, headers=None, files=False,
                       limit=CHUNK_BYTES):
        client = self.transport.files if files else self.transport.control
        for attempt in range(3):
            url, body, signed = self._parts(path, query=query, extra_headers=headers)
            try:
                async with client.stream(method, url, content=body, headers=signed) as r:
                    content = bytearray()
                    async for chunk in r.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > limit:
                            raise CommandProtocolError("Execd response exceeded chunk limit")
                    response = httpx.Response(r.status_code, headers=r.headers,
                                              content=bytes(content))
                if method != "GET" or response.status_code not in (429, 502, 503, 504):
                    return response
            except httpx.TransportError:
                if method != "GET" or attempt == 2:
                    raise
            if attempt == 2:
                return response
            await asyncio.sleep(0.1 * 2**attempt)
        raise AssertionError("unreachable")

    async def _submit(self, state, command, cwd, env, uid, timeout_ms):
        payload = {"command": command, "background": True, "timeout": timeout_ms,
                   "envs": env or {}}
        if cwd is not None:
            payload["cwd"] = cwd
        if uid is not None:
            payload["uid"] = uid
        url, body, headers = self._parts("/command", payload)
        # Status echoes the submitted command. Bound that response by the known
        # request size rather than rejecting legitimate large command strings.
        state.status_limit = len(body) + CHUNK_BYTES
        state.submitted = True
        try:
            async with self.transport.control.stream(
                "POST", url, content=body, headers=headers,
            ) as response:
                if response.status_code in (400, 401, 403, 404, 422):
                    state.submitted = False
                    raise CommandProtocolError(
                        f"Execd rejected command: HTTP {response.status_code}"
                    )
                if response.status_code != 200:
                    raise CommandSubmissionUnknown("Execd submission response is ambiguous")
                pending = b""
                async for chunk in response.aiter_bytes():
                    pending += chunk
                    if len(pending) > CHUNK_BYTES:
                        raise CommandProtocolError("Execd submission event is oversized")
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        line = line.strip()
                        if line.startswith(b"data:"):
                            line = line[5:].strip()
                        if not line.startswith(b"{"):
                            continue
                        node = json.loads(line)
                        if node.get("type") == "init":
                            cid = node.get("text")
                            if not isinstance(cid, str) or not re.fullmatch(r"[\w-]{1,128}", cid):
                                raise CommandProtocolError("Invalid execd execution ID")
                            state.execution_id = cid
                        if node.get("type") == "execution_complete" and state.execution_id:
                            return
        except (httpx.TransportError, ValueError) as exc:
            if state.execution_id:
                return  # Recover by ID; do not replay the POST.
            raise CommandSubmissionUnknown("Execd submission lost its execution ID") from exc
        if not state.execution_id:
            raise CommandSubmissionUnknown("Execd submission returned no execution ID")

    async def _wait(self, state):
        started = self.loop.time()
        seen = False
        delay = 0.1
        while True:
            r = await self._request("GET", f"/command/status/{state.execution_id}",
                                    limit=state.status_limit)
            if r.status_code == 404 and not seen and self.loop.time() - started < 3:
                await asyncio.sleep(0.1)
                continue
            if r.status_code != 200:
                raise CommandProtocolError(f"Execd status unavailable: HTTP {r.status_code}")
            data = r.json()
            seen = True
            if data.get("running") is False and type(data.get("exit_code")) is int:
                state.terminal = True
                return data["exit_code"]
            if data.get("running") is not True:
                raise CommandProtocolError("Execd status has no valid running/exit_code pair")
            await asyncio.sleep(delay * random.uniform(0.9, 1.1))
            delay = min(delay * 2, 1)

    async def _read_output(self, path):
        response = await self._request("GET", "/files/info", query={"path": path}, files=True)
        if response.status_code != 200:
            raise CommandProtocolError(f"Command output metadata unavailable: HTTP {response.status_code}")
        metadata = response.json().get(path, {})
        size = metadata.get("size")
        if type(size) is not int or size < 0:
            raise CommandProtocolError("Command output has invalid size")
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        output = io.StringIO()
        for offset in range(0, size, CHUNK_BYTES):
            end = min(offset + CHUNK_BYTES, size) - 1
            part = await self._request(
                "GET", "/files/download", query={"path": path}, files=True,
                headers={"Range": f"bytes={offset}-{end}"},
            )
            if (part.status_code != 206
                    or part.headers.get("content-range") != f"bytes {offset}-{end}/{size}"
                    or len(part.content) != end - offset + 1):
                raise CommandProtocolError("Command output Range mismatch or file changed")
            output.write(decoder.decode(part.content))
        output.write(decoder.decode(b"", final=True))
        after = await self._request("GET", "/files/info", query={"path": path}, files=True)
        if after.status_code != 200 or after.json().get(path) != metadata:
            raise CommandProtocolError("Command output changed after command termination")
        return output.getvalue()

    async def _interrupt(self, state):
        if state.terminal or not state.submitted:
            return
        if not state.execution_id:
            raise CommandSubmissionUnknown("Cannot interrupt command with unknown execution ID")
        try:
            r = await self._request("DELETE", "/command", query={"id": state.execution_id})
            # The server timeout may win the race and reject an interrupt for
            # an already finished command. Verify status even after rejection.
            self.env.logger.debug("Async interrupt execution_id=%s http_status=%s",
                                  state.execution_id, r.status_code)
        except httpx.TransportError:
            pass  # Only a terminal status proves that cancellation took effect.
        await self._wait(state)

    async def _remove_output(self, state, uid):
        if not state.directory or not state.terminal:
            return
        cleanup = _Command()
        await self._submit(cleanup, f"rm -rf -- {shlex.quote(state.directory)}",
                           "/", {}, uid, 10000)
        if await self._wait(cleanup) != 0:
            raise CommandProtocolError("Failed to remove command output directory")
        state.directory = None

    async def _recover(self, state, uid):
        async with asyncio.timeout(CLEANUP_SECONDS):
            await self._interrupt(state)
            await self._remove_output(state, uid)

    async def _protected_recovery(self, state, uid):
        task = asyncio.create_task(self._recover(state, uid))
        try:
            await await_owned(asyncio.gather(task, return_exceptions=True))
        finally:
            try:
                task.result()
            except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 -- Preserve caller failure.
                self.poisoned = True
                self.env.logger.warning("Async command recovery failed (%s); retire sandbox_id=%s",
                                        type(exc).__name__, self.env._sandbox_id)

    @staticmethod
    def _wrap(command, directory):
        root = shlex.quote(directory)
        return (
            "set +e\n"
            "harbor_async_umask=$(umask)\n"
            "umask 077\n"
            f"mkdir -- {root} || exit $?\n"
            f": > {root}/stdout && : > {root}/stderr || exit $?\n"
            'umask "$harbor_async_umask"\n'
            "(\n" + command + "\n"
            f") > {root}/stdout 2> {root}/stderr\n"
            "harbor_async_rc=$?\n"
            'exit "$harbor_async_rc"\n'
        )

    async def run(self, command, cwd, env, timeout_sec, uid):
        # close() must await command cleanup, not a startup caller that re-enters close().
        task = asyncio.create_task(self._run(command, cwd, env, timeout_sec, uid))
        return await task

    async def _run(self, command, cwd, env, timeout_sec, uid):
        if self.closing or self.poisoned:
            raise CommandProtocolError("Async command environment is retiring; do not retry here")
        timeout = 3600 if timeout_sec is None else timeout_sec
        if timeout <= 0:
            raise ValueError("Command timeout must be positive")
        state = _Command(directory=f"/tmp/harbor-async-{uuid.uuid4().hex}")
        current = asyncio.current_task()
        self.tasks.add(current)
        deadline = self.loop.time() + timeout
        acquired = False
        try:
            async with asyncio.timeout_at(deadline):
                await self.transport.commands.acquire()
                acquired = True
                if self.closing or self.poisoned:
                    raise CommandProtocolError("Async command environment is retiring")
                await self._submit(state, self._wrap(command, state.directory), cwd, env, uid,
                                   max(1, int((deadline - self.loop.time()) * 1000)))
                self.env.logger.debug("Async command accepted sandbox_id=%s execution_id=%s",
                                      self.env._sandbox_id, state.execution_id)
                rc = await self._wait(state)
                stdout = await self._read_output(f"{state.directory}/stdout")
                stderr = await self._read_output(f"{state.directory}/stderr")
            return stdout, stderr, rc
        except TimeoutError:
            await self._protected_recovery(state, uid)
            if state.submitted and not state.terminal:
                raise CommandProtocolError("Command deadline expired; termination unconfirmed") from None
            return "", f"command timed out after {timeout}s", 124
        except BaseException:
            if state.submitted and not state.execution_id:
                self.poisoned = True
            await self._protected_recovery(state, uid)
            raise
        finally:
            try:
                if state.terminal and not self.poisoned:
                    # A cleanup failure must not hide a valid result or original error.
                    async with asyncio.timeout(CLEANUP_SECONDS):
                        await self._remove_output(state, uid)
            except Exception as exc:  # noqa: BLE001 -- cleanup must not mask a command result
                self.env.logger.warning("Async output cleanup failed (%s) sandbox_id=%s",
                                        type(exc).__name__, self.env._sandbox_id)
            finally:
                if acquired:
                    self.transport.commands.release()
                self.tasks.discard(current)

    async def close(self):
        await await_owned(self._close())

    async def _close(self):
        async with self.close_lock:
            if self.closed:
                return
            self.closing = True
            tasks = list(self.tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            self.transport.users -= 1
            self.closed = True
            if self.transport.users == 0:
                _TRANSPORTS.pop(self.loop, None)
                await self.transport.close()

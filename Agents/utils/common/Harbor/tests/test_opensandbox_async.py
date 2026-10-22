import asyncio
import json
import logging
import os
import re
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from harbor_environment_adapters.providers.opensandbox_yicloud.async_commands import (  # noqa: E402
    AsyncCommandRunner,
    CommandProtocolError,
    CommandSubmissionUnknown,
    _SharedTransport,
)


class SharedConnectionCapacityTest(unittest.IsolatedAsyncioTestCase):
    async def test_control_and_files_requests_propagate_60_second_timeouts(self):
        observed = []

        async def handle(request):
            observed.append(request.extensions["timeout"])
            return httpx.Response(200)

        client = httpx.AsyncClient
        with patch("harbor_environment_adapters.providers.opensandbox_yicloud.async_commands.httpx.AsyncClient", side_effect=lambda **kwargs:
                   client(transport=httpx.MockTransport(handle), **kwargs)):
            transport = _SharedTransport()
        try:
            await transport.control.get("https://sandbox.invalid/command/status/test")
            await transport.files.get("https://sandbox.invalid/files/download")
        finally:
            await transport.close()
        self.assertEqual(observed, [{"connect": 60, "read": 60, "write": 60, "pool": 60}] * 2)

    async def test_128_control_requests_can_hold_connections_together(self):
        arrived = 0
        all_arrived = asyncio.Event()
        release = asyncio.Event()

        async def serve(reader, writer):
            nonlocal arrived
            try:
                headers = await reader.readuntil(b"\r\n\r\n")
                if headers.startswith(b"GET /hold "):
                    arrived += 1
                    if arrived == 128:
                        all_arrived.set()
                    await release.wait()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(serve, "127.0.0.1", 0, backlog=256)
        port = server.sockets[0].getsockname()[1]
        with patch.dict(os.environ, HARBOR_N_CONCURRENT="128"):
            transport = _SharedTransport()
        # Initialize HTTPX's lazy networking backend before timing admission.
        await transport.control.get(f"http://127.0.0.1:{port}/warmup")
        requests = []
        for _ in range(128):
            requests.append(asyncio.create_task(transport.control.get(f"http://127.0.0.1:{port}/hold")))
            await asyncio.sleep(.01)
        try:
            try:
                await asyncio.wait_for(all_arrived.wait(), timeout=5)
            except TimeoutError:
                pass
        finally:
            held_connections = arrived
            release.set()
            responses = await asyncio.gather(*requests, return_exceptions=True)
            await transport.close()
            server.close()
            await server.wait_closed()
        self.assertEqual(held_connections, 128, f"First response: {responses[0]!r}")
        self.assertTrue(all(isinstance(r, httpx.Response) and r.status_code == 200 for r in responses))


class BrokenStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b'{"type":"init","text":"cmd-1"}\n\n'
        raise httpx.ReadError("connection lost")


class AsyncCommandTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.signatures = []
        self.stdout = (b"x" * 65535) + "你好\r\n\nlast\n".encode()
        self.stderr = b"ERR\n"
        self.running = False
        self.status_calls = 0
        self.initial_404 = False
        self.mode = "bare"
        self.bad_range = False
        self.interrupt_status = 200
        self.command = ""
        self.extra_runner = None

        def signed(body, *, url):
            self.signatures.append((body, url))
            return {"X-Test-Token": "fake-token"}

        self.env = SimpleNamespace(
            _sandbox_id="test-sandbox", logger=logging.getLogger("async-test"),
            _execd_url=lambda p: "https://sandbox.invalid/proxy" + p,
            _signed_headers=signed,
        )
        self.runner = AsyncCommandRunner(self.env)
        await self.runner.transport.control.aclose()
        await self.runner.transport.files.aclose()
        transport = httpx.MockTransport(self.handle)
        self.runner.transport.control = httpx.AsyncClient(transport=transport)
        self.runner.transport.files = httpx.AsyncClient(transport=transport)

    async def asyncTearDown(self):
        if self.extra_runner:
            await self.extra_runner.close()
        await self.runner.close()

    async def handle(self, request):
        self.requests.append(request)
        path = request.url.path
        if request.method == "POST":
            payload = json.loads(request.content)
            assert payload["background"] is True
            if payload["command"].startswith("rm -rf"):
                return httpx.Response(200, content=b'{"type":"init","text":"cleanup"}\n')
            self.command = payload["command"]
            if self.mode == "lost":
                raise httpx.ReadError("response lost")
            if self.mode == "partial":
                return httpx.Response(200, stream=BrokenStream())
            if self.mode == "missing":
                return httpx.Response(200, content=b'{"type":"execution_complete"}\n')
            prefix = "data: " if self.mode == "sse" else ""
            return httpx.Response(200, content=(
                prefix + '{"type":"init","text":"cmd-1"}\n\n' + prefix
                + '{"type":"execution_complete"}\n\n'
            ).encode())
        if request.method == "DELETE":
            self.running = False
            return httpx.Response(self.interrupt_status)
        if "/command/status/" in path:
            if path.endswith("cleanup"):
                return httpx.Response(200, json={"running": False, "exit_code": 0})
            self.status_calls += 1
            if self.initial_404 and self.status_calls == 1:
                return httpx.Response(404)
            return httpx.Response(200, json={"running": self.running, "exit_code": 7,
                                            "content": self.command})
        name = request.url.params["path"]
        content = self.stdout if name.endswith("stdout") else self.stderr
        if path.endswith("/files/info"):
            return httpx.Response(200, json={name: {"size": len(content), "mode": 600}})
        if path.endswith("/files/download"):
            start, end = map(int, re.fullmatch(r"bytes=(\d+)-(\d+)", request.headers["range"]).groups())
            return httpx.Response(200 if self.bad_range else 206,
                                  content=content[start:end + 1],
                                  headers={"Content-Range": f"bytes {start}-{end}/{len(content)}"})
        raise AssertionError(path)

    def business_posts(self):
        return [r for r in self.requests if r.method == "POST"
                and not json.loads(r.content)["command"].startswith("rm -rf")]

    async def start_running_command(self):
        self.running = True
        task = asyncio.create_task(self.runner.run("sleep 100", None, None, 300, None))
        while not self.status_calls:
            await asyncio.sleep(0)
        return task

    async def test_output_preserves_channels_newlines_and_split_unicode(self):
        self.initial_404 = True
        result = await self.runner.run("printf ignored", "/work", {"A": "B"}, 60, 123)
        self.assertEqual(result, (self.stdout.decode(), self.stderr.decode(), 7))
        self.assertEqual(len(self.business_posts()), 1)
        payload = json.loads(self.business_posts()[0].content)
        self.assertEqual((payload["cwd"], payload["envs"], payload["uid"]), ("/work", {"A": "B"}, 123))
        self.assertTrue(any("path=%2Ftmp%2F" in url for _, url in self.signatures))

    async def test_standard_sse_and_none_timeout_use_same_background_path(self):
        self.mode = "sse"
        self.stdout = self.stderr = b""
        self.assertEqual(await self.runner.run("true", None, None, None, None), ("", "", 7))
        payload = json.loads(self.business_posts()[0].content)
        self.assertGreater(payload["timeout"], 3590000)

    async def test_status_can_echo_large_submitted_command(self):
        result = await self.runner.run("printf " + "x" * 100000, None, None, 30, None)
        self.assertEqual(result[2], 7)

    async def test_known_id_survives_submission_disconnect_without_replay(self):
        self.mode = "partial"
        self.assertEqual((await self.runner.run("true", None, None, 30, None))[2], 7)
        self.assertEqual(len(self.business_posts()), 1)

    async def test_unknown_submission_poisons_environment_without_replay(self):
        self.mode = "lost"
        with self.assertRaises(CommandSubmissionUnknown):
            await self.runner.run("side effect", None, None, 30, None)
        with self.assertRaises(CommandProtocolError):
            await self.runner.run("side effect", None, None, 30, None)
        self.assertEqual(len(self.business_posts()), 1)

    async def test_missing_id_is_not_success(self):
        self.mode = "missing"
        with self.assertRaises(CommandSubmissionUnknown):
            await self.runner.run("true", None, None, 30, None)

    async def test_ignored_range_is_not_silently_duplicated(self):
        self.bad_range = True
        with self.assertRaisesRegex(CommandProtocolError, "Range mismatch"):
            await self.runner.run("true", None, None, 30, None)
        self.assertEqual(len(self.business_posts()), 1)

    async def test_cancellation_interrupts_and_propagates(self):
        task = await self.start_running_command()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(sum(r.method == "DELETE" for r in self.requests), 1)
        self.assertFalse(self.runner.tasks)

    async def test_timeout_interrupts_before_returning_124(self):
        for status in (200, 500):
            with self.subTest(interrupt_status=status):
                self.running, self.interrupt_status = True, status
                self.requests.clear()
                result = await self.runner.run("sleep 100", None, None, 0.02, None)
                self.assertEqual(result[2], 124)
                self.assertEqual(sum(r.method == "DELETE" for r in self.requests), 1)
                self.assertFalse(self.runner.poisoned)

    async def test_128_native_environments_share_transport_and_survive_sibling_close(self):
        runners = [AsyncCommandRunner(self.env) for _ in range(127)]
        transport = self.runner.transport
        try:
            self.assertEqual(transport.users, 128)
            self.assertTrue(all(r.transport is transport for r in runners))
            await asyncio.gather(*(r.close() for r in runners))
            self.assertEqual(transport.users, 1)
            self.assertFalse(transport.control.is_closed)
            self.assertEqual((await self.runner.run("true", None, None, 30, None))[2], 7)
            await self.runner.close()
            self.assertTrue(transport.control.is_closed)
            self.assertTrue(transport.files.is_closed)
            # A later trial on the same event loop must get fresh clients.
            self.extra_runner = AsyncCommandRunner(self.env)
            self.assertIsNot(self.extra_runner.transport, transport)
            self.assertFalse(self.extra_runner.transport.control.is_closed)
        finally:
            await asyncio.gather(*(r.close() for r in runners))

    async def test_cancelled_close_finishes_recovery_and_releases_transport(self):
        recovering, release = asyncio.Event(), asyncio.Event()
        interrupt = self.runner._interrupt

        async def blocked_interrupt(state):
            recovering.set()
            await release.wait()
            await interrupt(state)

        with patch.object(self.runner, "_interrupt", blocked_interrupt):
            task = await self.start_running_command()
            closing = asyncio.create_task(self.runner.close())
            try:
                await asyncio.wait_for(recovering.wait(), 1)
                closing.cancel()
                await asyncio.sleep(0)
                closing.cancel()
                done, _ = await asyncio.wait([closing], timeout=.02)
                self.assertFalse(done, "shutdown abandoned command recovery")
            finally:
                release.set()
                await asyncio.gather(task, closing, return_exceptions=True)
        self.assertTrue(closing.cancelled())
        self.assertTrue(task.cancelled())
        self.assertTrue(self.runner.transport.control.is_closed)
        self.assertTrue(self.runner.transport.files.is_closed)
        self.assertEqual(self.runner.transport.users, 0)
        await asyncio.gather(self.runner.close(), self.runner.close())
        self.assertEqual(self.runner.transport.users, 0)
        self.extra_runner = AsyncCommandRunner(self.env)
        self.assertIsNot(self.extra_runner.transport, self.runner.transport)

    async def test_close_does_not_wait_for_startup_callers_reentrant_close(self):
        self.running = True

        async def startup():
            try:
                await self.runner.run("sleep 100", None, None, 300, None)
            finally:
                await self.runner.close()

        task = asyncio.create_task(startup())
        while not self.status_calls:
            await asyncio.sleep(0)
        closing = asyncio.create_task(self.runner.close())
        try:
            done, _ = await asyncio.wait([task, closing], timeout=1)
            self.assertEqual(len(done), 2, "startup cleanup and close deadlocked")
            self.assertTrue(task.cancelled())
            closing.result()
            self.assertTrue(self.runner.transport.control.is_closed)
        finally:
            task.cancel()
            closing.cancel()
            await asyncio.gather(task, closing, return_exceptions=True)

    async def test_repeated_cancellation_awaits_failed_recovery_and_retires_runner(self):
        recovering = asyncio.Event()
        release = asyncio.Event()

        async def interrupt(_state):
            recovering.set()
            await release.wait()
            raise CommandProtocolError("interrupt unavailable")

        with patch.object(self.runner, "_interrupt", interrupt):
            task = await self.start_running_command()
            try:
                task.cancel()
                await asyncio.wait_for(recovering.wait(), 1)
                task.cancel()
                done, _ = await asyncio.wait([task], timeout=.02)
                self.assertFalse(done, "caller abandoned recovery after repeated cancellation")
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(self.running)
                self.assertTrue(self.runner.poisoned)
                self.assertFalse(self.runner.tasks)
                with self.assertRaises(CommandProtocolError):
                    await self.runner.run("true", None, None, 30, None)
                self.assertEqual(len(self.business_posts()), 1)
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)

    async def test_slot_wait_can_be_cancelled_without_submission(self):
        with patch.object(self.runner.transport, "commands", asyncio.Semaphore(0)):
            task = asyncio.create_task(self.runner.run("true", None, None, 30, None))
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertFalse(self.business_posts())


if __name__ == "__main__":
    unittest.main()

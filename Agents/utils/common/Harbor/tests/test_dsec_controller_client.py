"""Test the asynchronous DSec Controller protocol client."""

from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

import httpx

HARBOR_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HARBOR_DIR))

from harbor_environment_adapters.providers.dsec_controller.client import (
    API_VERSION,
    DsecControllerClient,
)


class DsecControllerClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_requests_are_not_bounded_by_the_default_thread_pool(self):
        active = 0
        peak = 0
        release = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await release.wait()
            active -= 1
            return httpx.Response(
                200,
                request=request,
                json={
                    "api_version": API_VERSION,
                    "status": "ok",
                    "result": {"sandbox": {"state": "ready"}},
                },
            )

        transport = httpx.MockTransport(handler)
        async_client = httpx.AsyncClient(transport=transport)
        client = DsecControllerClient(
            "https://controller.example",
            client=async_client,
        )
        requests = [
            asyncio.create_task(client.sandbox_status(f"sandbox-{index}"))
            for index in range(128)
        ]
        for _ in range(100):
            if peak == len(requests):
                break
            await asyncio.sleep(0.01)
        release.set()
        results = await asyncio.gather(*requests)
        await client.close()

        self.assertEqual(peak, 128)
        self.assertTrue(all(result["state"] == "ready" for result in results))

    async def test_custom_headers_are_forwarded_on_requests(self):
        seen_backend = None

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal seen_backend
            seen_backend = request.headers.get("X-Backend")
            return httpx.Response(
                200,
                request=request,
                json={
                    "api_version": API_VERSION,
                    "status": "ok",
                    "result": {"sandbox": {"state": "ready"}},
                },
            )

        transport = httpx.MockTransport(handler)
        async_client = httpx.AsyncClient(transport=transport)
        client = DsecControllerClient(
            "http://10.100.184.182",
            allow_insecure_http=True,
            headers='{"X-Backend": "10.254.73.22:873"}',
            client=async_client,
        )
        result = await client.sandbox_status("sandbox-1")
        await client.close()

        self.assertEqual(result["state"], "ready")
        self.assertEqual(seen_backend, "10.254.73.22:873")


if __name__ == "__main__":
    unittest.main()

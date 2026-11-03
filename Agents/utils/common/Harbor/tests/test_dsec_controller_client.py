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


if __name__ == "__main__":
    unittest.main()

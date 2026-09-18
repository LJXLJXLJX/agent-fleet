"""Import-order regression guard.

`core.ecosystems` and `core.config` reference each other (config looks up validation hooks,
ecosystem modules raise ConfigError), so any entry point must be able to import either side
first without triggering a circular import. On 2026-09-17 this broke because of `core.config.__init__`'s
eager re-export when ecosystems was imported first (pytest did not expose it because of a
coincidental import ordering).
"""

from __future__ import annotations

import subprocess
import sys
import unittest


class ImportOrderTest(unittest.TestCase):
    def test_modules_importable_in_fresh_process(self) -> None:
        modules = (
            "artifact_cache_gateway.core.ecosystems",
            "artifact_cache_gateway.core.config",
            "artifact_cache_gateway.gateway.server",
            "artifact_cache_gateway.cli.main",
        )
        for module in modules:
            with self.subTest(module=module):
                result = subprocess.run(
                    [sys.executable, "-c", f"import {module}"],
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
                self.assertEqual(
                    result.returncode,
                    0,
                    msg=f"import {module} failed:\n{result.stderr}",
                )

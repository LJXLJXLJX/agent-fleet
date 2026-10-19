from __future__ import annotations

import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from Agents.utils.common.Harbor.harbor_environment_adapters.core.config import (
    resolve_backend_plan,
)

SITE_CUSTOMIZE = Path(__file__).resolve().parents[1] / "sitecustomize.py"


class QzSiteCustomizeTest(unittest.TestCase):
    def run_hook(
        self, environment_type: str, backend_plan: str = ""
    ) -> tuple[Mock, Mock]:
        patch_e2b_runtime = Mock()
        e2b_runtime = types.ModuleType("e2b_runtime")
        e2b_runtime.patch_e2b_runtime_from_env = patch_e2b_runtime
        patch_instruction = Mock()
        qz_task_instruction = types.ModuleType("qz_task_instruction")
        qz_task_instruction.patch_harbor_task_instruction = patch_instruction
        with (
            patch.object(sys, "path", [str(SITE_CUSTOMIZE.parent), *sys.path]),
            patch.dict(
                os.environ,
                {
                    "HARBOR_ENVIRONMENT_TYPE": environment_type,
                    "HARBOR_BACKEND_PLAN": backend_plan,
                    "QZ_SANDBOX_TEMPLATE_MAP": "/tmp/qz-map.json",
                },
                clear=True,
            ),
            patch.dict(
                sys.modules,
                {
                    "e2b_runtime": e2b_runtime,
                    "qz_task_instruction": qz_task_instruction,
                },
            ),
        ):
            spec = importlib.util.spec_from_file_location(
                "qz_sitecustomize_test",
                SITE_CUSTOMIZE,
            )
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        return patch_e2b_runtime, patch_instruction

    def test_instruction_patch_is_applied_for_qz(self) -> None:
        _, patch_instruction = self.run_hook(" QZ ")
        patch_instruction.assert_called_once_with()

    def test_instruction_patch_is_not_applied_for_docker(self) -> None:
        _, patch_instruction = self.run_hook("docker")
        patch_instruction.assert_not_called()

    def test_e2b_runtime_patch_is_applied_for_e2b(self) -> None:
        patch_e2b_runtime, _ = self.run_hook("e2b")
        patch_e2b_runtime.assert_called_once_with()

    def test_e2b_runtime_patch_is_applied_for_qz(self) -> None:
        patch_e2b_runtime, _ = self.run_hook(" QZ ")
        patch_e2b_runtime.assert_called_once_with()

    def test_e2b_runtime_patch_is_not_applied_for_docker(self) -> None:
        patch_e2b_runtime, _ = self.run_hook("docker")
        patch_e2b_runtime.assert_not_called()

    def test_hook_consumes_launcher_plan(self) -> None:
        plan = resolve_backend_plan({"HARBOR_ENVIRONMENT_TYPE": "qz"})
        patch_e2b_runtime, patch_instruction = self.run_hook("", plan.to_json())
        patch_e2b_runtime.assert_called_once_with()
        patch_instruction.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()

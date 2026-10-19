"""Harbor process startup hooks shared by every Agent Fleet agent."""

import os

from harbor_environment_adapters.core.config import runtime_backend_plan

environment_type = runtime_backend_plan(os.environ).environment_type.strip().lower()

if environment_type == "docker":
    from docker_runtime import patch_docker_runtime_from_env

    patch_docker_runtime_from_env()

if environment_type in {"e2b", "qz"}:
    from e2b_runtime import patch_e2b_runtime_from_env

    patch_e2b_runtime_from_env()

if environment_type == "qz" and os.environ.get(
    "QZ_SANDBOX_TEMPLATE_MAP", ""
).strip():
    from qz_task_instruction import patch_harbor_task_instruction

    patch_harbor_task_instruction()

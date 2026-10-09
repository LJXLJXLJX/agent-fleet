"""Compatibility import for the preserved qz legacy implementation."""

from harbor_environment_adapters.providers.e2b_series.qz_legacy.adapter import (
    QZ_DEFAULT_API_URL,
    QZ_DEFAULT_HOST_PREFIX,
    QZ_DEFAULT_SANDBOX_TIMEOUT_SEC,
    QZ_MAX_SANDBOX_TIMEOUT_SEC,
    QZ_TASK_INIT_TIMEOUT_SEC,
    QzSandboxEnvironment,
    apply_qz_environment,
    patch_envd_host,
    qz_sandbox_timeout_sec,
    sanitize_template_name,
)

__all__ = [
    "QZ_DEFAULT_API_URL",
    "QZ_DEFAULT_HOST_PREFIX",
    "QZ_DEFAULT_SANDBOX_TIMEOUT_SEC",
    "QZ_MAX_SANDBOX_TIMEOUT_SEC",
    "QZ_TASK_INIT_TIMEOUT_SEC",
    "QzSandboxEnvironment",
    "apply_qz_environment",
    "patch_envd_host",
    "qz_sandbox_timeout_sec",
    "sanitize_template_name",
]

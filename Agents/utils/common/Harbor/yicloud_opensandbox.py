"""Compatibility imports for the YiCloud-specific Harbor environment."""

from harbor_environment_adapters.providers.opensandbox_yicloud.adapter import (
    YiCloudOpenSandboxEnvironment,
)
from harbor_environment_adapters.providers.opensandbox_yicloud.client import (
    YiCloudSandboxReadyTimeoutError,
)
from harbor_environment_adapters.providers.opensandbox_yicloud.lifecycle import (
    _configure_auxiliary_pool,
)
from harbor_environment_adapters.providers.opensandbox_yicloud.services import (
    ServiceRuntime,
)

__all__ = [
    "ServiceRuntime",
    "YiCloudOpenSandboxEnvironment",
    "YiCloudSandboxReadyTimeoutError",
    "_configure_auxiliary_pool",
]

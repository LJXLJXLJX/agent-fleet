"""Core-layer shared exceptions (a leaf module: it must not import any other package module, so any import order stays cycle-free)."""


class ConfigError(ValueError):
    """Raised when source configuration is unsafe or invalid."""

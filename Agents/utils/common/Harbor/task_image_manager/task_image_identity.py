"""Identity from original task inputs, shared by cache and image preparation."""

from pathlib import Path

try:
    from harbor.environments.definition import environment_content_hash
except ImportError as exc:
    raise RuntimeError(
        "Harbor task image preparation requires the Harbor benchmark "
        "framework API harbor.environments.definition.environment_content_hash; "
        "install a supported Harbor runner version"
    ) from exc


def image_identity(environment_dir: Path, *, docker_image: str | None = None) -> str:
    """Return identity derived only from the original static task environment.

    Renderer output, runtime assets, mirrors, and every other build-time
    mutation are forbidden identity inputs. Any such dependency is a P0 bug.
    """
    return environment_content_hash(
        environment_dir,
        docker_image=docker_image,
        truncate=64,
    )

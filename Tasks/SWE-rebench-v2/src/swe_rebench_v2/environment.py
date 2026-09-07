from __future__ import annotations

from copy import deepcopy
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from swe_rebench_v2.instance_spec import InstanceSpec

# Environment rendering is adapted from the official SWE-rebench-V2 builder's
# combine.Dockerfile.j2 at commit c71902a8cf8d2b725f63d51f199f4d3e56f68d2d:
# https://github.com/SWE-rebench/SWE-rebench-V2/blob/c71902a8cf8d2b725f63d51f199f4d3e56f68d2d/combine.Dockerfile.j2

CLONE_REPOSITORY_OVERRIDES = {
    # The archived repository does not contain this dataset commit. Its history
    # was merged into Magistrala, which still serves the exact SHA.
    (
        "absmach/supermq",
        "1c0400d3a58409d4148d2f3cd7befd279dd97a42",
    ): "absmach/magistrala",
}

# The published dataset names this base ``php:8.3.16``, which selects the
# unextended PHP parent image.  The pinned upstream builder instead provides
# ``Dockerfile_php_8.3.16`` as ``php_8.3.16``; that image installs Git before
# the instance template's mandatory pre-install ``git clone``.  Keep the raw
# dataset metadata unchanged and correct only the generated build dependency.
BASE_IMAGE_NAME_OVERRIDES = {
    "php:8.3.16": "php_8.3.16",
}


def _resolved_base_image_name(spec: InstanceSpec) -> str:
    return BASE_IMAGE_NAME_OVERRIDES.get(spec.base_image_name, spec.base_image_name)


def resolve_base_image(spec: InstanceSpec, registry_prefix: str = "") -> str:
    image_name = _resolved_base_image_name(spec)
    prefix = registry_prefix.strip().rstrip("/")
    if not prefix:
        return image_name
    return f"{prefix}/{image_name}"


def render_environment_dockerfile(
    spec: InstanceSpec,
    template_path: Path,
    registry_prefix: str = "",
) -> str:
    raw = deepcopy(spec.raw)
    install_config = dict(raw.get("install_config") or {})
    install_config["image_name"] = _resolved_base_image_name(spec)
    install_config["install"] = list(spec.install_commands)
    raw["install_config"] = install_config
    raw["clone_repo"] = CLONE_REPOSITORY_OVERRIDES.get(
        (spec.repo, spec.base_commit), spec.repo
    )

    env = Environment(
        loader=FileSystemLoader(str(template_path.parent)),
        autoescape=False,
        keep_trailing_newline=True,
        undefined=StrictUndefined,
    )
    template = env.get_template(template_path.name)
    return template.render(
        spec=raw,
        base_image_registry=registry_prefix.strip().rstrip("/"),
        platform="linux/amd64",
    )

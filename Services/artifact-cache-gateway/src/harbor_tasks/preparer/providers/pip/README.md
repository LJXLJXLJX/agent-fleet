# providers/pip/

pip preparation-phase provider (package-ized from the former `providers/pip.py`; the external
path `artifact_cache_gateway.harbor_tasks.preparer.providers.pip` is unchanged, and `__init__.py`
re-exports `PipProvider, environment_for_image, environment_from_identity, CommandRunner`).

- `environments.py`: image/distro Python → `PipEnvironment` mapping (`_DISTRO_PYTHONS`).
- `resolver.py`: `resolver_script.py` (the in-container resolver script resource, loaded via
  importlib.resources, byte-identical to the former `_RESOLVER_SCRIPT`), `CommandRunner`/
  `_run_container`, and the `_TUNA_SIMPLE`/`_PYPI_FILES_ORIGIN` constants.
- `provider.py`: `PipProvider` (probe and warm-up).
- `resolver_script.py`: a package_data resource (the Python script executed inside the docker
  container; it runs in its own in-container namespace, so host-side linting does not apply —
  see the pyproject per-file-ignores).

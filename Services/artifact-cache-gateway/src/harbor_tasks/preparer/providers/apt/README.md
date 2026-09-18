# providers/apt/

APT preparation-phase provider (package-ized from the former `providers/apt.py`; the external
path `artifact_cache_gateway.harbor_tasks.preparer.providers.apt` is unchanged, and `__init__.py`
re-exports `AptProvider, environment_for_image, environment_from_identity, CommandRunner`).

- `environments.py`: image → `AptEnvironment` mapping (`_IMAGE_ENVIRONMENTS`).
- `resolver.py`: `resolver_script.sh` (the in-container resolver script resource, loaded via
  importlib.resources, byte-identical to the former `_RESOLVER_SCRIPT`), `CommandRunner`/
  `_run_container`, and package-name/Provides matching helpers.
- `repository.py`: `_RepositoryMixin` — gateway source derivation, deb822 record reading,
  repository-context parsing.
- `probe.py`: `_ProbeMixin` — in-container resolution and `probe`.
- `warm.py`: `_WarmMixin(_RepositoryMixin)` — `warm` and cache pull.
- `provider.py`: `AptProvider(_ProbeMixin, _WarmMixin)`.
- `resolver_script.sh`: a package_data resource (the shell script executed inside the docker
  container).

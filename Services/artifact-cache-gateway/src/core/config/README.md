# core/config/

Source configuration package (split out of the former `core/config.py`; the module path
`artifact_cache_gateway.core.config` remains usable, and `__init__.py` re-exports 6 public names).

- `errors.py`: `ConfigError`.
- The ecosystem path regexes and the `_valid_*` validators have moved into `../ecosystems/`
  (this package calls them via `from ..ecosystems import handler_for_kind` in
  `SourceConfig.build_url` and `GatewayConfig.__post_init__`); `_NPM_PATH_COMPONENT` and the
  `_PYTORCH_*` constants are imported from `../ecosystems/node.py` and `../ecosystems/pip.py`
  for use by from_dict/fetch_candidates. `validators.py` has been removed.
- `_shared.py`: `_origin` (scheme://netloc lowercase normalization).
- `upstream.py`: the `_UPSTREAM_LABEL`/`_PROXY_MODES` constants, `UpstreamConfig` and
  `_optional_positive`/`_upstream_policy`/`_upstream_from_dict`.
- `source.py`: the `_SOURCE_NAME`/`_SOURCE_KINDS`/`_SOURCE_ECOSYSTEMS`/`_CONFIG_UPDATE_POLICIES`
  constants, `_safe_relative_path`/`_path_list`, `SourceConfig` (kind constraints,
  build_url/fetch_candidates).
- `gateway.py`: `GatewayConfig`, `default_config`, `config_from_document`, `load_config`.

# core/

Foundation layer: does not import other business subpackages (storage/gateway/harbor_tasks/cli/ui)
at runtime, and is imported by the upper layers. Note that the hooks under `ecosystems/` still have
`TYPE_CHECKING` references and field-semantics dependencies on upper-layer types such as
`SourceConfig`/`CacheResult`/`CacheEntry` (see that directory's README), so it is not the strict
"zero gateway import" boundary that `storage/` enforces; such type references are used only for
static type checking and do not constitute a runtime reverse dependency.

- `config/`: source configuration package (split out of the former `config.py`) — `GatewayConfig` /
  `SourceConfig` / `UpstreamConfig` and source configuration loading (definitions of 9 ecosystems,
  13 source kinds, and the `_valid_*` path validators). The external path
  `artifact_cache_gateway.core.config` is unchanged.
- `source_naming.py`: source naming and routing derivation (`parse_*_route`,
  `apt_gateway_path`, `download_gateway_path`, frozen download naming, etc.); part of the
  storage compatibility red lines, modification forbidden.
- `logging_utils.py`: structured event logging helpers (`emit_event`).
- `local_env.py`: preparer local environment detection (`load_local_env`, `parse_local_env`).
- `ecosystems/`: the single ecosystem plugin directory (merged from `kinds/` +
  `gateway/ecosystems/`, finalized 2026-09-17); `EcosystemHandler` hook registry, pure functions;
  see that directory's README for guidance on adding an ecosystem.

# gateway/engine/

Gateway caching engine (package-ized from the former `gateway/engine.py`; the external path
`artifact_cache_gateway.gateway.engine` is unchanged, and `__init__.py` re-exports
`Gateway, CacheResult`).

- `result.py`: `CacheResult`.
- `_common.py`: single-point definition of `_HIT_RATE_LOGGER`.
- `_stats.py`: `RequestStats` — a standalone request-stats component (since 2026-09-17 it
  replaces the former mixin via composition). It explicitly receives a few dependencies (config,
  storage, the `source_display_name` function, etc.) instead of holding the full Gateway; it owns
  its own count state, stats lock, checkpoint thread, and shutdown flow, and exposes
  `record`/`record_git_mirror_state`/`record_upstream_attempts`/`stats`/`close`.
  The persistence sessions and the schema 1/2/3/4 contracts still live in `storage/request_stats.py`.
- `_inventory.py`: `_InventoryMixin` — freshness, index entries, `inventory_document`,
  `backfill_inventory`.
- `_failures.py`: `_FailureMixin` — `_record_failure`/`recent_failures`.
- `core.py`: `Gateway(_InventoryMixin, _FailureMixin)` composes and holds `RequestStats`
  (`self._request_stats`), keeps the external `stats()`/`record_git_mirror_state()`/`close()`
  contract and delegates to the component; `_record`/`_record_upstream_attempts` are thin
  delegations. `__init__` (the parsing pipeline and the publish policy
  frozen/transparent-download, `_should_publish`) is a compatibility red line.

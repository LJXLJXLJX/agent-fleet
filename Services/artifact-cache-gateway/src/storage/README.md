# storage/

Storage contract layer: depends only on `core` (leaf `config/errors`/`exceptions`, no gateway
dependency, guarded statically and in an independent process by `tests/test_storage_contract.py`,
decoupled in 2026-09-17 Phase 3). Holds the cache entry, storage protocol, inventory records,
and request-stats session persistence schemas, plus the two types of authoritative storage
implementations.

- `base.py`: `CacheEntry`, the `Storage` protocol, `StorageError`.
- `inventory.py`: `CacheObject`/`InventoryListing` and their validation and serialization (the
  storage-contract part of the former `gateway/inventory.py` moved here; `gateway/inventory.py`
  keeps only the `cache_object` construction that depends on source/ecosystem hooks).
- `request_stats.py`: the `RequestStatsSession` dataclass, the schema 1/2/3/4 validation
  functions, count-structure helpers, and constants (the persistence part of the former
  `gateway/request_stats.py` moved here; the ecosystem→stats module-name grouping stays in
  `gateway/request_stats.py`).
- `gpfs.py`: `FileStorage` (explicit dev/test storage, laying out blob/metadata/inventory by the
  url sha256).
- `s3.py`: `S3Storage` and `S3Settings` (S3 is the authoritative store for HTTP artifacts).

The key-computation functions (`FileStorage.url_key`/`blob_path`/`metadata_path`, and the
`S3Storage` `blob_key`/`metadata_key`/`inventory_key`/`inventory_prefix`) are storage
compatibility red lines; modification is forbidden.

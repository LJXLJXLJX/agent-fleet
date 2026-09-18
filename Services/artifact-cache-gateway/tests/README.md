# tests/

pytest test suite. You must run `pip3 install --user -e .` first (the package maps to `src/` via
`package_dir`).

- `test_source_naming.py`: source naming and route parsing (golden coverage of the storage
  compatibility red lines).
- `test_s3_storage.py`: S3Storage key computation and storage semantics (golden coverage).
- `test_gateway.py`: gateway caching/publishing behavior.
- `test_git_mirror.py`: Git mirror behavior.
- `test_dataset_preparation.py`: dataset analyzer / preparer tooling.
- `test_extracted_resources.py`: sha256 snapshot guard for the resolver scripts and webui static
  assets.
- `test_storage_contract.py`: storage layering guard (AST static assertions of no gateway import
  plus an independent-process serialization round-trip that never loads gateway).
- `test_request_stats_component.py`: key behavior of the standalone request-stats component
  (`RequestStats` in `engine/_stats.py`) — constructed directly without going through
  Gateway/Fetcher/server, covering record/query/persist/close, historical session recovery,
  persistence-failure states, thread exit, and concurrency without lost counts.
- `test_import_order.py`: fresh-process import-order guard (`core.ecosystems` ↔ `core.config` has
  no cycle).
- `fixtures/`: test data.

The golden tests for storage key computation, routing, and naming are a regression red line:
refactoring may only move code, and these tests must pass unchanged.

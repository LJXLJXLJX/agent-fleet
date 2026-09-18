# scripts/

Development and operations helper scripts.

- `start-gateway.sh`: wrapper that starts the Gateway service process locally/in a container.
- `migrate_source_names.py`: one-time data migration for a historical source-naming change.
  The storage layout is determined by pure functions such as
  `url_key`/`blob_path`/`metadata_path`/`blob_key`, so no new data migration should normally be
  needed; if one appears necessary, first go back to the key-computation functions in `src/`.

When adding a script, register its purpose and invocation here.
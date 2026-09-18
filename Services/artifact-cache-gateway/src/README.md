# src/

Source code of the `artifact_cache_gateway` Python package (`package_dir` in
`setup.cfg` maps the package name to this directory).

## Layered structure

Dependencies may only point downward: `cli → harbor_tasks → gateway → storage → core`.
The UI is a resource provider mounted by `gateway/server.py`; its actual direction is
`gateway → ui` (ui does not depend back on gateway).

| Directory | Layer | Responsibility |
| --- | --- | --- |
| `core/` | Foundation layer | source naming/routing, logging, local environment detection; `config/` configuration models and loading; `ecosystems/` the single ecosystem plugin directory (all hooks such as kind validation/rewriting/inventory fields, finalized 2026-09-17) |
| `storage/` | Storage contract layer | `base.py` protocol and exceptions, `inventory.py`/`request_stats.py` persistence data contracts (no gateway dependency), `gpfs.py`/`s3.py` storage implementations |
| `gateway/` | Gateway runtime | Generic HTTP cache proxy engine (`engine/`/`fetcher.py`/`server.py`/`rewrites.py`/`git_http.py`; ecosystem hooks look into `core/ecosystems/`), inventory, request stats, status; `services/` only holds services with standalone implementations (apt/git/image) |
| `harbor_tasks/` | Dataset tooling | `analyzer/` dataset dependency analysis, `preparer/` preparation plan generation and warm-up |
| `ui/` | Web UI | Operations web interface |
| `cli/` | Entry point | Implementation of the four console commands (`main.py`/`inventory.py`/`stats.py`) |

Each subdirectory has its own README describing module details.

## Compatibility red lines

Storage key computation (`url_key`/`blob_path`/`metadata_path`/`blob_key`/`metadata_key`/`inventory_key`),
routing naming (all functions in `core/source_naming.py`, `SourceConfig.build_url`), and the publish
policy (frozen/transparent-download and `_should_publish`) determine the layout of stored data:
**only import paths may be changed as-is; any behavioral change is forbidden**. The corresponding
golden tests live under `tests/`.

# cli/

Entry point CLI funnel (`__main__.py` and the console_scripts point here).
Dependency direction: cli → all other layers.

- `main.py`: the `artifact-cache-gateway` / `python -m artifact_cache_gateway` server entry
  point (formerly `cli.py`).
- `inventory.py`: `artifact-cache-inventory` (formerly `inventory_cli.py`).
- `stats.py`: `artifact-cache-stats` (formerly `stats_cli.py`).

The `artifact-cache-prepare` entry point lives in `harbor_tasks/preparer/cli.py` (the preparation
tooling belongs to harbor_tasks).

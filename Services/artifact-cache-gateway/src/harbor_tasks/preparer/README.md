# harbor_tasks/preparer/

Preparation-plan execution (the `preparation/` directory moved wholesale + `prepare_cli.py` →
`cli.py`).

- `cli.py`: the `artifact-cache-prepare` entry point (`main` dispatches the subcommands).
- `_defaults.py`: default path constants.
- `parser.py`: argument parsing (`parser` construction and shared argument groups).
- `settings.py`: run-settings loading and analysis-plan generation.
- `commands.py`: the core subcommands `analyze` / `mirror-images` / `prepare`.
- `warm_commands.py`: the maintenance subcommands `warm-*` / `configure-*` family.
- `models.py`: probe/warm data models and `ProbeSettings`.
- `orchestrator.py`: `probe_packages` / `warm_packages`.
- `report.py`: probe/warm report writing.
- `providers/`: apt/pip/npm package probe and warm-up providers (used only by the preparer).
- `direct_download/`: v1 exact-object and v2 frozen-origin download plans with cache
  refresh/warm-up.

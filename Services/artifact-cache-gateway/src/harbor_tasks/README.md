# harbor_tasks/

Harbor task tooling: dataset analysis (analyzer) and preparation-plan execution (preparer).
Dependency direction: harbor_tasks → gateway → storage → core.

- `analyzer/`: moved in from `dataset_analyzer.py` (a single file whose content is
  `__init__.py`); analyzes dataset images, package dependencies, direct downloads, and GitHub
  repos, producing an auditable preparation plan.
- `preparer/`: the `preparation/` directory moved wholesale + `prepare_cli.py` → `cli.py` (the
  `artifact-cache-prepare` entry point); `providers/` (apt/pip/npm probing and warm-up) and
  `direct_download/` serve only the preparer and are a separate thing from the server-side
  ecosystem adaptations (`gateway/services/`).

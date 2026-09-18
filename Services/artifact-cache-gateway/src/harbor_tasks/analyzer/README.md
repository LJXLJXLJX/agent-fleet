# harbor_tasks/analyzer/

Dataset analyzer (split out of the former `dataset_analyzer.py`; the external path
`artifact_cache_gateway.harbor_tasks.analyzer` is unchanged, and `__init__.py` re-exports all
public names).

Pipeline layers (dependency direction: `cli → each layer → models/shell`, orchestrated by
`__init__`):

- `models.py`: data-model dataclasses (Aggregate/Install/AptDependency/ExternalBuildInput/
  ExternalBuildInputIssue/BuildContext/ScanSource) plus `_stable_id`/`_resolution_environment_id`
  and `NODE_REGISTRY_MANAGERS`.
- `shell.py`: shell lexical core — heredocs (`unquoted_heredoc_matches`/`strip_heredocs`),
  logical lines (`logical_shell_lines`), command splitting (`shell_commands`/`command_start`),
  and lexical constants.
- `dockerfile.py`: Dockerfile parsing (`docker_instructions`/`from_image`/`from_stage`),
  task discovery (`discover_tasks`/`read_text`), and scan-source/build-context mapping
  (`docker_scan_sources`/`shell_contexts`).
- `apt.py`: static recognition of `apt_dependencies` / `apt_repository_declarations`.
- `external.py`: recognition of external build inputs (download URLs, git clone targets) and
  issue reporting.
- `packages.py`: recognition of package-manager install commands (`identify_install`), option
  parsing, requirements expansion, and shell script collection.
- `report.py`: `markdown` report and `write_outputs` persistence (lazily imported by the
  preparer).
- `cli.py`: `parse_args`/`main` + `__main__` guard (the module docstring is the CLI description).
- `__init__.py`: pipeline orchestration `analyze()` + re-export of all public names.

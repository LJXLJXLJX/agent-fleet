# AGENTS.md — Artifact Cache Gateway

## Constraints

1. **Every submodule folder must have its own README**: every directory in the repository with
   meaningful content (such as each subpackage under `src/`, `cli/`, `tests/`, `deploy/`,
   `scripts/`, `docs/`, `config/`) must contain a `README.md` describing the directory's
   responsibilities, main modules and external contracts. Adding a subdirectory requires adding
   its README in the same change; moving/deleting a directory moves/deletes its README too.
2. **Every commit must keep `STRUCTS.md` in sync**: when files or directories are added, moved,
   renamed or deleted, update `STRUCTS.md`'s directory tree and the corresponding file-purpose
   descriptions in the same commit; verify they match before committing.
3. Real credentials go only in Git-ignored local secret files, environment variables or platform
   secrets; they must not be written into committed config, code, documentation, command
   examples, logs or artifacts.
4. Minimal changes: no unrequested refactors or "drive-by cleanups"; compatibility red-line
   functions such as storage key computation, route naming and publish policy may only be moved
   verbatim, see `wip/artifact-cache-gateway-refactoring-target.zh.md` (workspace).
5. Introduce no new dependencies; do not change the public HTTP interface or console command
   names.
6. Preserve the user's existing changes, do not overwrite unrelated files, and do not hand-edit
   generated artifacts such as `build/`, `*.egg-info`.

## Common commands

```bash
pip3 install --user -e .   # The package maps via package_dir to src/; install required before testing
python3 -m pytest tests/   # Full test run
```

See `docs/developer-guide.md` for details.
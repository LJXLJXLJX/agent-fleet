# config/

Server-side source configuration directory.

- `sources.json`: the Artifact Cache Gateway source manifest (9 ecosystems, 13 source kinds),
  defining each source's upstream address, ecosystem, kind, path validation and publish policy.
  Loaded by the server; changes must follow the review process in `docs/operator-guide.md`.

This directory may contain Git-ignored local override configs (such as `*.local.json`); real
credentials must not be committed.
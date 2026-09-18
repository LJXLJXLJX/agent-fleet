# gateway/

Gateway runtime (server side). The generic HTTP cache proxy trunk is driven by `SourceConfig`;
this directory carries the core flow, while the three services with standalone implementations
(apt / git / OCI image) live under `services/`.

- `engine/`: the `Gateway` engine package (cache-hit decisions, origin fetching, publish policy;
  the frozen/transparent-download publish policy and `_should_publish` are storage compatibility
  red lines, modification forbidden).
- `fetcher.py`: upstream fetching (retry, proxy, Range).
- `server.py`: HTTP server (routing, status and health checks; response rewriting and Git request
  body parsing have been split out).
- `rewrites.py`: generic HTML index rewriting trunk; npm/dart/julia/cargo/rustup kind-specialized
  rewriting lives in `core/ecosystems/` (invoked via the `rewrite_matches`/`rewrite` hooks).
- `git_http.py`: Git Smart HTTP request body parsing (chunked/gzip).
- `inventory.py`: inventory objects and paginated listing (one-way dependency on `storage/base.py`).
- `request_stats.py`: only does ecosystem→stats module-name grouping and the `empty_modules`
  combination; `RequestStatsSession`/schema validation/count structures live in
  `../storage/request_stats.py`.
- `status.py`: status document assembly.
- `services/`: apt (`apt_gateway`), git (`git_mirror`), OCI image (`image_mirror`).

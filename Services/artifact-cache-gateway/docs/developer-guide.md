# Developer Guide

This document explains how to understand, modify, and verify the Artifact Cache Gateway. It starts from the user-visible contract and then goes into the module implementation; do not turn a one-off dataset experiment, a platform address, or a temporary construction step into general product behavior.

## Local Environment

You need Python 3.10+ and Git. Some dataset-preparation features also require Docker, Skopeo, and access to the target Registry.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

Use explicit file storage for local runs, so that a development directory is never mistaken for a production fallback:

```bash
artifact-cache-gateway \
  --storage file \
  --cache-dir /tmp/artifact-cache \
  --config config/sources.json \
  --cache-mode all \
  --host 127.0.0.1 \
  --port 8080
```

Run the tests:

```bash
python -m unittest discover -s tests -v
```

Changes that touch the HTTP server, Git Smart HTTP, Range, S3 storage, or the source parser should run the full test suite. If you only change a local provider, you can run the relevant test file first and then the full suite.

## Code Structure

`setup.cfg` maps `src/` to the Python package `artifact_cache_gateway`, organized in layers: `cli → harbor_tasks → gateway → storage → core`. The UI is a resource provider mounted by `gateway/server.py`; the actual dependency direction is `gateway → ui` (the ui never depends back on gateway). See `src/README.md` for details.

| Module | Responsibility |
| --- | --- |
| `cli/main.py` | Gateway CLI, storage and server assembly |
| `gateway/server.py` | HTTP routes, response rewriting, Git Smart HTTP CGI bridge |
| `core/config/` | Source schema, path/origin/protocol validation |
| `gateway/engine/` | Cache lookup, refresh, stats, and failure recording (`core.py` composes the standalone `RequestStats` component; see `engine/_stats.py` and `engine/README.md`) |
| `gateway/fetcher.py` | Fixed upstream chain, proxy policy, download and redirect validation |
| `storage/base.py` / `storage/gpfs.py` | Storage protocol/errors and the development FileStorage |
| `storage/s3.py` | S3 metadata, blobs, inventory, and request sessions |
| `gateway/inventory.py` | Artifact ecosystem, package, version, and object parsing |
| `gateway/status.py` / `ui/webui.py` | Redacted status model and read-only UI |
| `gateway/services/git.py` | GitHub canonicalization, bare mirror, pre-heating, and submodule discovery |
| `harbor_tasks/analyzer/` | Static analysis and plan generation for datasets |
| `harbor_tasks/preparer/cli.py` | Dataset preparation command orchestration |
| `gateway/services/image.py` | OCI plans, target filtering, and Skopeo execution |
| `gateway/services/apt.py` | Compiling APT candidates into Gateway sources |
| `harbor_tasks/preparer/direct_download/` | Static URL review, plans, pre-heating, and refresh |
| `harbor_tasks/preparer/providers/` | APT, pip, and npm resolution environments, probes, and pre-heating |

Tests are organized around four primary boundaries: Gateway/config, S3, Git mirror, and dataset preparation.

## HTTP Artifact Data Flow

A single `/v1/cache/<source>/<path>` request:

1. `gateway/server.py` parses the source and relative path.
2. `core/config/` validates the path, query, and protocol format against the source kind and builds the fixed upstream candidates.
3. `gateway/engine/` reads URL metadata; fresh objects are served directly from storage.
4. A MISS or expired object is handed to `gateway/fetcher.py`, which tries the fixed upstreams in order.
5. The download is written to a temp file first, validating status, length, redirect, and throughput.
6. When the cache mode allows it, storage first publishes the content-addressed blob, then atomically updates the URL metadata and inventory.
7. The server applies controlled URL rewriting or validation to metadata for PyPI/npm/Cargo/Dart/Julia registries before responding.

The cache key is derived from the canonical upstream URL, never from an arbitrary caller-supplied string. Response rewriting must stay within the configured Gateway source.

## Core Invariants

### Never Become an Arbitrary URL Proxy

- upstream origins can only come from source config or an audited plan;
- paths must pass protocol validation, prefix, filename prefix, or exact-path checks;
- redirects must stay within the exact origin and path policy of the current upstream;
- query is denied by default and enabled explicitly only when the protocol actually needs it;
- logs and status must not leak full URLs, queries, proxy addresses, or credentials.

### S3 Is the Production Authoritative Store

- a failed S3 startup check must exit with an error;
- production mode must not auto-switch to FileStorage;
- blobs are stored by SHA-256 content address;
- metadata and inventory are updated only after a complete object is published;
- upstream staging files are cleanable and must not be the only cached copy.

### Failures Must Not Break Existing Objects

- on a failed refresh, keep the current metadata and blob;
- temp downloads, Git clones, and publish paths must be cleanable;
- the Git mirror can `os.replace` into the official path only after passing bare-repository validation;
- concurrent fills of the same object must merge or race safely and never produce partial files.

### Policy Must Be Observable but Redacted

`/v1/status` may show source name, kind, proxy mode, fallback label, timeout, and stats; it must not expose upstream URLs or secrets. Request session schema 4 stores per-module counts, cache fills, and upstream attempts under `modules`, plus the same per-source metrics under each module's `sources`; the Git mirror always uses `git_clone.sources.github`. When reading schema 1/2, keep the global totals and mark modules that cannot be attributed as `unclassified`; when reading schema 3, mark the gap between module totals and attributable sources as `historical_unattributed`, never placing it into `sources`. Error logs record the stage and candidate index, not sensitive input.

## Adding or Modifying a Source

Prefer expressing new needs in an existing kind:

1. Choose the correct `kind` and `ecosystem`.
2. Configure a fixed primary and the necessary ordered fallbacks.
3. Set `direct` or `configured` for each upstream.
4. Use the narrowest path prefixes or exact paths.
5. Add only redirect origins you have actually validated.
6. Configure a TTL for mutable metadata and keep immutable semantics for versioned artifacts.
7. Update `config_updated_at` and explicitly choose `manual` or an `expires` policy with `config_expires_at`.
8. Add tests for positive cases, path traversal, query, redirect boundary, and rewrite.

Generated source names must be readable host/path slugs with no URL hash. Hashes used to only reduce name collision probability and never indicated freshness or expiry; on a collision the system should now fail explicitly and let the admin determine the name. Config update time and review deadlines are independent metadata — they do not participate in the cache key and do not change artifact TTL.

If an existing kind cannot safely express the protocol, add a new protocol-validation function rather than loosening a source into a broad `generic`.

The Dart Pub `archive_url` rewrite is fail-closed: only the `dart-pub-archives` primary/fallback base URLs can map to a valid archive path; unknown absolute URLs are not returned to clients. Julia `/registries` likewise accepts only valid relative registry objects, or the equivalent absolute address from a source-audited upstream/redirect origin. The Pkg Server diff and bundle endpoints are out of scope for the current implementation.

Config examples should use placeholders and must not embed internal hostnames, personal directories, or credentials.

## Adding a New Package Provider

Providers live in `harbor_tasks/preparer/providers/` and should keep these boundaries:

- the analyzer produces the consumption context and resolution environment;
- the resolver produces fixed artifacts in an explicit environment;
- the probe records the availability and latency of domestic sources without directly changing Gateway policy;
- the warmer only consumes Gateway routes that are audited or determined by the provider;
- deduplicated artifacts retain all their consumers;
- unsupported environments must be reported explicitly rather than silently treated as success.

APT, pip, and npm have different environment identities; do not abstract them into a generic function that resolves only by package name.

## Modifying the Git Mirror

The Git route's runtime admission is a protocol boundary, not a dataset allowlist:

- the canonical upstream is always public `github.com`;
- both the URL and the route can only yield a safe owner/repository;
- the plan is used only for pre-heating, submodule discovery of pinned commits, and coverage;
- an unplanned repository can be registered and filled on its first online request;
- by default `warm-git` can only traverse planned + derived repos and must not expand bulk pre-heating just because new repositories were observed at runtime;
- the legacy `.derived-allowlist.json` format must stay read-compatible unless a migration is provided.

Git HTTP changes must cover at least: cloning unplanned repositories, dangerous-path rejection, read-only upload-pack, existing-mirror HITs, submodule manifests, and concurrent publication.

## Modifying Dataset Analysis

Static analysis results must state their source and environment. Common principles:

- a Dockerfile stage inherits its own base image;
- a referenced script inherits only the stage that references it;
- requirements, heredocs, and explicit install commands keep their source location;
- dynamic variables are marked unresolved and not guessed;
- direct URLs are stripped of credentials and query before being written to disk;
- output plans are separated from executed results; write operations require `--execute`.

Newly recognized rules should add a small dataset fixture that tests both the discovery and adjacent syntax that must not be discovered.

## Testing Strategy

### Unit and Local Integration

```bash
python -m unittest tests.test_gateway -v
python -m unittest tests.test_s3_storage -v
python -m unittest tests.test_git_mirror -v
python -m unittest tests.test_dataset_preparation -v
```

Tests use a local HTTP server, temp directories, and a fake S3 client; they must not depend on the real public network or production credentials.

### External Validation

Validation that involves a real Registry, S3, proxy, GPFS, or Sandbox must be reported separately:

- the platform, date, and actual entry point;
- the scope of components validated;
- the output directory or redacted results;
- external integrations not validated;
- cleanup results and secret scans.

A single package smoke cannot substitute for Git, OCI image, or final task image prebuild validation.

## Documentation Rules

Documentation describes currently available behavior and does not record construction schedules, stale experiment figures, or TODO lists. Historical design discussions stay in issues, PRs, or the version history.

When you change a user-visible contract, update in the same change:

- the capabilities and boundaries in the README;
- the corresponding role guide;
- the CLI `--help`;
- comments in example config;
- the tests covering that contract.

Command examples must use placeholders for unknown endpoints, addresses, and secrets. Long-running commands should state a sane concurrency, output location, and how to observe progress.

## Pre-Commit Checks

```bash
git diff --check
python -m unittest discover -s tests -v
```

Also confirm you are not committing `config.local.env`, authfiles, S3 keys, Registry passwords, proxy nodes, or real platform Secrets.
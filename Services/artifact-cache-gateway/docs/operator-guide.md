# Operator Guide

This document describes the current operating contract for people who deploy and maintain the Artifact Cache Gateway. The platform address, accounts, proxy nodes, and secrets must be supplied by the actual environment and must not be copied from historical documentation.

## Production Topology

Production mode consists of three persistence boundaries:

```text
Gateway process
  ├─ S3 bucket/prefix       HTTP artifacts, metadata, inventory, request sessions
  ├─ local work directory  upstream staging files, discardable
  └─ Git mirror root       bare Git repositories, needs a shared or persistent filesystem
```

S3 is the single authoritative store for HTTP artifacts. On startup the Gateway verifies bucket authentication and connectivity; if that fails it does not silently fall back to local files. The Git mirror does not write to S3.

The service itself has no authentication. The production ingress should be confined to a trusted internal network, or placed behind a reverse proxy that provides authentication, rate limiting, and auditing.

## Required Configuration

Start from a template:

```bash
cp config.example.env config.local.env
chmod 600 config.local.env
```

S3 mode requires:

| Variable | Meaning |
| --- | --- |
| `ARTIFACT_CACHE_S3_ENDPOINT` | HTTP(S) endpoint without path/query/fragment |
| `ARTIFACT_CACHE_S3_REGION` | S3 region |
| `ARTIFACT_CACHE_S3_BUCKET` | An existing bucket the current credentials can access |
| `ARTIFACT_CACHE_S3_PREFIX` | A secure key prefix reserved exclusively for the Gateway |
| `ARTIFACT_CACHE_S3_ACCESS_KEY_ID` | Access key ID |
| `ARTIFACT_CACHE_S3_SECRET_ACCESS_KEY` | Access secret |
| `ARTIFACT_CACHE_S3_SESSION_TOKEN` | Optional session token |

Runtime policies:

| Variable | Default | Description |
| --- | --- | --- |
| `ARTIFACT_CACHE_STORAGE` | `s3` | Keep `s3` in production; `file` is for development only |
| `ARTIFACT_CACHE_DIR` | `/data/artifact-cache` | Staging directory for S3 upstream fetches |
| `ARTIFACT_CACHE_CONFIG` | Built-in config | Path to the source JSON |
| `ARTIFACT_CACHE_UPSTREAM_PROXY` | Unset | Only used by `configured` upstreams and Git fill |
| `ARTIFACT_CACHE_CACHE_MODE` | `proxy-only` | `proxy-only` or `all` |
| `ARTIFACT_CACHE_DOWNLOAD_PLAN_DIR` | Unset | Direct-download plan directory merged at startup |
| `ARTIFACT_CACHE_GIT_MIRROR_PLAN` | Unset | Enables Git routing and provides a pre-heat/coverage manifest |
| `ARTIFACT_CACHE_GIT_MIRROR_ROOT` | Platform-default GPFS path | Bare mirror root directory |

The S3 client is fixed to path-style addressing and SigV4 and explicitly ignores the shell proxy. The HTTP connection pool scales to 64 per concurrent Gateway reader; transient S3 `get_object` errors on a cache hit are retried twice with short backoff, but deterministic errors such as 404 and 403 are not retried, and a failed cache read never bypasses the cache back to an upstream fetch. Real secrets are kept only in platform Secrets, environment variables, or Git-ignored local files.

## Startup Methods

### Standalone Container

`scripts/start-gateway.sh` reads `config.local.env` from the repo root, builds or reuses the local image, starts the service on the host network, and waits for `/healthz`:

```bash
scripts/start-gateway.sh --build
```

Use `--build` after changing code or the Dockerfile; you can simply restart after editing only environment variables or `config/sources.json`. The script binds `127.0.0.1:8080` by default and does not enable the Git mirror or dataset download plans.

### Compose

```bash
docker compose -f deploy/compose.yaml up --build -d
```

The Compose example suits a basic S3 Gateway. If you enable the Git mirror, you must additionally mount a persistent mirror root and pass the plan path; do not place Git data in the container's ephemeral layer.

### Hosted Platform Image

`deploy/Dockerfile.hosted` adds a pinned-version mihomo and a two-process supervisor inside the Gateway image. The runtime directory defaults to:

```text
<RUNTIME_CONFIG_DIR>/
  config.local.env
  mihomo.yaml
  sources.json
  git-mirror-plan.json       # optional; enables the Git route when present
  download-plans/            # optional; one JSON plan per dataset
```

Platform settings:

```bash
ARTIFACT_CACHE_RUNTIME_CONFIG_DIR=<RUNTIME_CONFIG_DIR>
```

The entrypoint copies the sensitive runtime config into a user-scoped read-only temp directory inside the container, validates the S3 variables, starts mihomo, and then starts the Gateway. `download-plans/*.json` are merged at startup; you must restart the service after changing sources or plans.

## Source Policy

Each source in `config/sources.json` has a stable name and a fixed upstream. Clients are routed as:

```text
/v1/cache/<source-name>/<relative-path>
```

Core fields:

| Field | Purpose |
| --- | --- |
| `name` | Stable source name in the URL; only lowercase letters, digits, and hyphens are allowed |
| `kind` | Determines additional protocol validation and response rewriting |
| `ecosystem` | Web UI and inventory classification |
| `base_url` | Fixed primary upstream |
| `proxy_mode` | `direct` or `configured` |
| `fallback_upstreams` | Ordered, fixed backup upstreams |
| `allowed_path_prefixes` | Allowed path prefixes |
| `allowed_exact_paths` | Allowed exact objects; `@root` denotes the origin root object |
| `allowed_filename_prefixes` | Controlled prefixes that apply only to single-level filenames |
| `allowed_redirect_origins` | Exact origins redirects may reach |
| `allow_query` | Whether query is accepted; statically `false` for typical static sources |
| `mutable_path_prefixes` | Paths needing TTL/conditional-refresh semantics |
| `metadata_ttl_seconds` | TTL for mutable metadata |
| `config_updated_at` | `YYYY-MM-DD` date when an admin last modified or reviewed the source config |
| `config_update_policy` | `manual` or `expires`; no automated config updates are performed |
| `config_expires_at` | Review deadline for the `expires` policy; the Web UI warns strongly once expired |
| `attempt_timeout_seconds` | Timeout for a single upstream attempt |
| `slow_after_seconds` | When low-throughput detection begins |
| `min_bytes_per_second` | When sustained throughput falls below this, fall over to the fixed fallback |

`kind` can apply protocol-level validation to npm, Go proxy, Go sumdb, Cargo sparse, crate download, Dart Pub, Julia Pkg Server, PyTorch wheels, and APT paths. When adding a new source, choose the narrowest kind and path rather than relying on a broad generic source to elude validation. Dart uses two fixed sources for metadata and archives; metadata can only rewrite `archive_url` to an audited archive source. Julia only permits `registries` and three categories of content-addressed objects.

Auto-generated APT repository, APT static object, and download source names are formed from readable host/path and carry no hash suffix. If two upstreams resolve to the same name, prepare/merge must report a conflict and the admin must choose a more explicit name, rather than reintroducing opaque suffixes. Historical config can use `scripts/migrate_source_names.py` to update sources, rewrite routes, and refresh metadata in one pass.

`config_update_policy=manual` means updates happen only after an explicit review by an admin; `expires` must also set `config_expires_at`. The deadline is used solely for a status-page reminder — it never automatically relaxes, deletes, or rewrites a source on expiry, and it does not stop serving existing cached content. Whether an artifact is refreshed is still governed only by path mutability and `metadata_ttl_seconds`.

After changing config, run at least the full unit tests and use `/v1/status` to confirm that sources, update status, and fallback summaries are as expected.

## Proxy and Cache Admission

Each upstream declares itself independently:

- `direct`: explicitly does not use `ARTIFACT_CACHE_UPSTREAM_PROXY`;
- `configured`: uses the proxy when configured, otherwise connects directly.

The Gateway clears any inherited `HTTP_PROXY`, `HTTPS_PROXY`, and `ALL_PROXY`, so an ambient proxy cannot change policy. Setting a shell proxy does not mean the Gateway fetch path used it.

Cache admission is controlled by `ARTIFACT_CACHE_CACHE_MODE`:

- `proxy-only`: only downloads that actually succeeded through the configured proxy are published to S3; domestic direct-connect successes return `BYPASS`.
- `all`: every successful download is published to S3.

In either mode the full response is first downloaded to a temp file and length-checked. Metadata and content-addressed blobs are published only after that completes successfully.

## Git Mirror

The CLI enables the Git Smart HTTP route only when `--git-mirror-plan` or `ARTIFACT_CACHE_GIT_MIRROR_PLAN` is provided. The plan may be empty; it is used only for pre-heating, submodule discovery of pinned commits, and planned ready/missing statistics, and never restricts the online-set of repositories.

Runtime security boundary:

- upstream is fixed to `https://github.com/<owner>/<repository>.git`;
- owner/repository allow only the two safe path components;
- credentials, query, fragment, or extra paths are rejected;
- only `git-upload-pack` is served; receive-pack is refused;
- first fill and refresh are protected by concurrency, timeouts, thread locks, and cross-process file locks;
- clones complete in a temp directory and are atomically published only after being verified as bare repositories.

Status fields include the plan total, plan ready/missing, derived submodules, on-demand repos of the current process, cached repositories, and request results. The historical `.derived-allowlist.json` filename is kept for compatibility, but its content is now a submodule pre-heat manifest rather than a runtime allowlist.

## Direct Download Plans

Download plans are used only for pre-heating, capacity estimation, and observability; they do not control runtime admission on the dynamic download ingress. The absence of a plan, of an origin source, or of a previously analyzed request path can never justify rejecting a prebuild GET.

Requirements:

- a plain query is part of the original request identity and must not be lost;
- calls that cannot preserve authentication, request method, headers, proxy, or output semantics are BYPASS'd by the wrapper;
- plans do not generate per-object or per-origin runtime allowlists;
- new URLs are fetched on demand, with no config update or Gateway restart required;
- the versioned ingress is `/v1/cache/download/v1/<scheme>/<hex-authority>/{root|object/<hex-path>}`, where both the origin and path are reversibly encoded in lowercase hex to avoid divergent `%2F` decoding by reverse proxies and never use a truncated source name as routing identity.

> **This ingress is only for the OpenSandbox prebuild automated client — manual curl/wget calls are prohibited.** Until an application-layer token lands, deployments must keep it on an internal address and restrict it with network policy to the prebuild worker; a future token will only vouch for prebuild callers, not audit the target URL.

## OpenSandbox Prebuild APT Dynamic Ingress

APT uses `/v1/cache/apt/v1/<scheme>/<hex-authority>/base/<hex-base-path>/...`. The Gateway reconstructs the repository origin, base path, and APT-appended path from the route, and does not read the APT plan or `sources.json` for runtime admission. The Agent Fleet side no longer supplies override JSON or source-map secrets. New repositories need no config change or service restart.

Like the download data plane, this ingress is only for the prebuild automated client. Existing named APT sources remain usable by other explicit clients, but must not be re-wired as compat overrides for the new prebuild flow.

## Health, Status, and Observability

| Endpoint | Operational use |
| --- | --- |
| `/healthz` | Process liveness; does not imply all upstreams are reachable |
| `/v1/status` | Storage type, source-policy summary, request rollups, recent failures, Git status |
| `/v1/objects` | Read-only, paginated cache inventory |
| `/ui/` | Read-only page for the above |

Request counters are saved to S3 as schema 4 session documents, so they remain aggregateable across normal restarts. `requests.modules` stores per-module rollups for `apt`, `pypi`, `npm`, `go`, `rust`, `curl`, and `git_clone`; each module's `sources` then stores cache status, write volume, and upstream-fetch results per Gateway source; all GitHub repositories are aggregated under `git_clone.sources.github`. Data from schema 1/2 that has no module dimension is counted only under `unclassified`; schema 3 data that has a module but no source dimension is shown under the module-level `historical_unattributed` field and is never misreported as a source. Structured logs record source, stage, candidate location, proxy mode, latency, and result — not full upstream URLs, proxy addresses, or secrets.

Common checks:

```bash
curl -fsS http://127.0.0.1:8080/healthz
curl -fsS http://127.0.0.1:8080/v1/status
docker logs --tail 100 <gateway-container>
```

## Troubleshooting

### Startup S3 Check Fails

Verify the endpoint format, whether the bucket exists, credential permissions, and network connectivity. The S3 endpoint must not carry a path. The Gateway will not automatically switch to local disk.

### Requests Constantly BYPASS

Check the cache mode and the `proxy_mode` of the upstream that was hit. Under `proxy-only`, a direct success is an expected BYPASS.

### All Fallbacks Fail

From the logs, confirm whether the failure stage is connect, response, redirect, read, or throughput. Do not temporarily relax the origin or path; first verify the fixed upstream and proxy path.

### Git Planned Missing

`planned_missing_repositories` only means there is no mirror directory in the plan yet. Run `warm-git` to get per-repository results; upstream deletion, migration, or privatization requires a plan update or an explicit alias policy — credentials must not be used to bypass the public-repo boundary.

### Temp Directory Growth

The per-object cap is controlled by `--max-object-gib`; on startup files under `fetch-*` older than 24 hours are cleaned up. There is currently no global temp-space quota, so disk monitoring and capacity limits should be provided by the platform.

## Backup and Lifecycle

- Configure platform-side quotas, lifecycle, backup, and TLS policies for the S3 bucket; the project itself has no GC.
- The Git mirror root is data that can be refilled from public GitHub, but upstreams may disappear; include it in storage backups if reproducibility matters.
- Do not hand-edit content-addressed blobs or URL metadata.
- Stop all Gateway and warm processes before deleting or migrating the S3 prefix or the Git mirror root.
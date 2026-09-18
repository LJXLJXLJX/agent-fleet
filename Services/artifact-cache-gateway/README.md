# Artifact Cache Gateway

Artifact Cache Gateway is a read-only dependency cache for build environments. It routes
package managers, prebuild downloads and public GitHub clones to a sticky internal
entry point: it fills the cache in real time while the upstream is reachable, and reuses
already-published objects when the upstream is not reachable.

The project also ships dataset-analysis tooling that discovers the public base images,
package dependencies, direct downloads and GitHub repositories for image prebuilds, and
produces reviewable, executable preparation plans.

## Capabilities at a glance

| Scenario | Client entry point | Cache location | Runtime admission |
| --- | --- | --- | --- |
| Configured package-manager source | `/v1/cache/<source>/...` | S3 | source protocol and path policy |
| prebuild APT | `/v1/cache/apt/v1/...` | S3 | Derived from the repository URI; no source or map required |
| prebuild `curl` / `wget` GET | `/v1/cache/download/v1/...` | S3 | Unseen URLs are fetched on demand from origin; no origin/path registration required |
| Public GitHub clone | `/v1/git/github/<owner>/<repo>[.git]` | Persistent filesystem | GitHub-only protocol constraint; repository plan not checked |
| Public base OCI image | internal Registry reference | Harbor and other Registries | Offline analysis and mirror plan |

S3 is the authoritative storage for HTTP artifacts; the local directory is only used for
on-demand origin staging. Git bare mirrors need random file access, so they are stored
separately on a persistent filesystem such as GPFS. OCI images do not pass through the
Gateway data plane; they are synced directly to the target Registry by
`artifact-cache-prepare`.

## What happens to a request

```text
build client
  ├─ package / static URL ─> Gateway source policy ─> S3 HIT
  │                                             └─> pinned upstream ─> atomic write to S3
  ├─ GitHub clone ─────────> Git Smart HTTP ───────> bare mirror HIT
  │                                             └─> github.com clone --mirror
  └─ OCI image pull ───────> internal Registry (pre-synced by the prepare tool)
```

Ordinary package sources remain subject to origin, path, query, redirect and protocol
format constraints. prebuild APT and dynamic downloads are separate transparent cache
entry points: they do not audit target URLs against a source plan; APT repositories need no
explicit map, and ordinary queries are part of the request identity. Any semantics-preserving
authentication, custom methods or complex curl/wget invocations should bypass the cache
directly on the client side. The Git path pins the upstream to public
`github.com` and only
serves `git-upload-pack`; it rejects push, credentials, queries, fragments and unsafe paths.

> **The APT and curl/wget dynamic entry points are for the OpenSandbox prebuild automated
> flow only. Do not hand-craft calls, write them into tasks, or expose them to the public
> internet or ordinary Sandboxes. Caller restrictions are enforced by network isolation and
> later prebuild authentication, not by a URL allowlist.**

## Quick start

Requires Python 3.10+. Production runs require S3 by default; the `file` storage is only for
local development and testing.

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .

artifact-cache-gateway \
  --storage file \
  --cache-dir /tmp/artifact-cache \
  --config config/sources.json \
  --cache-mode all \
  --host 127.0.0.1 \
  --port 8080
```

In another terminal, check the service:

```bash
curl -fsS http://127.0.0.1:8080/healthz
curl -fsS http://127.0.0.1:8080/v1/status
```

Browse to `http://127.0.0.1:8080/ui/` to inspect cache status, sources, recent failures and the
object inventory.

Common client configuration:

```bash
# PyPI
python -m pip install \
  --index-url http://127.0.0.1:8080/v1/cache/pypi-simple \
  requests

# npm
npm install \
  --registry http://127.0.0.1:8080/v1/cache/npm-registry \
  lodash

# Dart Pub
PUB_HOSTED_URL=http://127.0.0.1:8080/v1/cache/dart-pub \
  dart pub global activate melos

# Julia Pkg Server
JULIA_PKG_SERVER=http://127.0.0.1:8080/v1/cache/julia-pkg \
  JULIA_PKG_PRECOMPILE_AUTO=0 \
  julia --project=. -e 'using Pkg; Pkg.instantiate()'

# Public GitHub; production must first have the Git mirror route enabled by operations
git config --global \
  url."http://127.0.0.1:8080/v1/git/github/".insteadOf \
  "https://github.com/"
```

See the [user guide](docs/user-guide.md) for complete client configuration.

## Production operation

Copy the configuration template and write real credentials into the Git-ignored
`config.local.env`:

```bash
cp config.example.env config.local.env
```

At minimum the following S3 configuration is required:

```text
ARTIFACT_CACHE_S3_ENDPOINT
ARTIFACT_CACHE_S3_REGION
ARTIFACT_CACHE_S3_BUCKET
ARTIFACT_CACHE_S3_PREFIX
ARTIFACT_CACHE_S3_ACCESS_KEY_ID
ARTIFACT_CACHE_S3_SECRET_ACCESS_KEY
```

The repository offers two container entry points:

- `scripts/start-gateway.sh --build`: single-machine host-network startup, suited to a dev
  machine or a standalone node.
- `deploy/Dockerfile.hosted`: the Gateway and mihomo are supervised by the same container,
  suited to an internal hosted platform.

See the [operator guide](docs/operator-guide.md) for production deployment, source policy,
cache mode, runtime directories and failure handling.

## Preparing build dependencies for datasets

Analyze first, without writing to the Registry:

```bash
artifact-cache-prepare analyze \
  /data/harbor-datasets/<dataset> \
  --output-dir /data/artifact-preparation/<dataset>
```

After reviewing the generated plan, sync the missing base images and warm up the problem
packages:

```bash
artifact-cache-prepare prepare \
  /data/harbor-datasets/<dataset> \
  --output-dir /data/artifact-preparation/<dataset> \
  --gateway-url http://<INTERNAL_GATEWAY>/v1/cache \
  --concurrency 4 \
  --execute
```

Direct-download registration and bulk Git warm-up are separate steps. See the
[dataset preparation guide](docs/dataset-preparation.md) for details.

## HTTP interface

| Path | Purpose |
| --- | --- |
| `/healthz` | Liveness check |
| `/ui/` | Read-only web UI |
| `/v1/status` | Sources, per-module and per-source request counters, recent failures and Git mirror status |
| `/v1/objects` | Paginated object inventory |
| `/v1/cache/<source>/<path>` | HTTP artifact GET/HEAD |
| `/v1/git/github/<owner>/<repo>[.git]` | Read-only Git Smart HTTP; compatible with URLs that omit `.git` in submodules |

`/v1/status` and the logs do not return the full upstream URL, proxy address or credentials.
Auto-generated APT and download sources use readable names composed from host/path, with no
hash in the name.
Source configuration additionally records the last-update date and a manual review policy;
these fields do not substitute for the artifact metadata TTL.
Request statistics are first persisted per module (`apt`, `pypi`, `npm`, `go`, `rust`,
`curl` and `git_clone`), then broken out per Gateway source within the module; all GitHub
repositories are aggregated under `git_clone.sources.github`. Pre-upgrade aggregates without
a module dimension are kept in `unclassified`, and aggregates that have a module but no
source dimension are kept at module level under `historical_unattributed`; they are never
disguised as a source and never back-filled by guessing.

## Documentation

- [User guide](docs/user-guide.md): how to point pip, APT, npm, Go, Cargo, Rustup, Git and
  static downloads at the Gateway.
- [Operator guide](docs/operator-guide.md): S3, proxies, source policy, deployment,
  observability and day-to-day maintenance.
- [Dataset preparation guide](docs/dataset-preparation.md): analysis, review, image sync,
  package warm-up, direct downloads and Git warm-up.
- [Developer guide](docs/developer-guide.md): code structure, core invariants, local testing
  and how to extend.

The complete set of CLI arguments always follows the commands themselves:

```bash
artifact-cache-gateway --help
artifact-cache-prepare --help
artifact-cache-prepare <command> --help
```

## Current boundaries

- The service itself has no user authentication; deploy it on a trusted internal network or
  behind an existing auth layer.
- HTTP artifacts have no S3 GC, capacity quotas, metrics history backend, or config-write UI.
- Git mirrors do not support private repositories, GitLab, SSH transport, Git LFS or push.
- The Git mirror plan is a warm-up and coverage checklist, not a runtime repository allowlist.
- Dataset analysis is static analysis; dependencies inside runtime concatenation, remote
  scripts, and unresolvable variables may need manual complements.
- `artifact-cache-prepare` does not modify the original dataset and is not responsible for the
  final task image build.

## Development

```bash
python -m unittest discover -s tests -v
```

Please read the [developer guide](docs/developer-guide.md) before committing changes, and
keep the security invariants of source policy, log redaction, atomic publication and S3 as
authoritative storage.
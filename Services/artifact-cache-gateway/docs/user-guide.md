# User Guide

This document is for users who consume the Artifact Cache Gateway from builds, CI, or Sandbox environments. The `http://<INTERNAL_GATEWAY>` in the examples must be replaced with the internal address provided by your environment; do not write internal addresses or credentials into public repositories.

## Before You Start

First confirm the service is reachable:

```bash
curl -fsS http://<INTERNAL_GATEWAY>/healthz
```

Once it returns `ok`, look at the currently enabled sources:

```bash
curl -fsS http://<INTERNAL_GATEWAY>/v1/status
```

Sources share a uniform form:

```text
http://<INTERNAL_GATEWAY>/v1/cache/<source>/<source-specific-path>
```

Clients cannot specify an arbitrary upstream through the Gateway. If the source does not exist or the path does not match that source's protocol, the request is rejected.

Auto-generated source names directly describe the upstream host/path and contain no hash. The Web UI separately shows "Config last updated" and "Config update policy": `Manual` means updates happen only after an admin reviews the config; a deadline policy prompts the admin to review upon expiry, but it never switches upstreams automatically or interrupts existing caches. That date describes the source config, not the publish time or freshness of a cached package.

## pip and PyPI

Use `pypi-simple` for regular PyPI:

```bash
python -m pip install \
  --index-url http://<INTERNAL_GATEWAY>/v1/cache/pypi-simple \
  <package>
```

You can also set it in the build environment:

```bash
export PIP_INDEX_URL=http://<INTERNAL_GATEWAY>/v1/cache/pypi-simple
```

Artifact links returned by the simple index are rewritten back to the Gateway's `pypi-files` source, so clients do not need to configure a separate download address.

PyTorch wheels use their own source. The Gateway accepts the officially named `cpu`, `xpu`, `cu<version>`, and `rocm<version>` channels without enumerating versions individually; requests can still only be package indexes or supported artifact files:

```bash
python -m pip install \
  --index-url http://<INTERNAL_GATEWAY>/v1/cache/pytorch/cpu \
  torch
```

For example, CUDA 12.9:

```bash
python -m pip install \
  --index-url http://<INTERNAL_GATEWAY>/v1/cache/pytorch/cu129 \
  torch torchvision
```

SGLang and FlashInfer do not yet provide a package index. Callers should download through their own controlled GitHub Release sources and install the local files. The source base URL is pinned to the trusted project's `releases/download/` directory, so any release version of that project is allowed, but other GitHub repositories or other project paths are not:

```bash
curl -fL \
  http://<INTERNAL_GATEWAY>/v1/cache/sglang-wheel-files/v0.4.6.post1/sglang_kernel-0.4.6.post1+cu129-cp310-abi3-manylinux2014_x86_64.whl \
  -o /tmp/sglang_kernel-0.4.6.post1+cu129-cp310-abi3-manylinux2014_x86_64.whl

wget -O /tmp/flashinfer_jit_cache-0.6.18+cu129-cp39-abi3-manylinux_2_28_x86_64.whl \
  http://<INTERNAL_GATEWAY>/v1/cache/flashinfer-wheel-files/v0.6.18/flashinfer_jit_cache-0.6.18+cu129-cp39-abi3-manylinux_2_28_x86_64.whl

python -m pip install \
  /tmp/sglang_kernel-0.4.6.post1+cu129-cp310-abi3-manylinux2014_x86_64.whl \
  /tmp/flashinfer_jit_cache-0.6.18+cu129-cp39-abi3-manylinux_2_28_x86_64.whl
```

`sgl_deep_gemm` and `sgl_deep_ep` use the same `sglang-wheel-files` route. Release wheels are treated as immutable download caches, but deploying a source config does not by itself pre-heat objects.

## npm

Specify the Registry once:

```bash
npm install \
  --registry http://<INTERNAL_GATEWAY>/v1/cache/npm-registry \
  <package>
```

Or set an environment variable:

```bash
export npm_config_registry=http://<INTERNAL_GATEWAY>/v1/cache/npm-registry
```

The Gateway rewrites tarball URLs in the metadata so that subsequent downloads still go through the same source. Both regular and scoped packages are subject to the npm Registry path syntax.

## Dart Pub

Point your hosted repository root at `dart-pub`:

```bash
export PUB_HOSTED_URL=http://<INTERNAL_GATEWAY>/v1/cache/dart-pub
export PUB_CACHE=/workspace/.pub-cache
dart pub global activate melos
```

Regular projects continue to run `dart pub get`. The Gateway caches package metadata and forcibly rewrites the audited `archive_url` values to `dart-pub-archives`; unknown origins, malformed package/version paths, and queries are rejected rather than passed through to the client. Callers do not need to configure an archive source, a domestic mirror, or fallbacks for S3 or upstream proxies.

## Julia Pkg Server

Point your Pkg Server root at `julia-pkg`:

```bash
export JULIA_PKG_SERVER=http://<INTERNAL_GATEWAY>/v1/cache/julia-pkg
julia --project=. -e 'using Pkg; Pkg.instantiate()'
```

The Gateway supports the standard download chain of `registries`, registry snapshots, package sources, and artifacts. Only `registries` is TTL metadata; the remaining content-addressed objects are cached as immutable. Upstream redirects happen inside the Gateway. To isolate dependency downloads for verification you can set `JULIA_PKG_PRECOMPILE_AUTO=0`, but this only excludes precompile time from the cache verification: the Gateway does not proxy Julia code precompilation and does not resolve precompile hangs.

A successful Pkg Server path never returns the upstream `Location` to the client. Julia Pkg itself will still attempt package/artifact origins recorded in the registry when a package-server download fails; the Gateway cannot remove those addresses without breaking the registry content hash. Runtime environments that must never touch the public network even on failure still need an egress network policy.

## APT

APT source names represent a fixed distribution or third-party repository. Ubuntu example:

```text
deb http://<INTERNAL_GATEWAY>/v1/cache/ubuntu jammy main universe
deb http://<INTERNAL_GATEWAY>/v1/cache/ubuntu jammy-updates main universe
deb http://<INTERNAL_GATEWAY>/v1/cache/ubuntu jammy-security main universe
```

Debian example:

```text
deb http://<INTERNAL_GATEWAY>/v1/cache/debian bookworm main
deb http://<INTERNAL_GATEWAY>/v1/cache/debian bookworm-updates main
deb http://<INTERNAL_GATEWAY>/v1/cache/debian-security bookworm-security main
```

The Gateway preserves the distribution's original signature verification. Do not use `trusted=yes`, disable signature checks, or mix keys from other distributions. A third-party APT repository is usable only if its source exists in `/v1/status`; its source name and signature-key path follow the operator-provided config.

APT metadata is a mutable object and is conditionally refreshed by TTL; versioned packages under `pool/` are usually reused as immutable objects.

### Dynamic APT Route for OpenSandbox Prebuild

The named sources above remain for explicit ordinary clients. Agent Fleet prebuild no longer maintains a JSON map for third-party repositories and does not require them to appear in `/v1/status`. Its build-time wrapper generates an internal route from the source URI:

```text
/v1/cache/apt/v1/<scheme>/<hex-authority>/base/<hex-base-path>/...
```

The trailing `...` is appended normally by APT, and the Gateway reconstructs the original upstream URL from the whole path. Like the curl/wget dynamic ingress below, this route is only for the prebuild automated flow, not an interface for manually configuring APT sources.

## Go Modules

```bash
export GOPROXY=http://<INTERNAL_GATEWAY>/v1/cache/go-proxy
export GOSUMDB="sum.golang.org http://<INTERNAL_GATEWAY>/v1/cache/go-sumdb"
go mod download
```

`GOSUMDB` still uses Go's built-in `sum.golang.org` public key; only the checksum database's transport address is replaced. The Gateway accepts only the standard module proxy and sumdb paths and never disables integrity checks.

If the business explicitly allows a public-network fallback, you can append `,direct` to `GOPROXY` yourself; this bypasses the Gateway, and whether it is allowed is up to the calling environment.

## Cargo and Rustup

A Cargo project can configure a sparse registry in `.cargo/config.toml`:

```toml
[source.crates-io]
replace-with = "artifact-cache-gateway"

[source.artifact-cache-gateway]
registry = "sparse+http://<INTERNAL_GATEWAY>/v1/cache/cargo-index/"
```

The Gateway rewrites crate download addresses in the sparse index to the `cargo-crates` source.

Rustup uses the following variables:

```bash
export RUSTUP_DIST_SERVER=http://<INTERNAL_GATEWAY>/v1/cache/rustup-dist
export RUSTUP_UPDATE_ROOT=http://<INTERNAL_GATEWAY>/v1/cache/rustup-update
```

The install script has a fixed route and accepts no arbitrary URL:

```bash
curl -fsS \
  http://<INTERNAL_GATEWAY>/v1/cache/rustup-init/rustup-init.sh \
  -o /tmp/rustup-init.sh
```

Before executing a remote install script, still review its content and verify the source per your project's security requirements.

## Public GitHub Clone

When the operator has enabled the Git mirror route, standard HTTPS GitHub URLs can be rewritten automatically:

```bash
git config --global \
  url."http://<INTERNAL_GATEWAY>/v1/git/github/".insteadOf \
  "https://github.com/"

git clone https://github.com/<owner>/<repository>.git
```

Repositories do not need to appear in a dataset plan beforehand. The first request performs a `git clone --mirror` from public `github.com` and atomically publishes it on success; later clones are served from the persistent bare mirror. Repositories that differ only in case reuse the known canonical path. The Gateway route accepts repository URLs with or without `.git`, so the same `insteadOf` config also covers common GitHub submodule URLs.

Supported boundaries:

- only credential-free public GitHub repositories;
- only the `git-upload-pack` needed for clone/fetch;
- no push, private repositories, GitLab, SSH transport, or Git LFS;
- owner/repository must be two safe path segments and cannot carry query or fragment.

To remove the global rewrite after your build:

```bash
git config --global --unset-all \
  url."http://<INTERNAL_GATEWAY>/v1/git/github/".insteadOf
```

## OpenSandbox Prebuild curl / wget Downloads

> **Manual calls are prohibited.** The dynamic download ingress is only for Agent Fleet's OpenSandbox prebuild wrapper; it is not a public download API and must not be written into task Dockerfiles or ordinary Sandbox environments.

Task authors keep writing their original `curl`/`wget` commands. The wrapper translates plain HTTP(S) GETs whose semantics can be preserved into an internal versioned route; a brand-new URL that never appeared in analysis or a plan can also be fetched on demand on its first request:

```json
{
  "upstream_url": "https://example.org/releases/tool-1.2.3.tar.gz",
  "gateway_path": "/v1/cache/download/v1/https/<hex-authority>/object/<hex-path>"
}
```

This route is an internal protocol illustration, not a manual invocation example. A plain query is kept as part of the full request identity; calls that carry authentication, cookies, custom headers, a request body, a method, a proxy, or implicit config that the cache layer cannot guarantee to be equivalent are executed as the original `curl`/`wget` directly, and the task is never rejected because of a Gateway capability boundary. Download plans are only for pre-heating and observability, not an origin/path allowlist.

## Understanding Cache Status

The Web UI and `/v1/status` use the following statuses:

| Status | Meaning |
| --- | --- |
| `HIT` | Reuses an already-published S3 object directly; no upstream access |
| `MISS` | Fetches upstream for the first time and publishes a new object |
| `REFRESH` | Published new content after a mutable object expired |
| `REVALIDATED` | A conditional request returned not-modified; the old object is reused |
| `STALE` | Refresh failed; a still-usable old object is returned |
| `BYPASS` | Upstream fetch succeeded but the current cache mode does not publish to S3 |
| `ERROR` | The request could not return a usable object |

Range and HEAD requests are supported on cached objects. `BYPASS` is not a cache hit: the next request may fetch upstream again.

The Web UI's "per-submodule independent stats" first separates APT, PyPI, npm, Go Modules, Rust/Cargo, curl/wget, and Git clone, then shows requests, hits, upstream fetches, and writes per Gateway source within each module; all repositories under GitHub are aggregated as a single `GitHub` source. Cumulative stats saved before an upgrade that have no module field are shown as "historical unclassified"; those that already have a module field but no source field are shown as "recorded before the upgrade without a data source (not a data source)". New requests are attributed precisely to their source.

The APT source list clearly marks the currently registered boundary: new packages and versions from repositories inside the list are cached in real time on request; third-party repositories outside the list are not auto-registered — an admin must manually run prepare, review, and merge the source config, then restart the Gateway.

## FAQ

### Returns 404

Usually means the source is not configured, the path does not match the protocol, a static download is not registered, or the Git path format is invalid. First check the source list in `/v1/status`, then review the request path.

### Returns 502

The Gateway accepted the request, but every fixed upstream failed, or the GitHub mirror fill failed. Check the recent failures in the Web UI and the server-side structured logs; URLs and proxy info in the logs are redacted.

### First Request Is Slow, the Second Is Fast

This is normal live-fill behavior. The first request must complete the upstream download and the atomic publish; the second is a strict cache hit.

### Always BYPASS

The operator may have enabled the default `proxy-only` admission: objects that succeeded via domestic direct connection are not written to S3. To cache all successful responses, the operator must change the cache mode to `all`.

### Pre-heated, but the Build Still Reaches the Public Network

Pre-heating does not modify client config automatically. Confirm that the pip/npm/APT/Git environment really points at the Gateway; for direct downloads, you must use the exact path given by the plan.
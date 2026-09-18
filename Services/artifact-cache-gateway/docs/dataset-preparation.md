# Dataset Preparation Guide

`artifact-cache-prepare` targets Harbor-style benchmark datasets. It scans a task's Dockerfile, referenced scripts, and package declarations to produce preparation results for public base images, packages, direct downloads, and Git repositories.

The tool does not modify the original dataset and does not build the final task image. It converts shareable external inputs into auditable internal dependency entry points, reducing public-network dependency for the subsequent image prebuild.

## Recommended Flow

```text
dataset
  -> analyze
  -> review generated plans and unresolved inputs
  -> register exact HTTP downloads / APT sources
  -> mirror missing OCI base images
  -> warm problem packages and planned Git repositories
  -> run the real image prebuild through internal endpoints
```

Analysis and write operations are separate. Every command that writes to the Registry, S3, or the Git mirror requires an explicit `--execute`.

## 1. Configure the Target Environment

Registry credentials live only in the Git-ignored `config.local.env` or an existing authfile:

```text
YICLOUD_HARBOR_HOST=<registry-host>
YICLOUD_HARBOR_USERNAME=<registry-user>
YICLOUD_HARBOR_PASSWORD=<registry-password>
ARTIFACT_MIRROR_PROJECT=public-mirror
ARTIFACT_MIRROR_PLATFORM=linux/amd64
ARTIFACT_MIRROR_CONCURRENCY=4
```

Optional variables:

- `ARTIFACT_MIRROR_SOURCE_PREFIX_MAP_JSON`: configures the ordered mapping from upstreams to an accessible mirror;
- `ARTIFACT_MIRROR_UPSTREAM_PROXY`: an explicit upstream proxy;
- `ARTIFACT_MIRROR_DIRECT_UPSTREAM_WITH_PROXY`: whether, after a domestic mirror fails, to try the official source through the proxy;
- `ARTIFACT_CACHE_GATEWAY_URL`: the `/v1/cache` root used for warming packages and direct downloads.

The tool does not implicitly inherit an ambient proxy. Whether each actual downloading component uses a proxy is decided by command arguments and the generated execution environment.

## 2. Analyze Only

```bash
artifact-cache-prepare analyze \
  /data/harbor-datasets/<dataset> \
  --output-dir /data/artifact-preparation/<dataset> \
  --shell-scope referenced
```

By default it probes the domestic sources of the discovered APT, pip, and npm requirements. For a quick static inventory only:

```bash
artifact-cache-prepare analyze \
  /data/harbor-datasets/<dataset> \
  --output-dir /data/artifact-preparation/<dataset> \
  --no-probe-domestic-packages
```

`--shell-scope` controls the shell-script analysis range:

- `referenced`: default; only scans scripts actually referenced by the Dockerfile;
- `all`: scans all candidate scripts in the build context, covering more but producing more false positives;
- `none`: analyzes only the Dockerfile and directly declared files.

## 3. Understanding the Output

Main files:

| File | Purpose |
| --- | --- |
| `summary.json` / `report.md` | Task, stage, dependency, and coverage summary |
| `image-mirror-plan.json` | Plan from upstream OCI images to internal targets |
| `image-mirror-selection.json` | Existing/missing target results after running mirror/prepare |
| `package-probe-report.json` | Domestic-source probe results and consumption contexts for APT, pip, npm |
| `apt-cache-candidates.json` | Candidates requiring fixed third-party APT source/key/objects |
| `apt-gateway-plan.json` | APT source plan that can be merged into the Gateway |
| `external-build-inputs.json` | Explicit Git clone and curl/wget inventory |
| `download-gateway-plan.json` | Registrable precise static download mappings |
| `git-mirror-plan.json` | Git pre-heat, submodule discovery, and coverage plan |

The report keeps the consuming task, Dockerfile stage, base image, and requirement source so you can tell whether same-named packages sit in different resolution environments.

### Why Environments Cannot Be Merged

Package resolution is at least affected by these dimensions:

- base image / distribution / codename / architecture;
- Python implementation, version, ABI, and wheel platform;
- npm Registry and resolution options;
- the Dockerfile stage, and which stage references a script.

Identical package names cannot be deduplicated by string alone. The tool first resolves within each resolution environment, then deduplicates by final artifact digest or URL while preserving all consumers.

## 4. Review Checklist

Check before any write operation:

### OCI Image

- does the source reference come from the dataset's actual `FROM`;
- is the source-prefix mirror a verified mapping, not a guessed path;
- do the target repository, tag, platform, and digest match internal Registry conventions;
- has the unresolved variable image been handled manually.

### Packages

- is `unavailable` because the source is unreachable, the version does not exist, or the resolution environment is unsupported;
- are the Gateway sources for slow/unavailable artifacts fixed and narrow enough;
- do third-party APT repositories contain only the keys and sources that were in effect at declaration time;
- can an unsupported manager affect the target task.

### Direct Download

- are known URLs suitable for pre-heating, or likely to be large or frequently changing;
- will calls with authentication, a request body, a custom method, or complex headers correctly BYPASS;
- plans are only pre-heat and observability input and must not generate origin/path admission;
- the original Dockerfile keeps ordinary curl/wget forms and does not embed an internal `gateway_path`.

### Git

- repositories in the plan are for coverage and pre-heating, not admission allowlists;
- does the pinned commit still exist;
- are submodule URLs public GitHub;
- do deleted, migrated, or privatized repositories need an explicit alias or a dataset revision.

## 5. Sync Public Base Images

The combined command re-analyzes, checks targets, uploads only the missing images, and by default pre-heats the probed problem packages:

```bash
artifact-cache-prepare prepare \
  /data/harbor-datasets/<dataset> \
  --output-dir /data/artifact-preparation/<dataset> \
  --gateway-url http://<INTERNAL_GATEWAY>/v1/cache \
  --concurrency 4 \
  --execute
```

If you want to strictly use an already-audited image plan:

```bash
artifact-cache-prepare mirror-images \
  /data/artifact-preparation/<dataset>/image-mirror-plan.json \
  --concurrency 4 \
  --execute
```

Results are written to `image-mirror-result.json`, recording each source attempt, target, source/target digests, and errors. Only after an image is obtained successfully and its target platform is verified should the image prebuild use the internal reference.

`--direct-upstream-with-proxy` is used only when there is an explicit proxy and the domestic Registry mirror has already failed:

```bash
artifact-cache-prepare mirror-images \
  /data/artifact-preparation/<dataset>/image-mirror-plan.json \
  --upstream-proxy http://<proxy-host>:<proxy-port> \
  --direct-upstream-with-proxy \
  --execute
```

This is a long network task. Before running it, confirm the target Registry permissions, concurrency, and output path; while running, you can watch the command's per-image progress lines.

## 6. Prepare Package Dependencies

Warm only packages already probed as slow/unavailable:

```bash
artifact-cache-prepare warm-packages \
  /data/artifact-preparation/<dataset>/package-probe-report.json \
  --gateway-url http://<INTERNAL_GATEWAY>/v1/cache \
  --execute
```

Providers use the report's resolution environments: APT by distro/codename/architecture, pip by Python/wheel environment, and npm by Registry resolution. Warming verifies the object the Gateway returns; it never auto-maps an unknown package manager onto an existing source.

Third-party APT candidates are first compiled into safe source config:

```bash
artifact-cache-prepare configure-apt \
  /data/artifact-preparation/<dataset>/apt-cache-candidates.json \
  --base-config config/sources.json \
  --output /data/artifact-preparation/<dataset>/sources.reviewed.json
```

After reviewing and deploying the new config and restarting the Gateway, run the relevant package warming or image prebuild.

## 7. Analyze and Warm Direct Downloads

Generate the merged config and a standalone plan:

```bash
artifact-cache-prepare configure-downloads \
  /data/artifact-preparation/<dataset>/external-build-inputs.json \
  --base-config config/sources.json \
  --output /data/artifact-preparation/<dataset>/sources.reviewed.json \
  --plan-output /data/artifact-preparation/<dataset>/download-gateway-plan.json
```

The plan is used to fill known objects in advance and estimate cache benefit. It is not a runtime license for new URLs: the absence of a plan does not stop OpenSandbox prebuild from requesting a never-seen URL on demand, and no Gateway restart is needed for a new origin. The original Dockerfile does not use the plan's internal routes; routes are generated only by the prebuild wrapper.

Once in effect, bulk-fill and verify the second request:

```bash
artifact-cache-prepare warm-downloads \
  /data/artifact-preparation/<dataset>/download-gateway-plan.json \
  --gateway-url http://<INTERNAL_GATEWAY>/v1/cache \
  --concurrency 4 \
  --execute
```

> The commands above are operational entry points of the dedicated preparation tool. Manually accessing the dynamic download data plane with curl/wget is prohibited.

Refresh policy:

- objects carrying a release tag, version, or digest default to immutable and are not auto-refreshed;
- installer, branch, latest, nightly, and similar objects default to manual and are checked only on explicit refresh;
- forcing a check of immutable objects requires the extra `--include-immutable`.

```bash
artifact-cache-prepare refresh-downloads \
  /data/artifact-preparation/<dataset>/download-gateway-plan.json \
  --config /data/artifact-preparation/<dataset>/sources.reviewed.json \
  --env-file config.local.env \
  --storage s3 \
  --execute
```

## 8. Warm GitHub Repositories

```bash
artifact-cache-prepare warm-git \
  /data/artifact-preparation/<dataset>/git-mirror-plan.json \
  --root <PERSISTENT_GIT_MIRROR_ROOT> \
  --upstream-proxy http://<proxy-host>:<proxy-port> \
  --concurrency 4 \
  --execute
```

By default it traverses the plan plus submodules discovered from pinned commits. To prepare only a single repository:

```bash
artifact-cache-prepare warm-git \
  /data/artifact-preparation/<dataset>/git-mirror-plan.json \
  --root <PERSISTENT_GIT_MIRROR_ROOT> \
  --repository <owner>/<repository> \
  --execute
```

An existing bare mirror is treated as a HIT by default and is not fetched automatically. Add `--refresh` when you really need to update the upstream refs. A live Gateway can still fill a safe public GitHub repository not present in the plan.

Warming thousands of repositories is a long network and heavy-IO operation; before running it, confirm capacity, concurrency, the proxy, and the result JSON path.

## 9. Hand Off to Image Prebuild

Once dependencies are prepared, the final image prebuild must at minimum:

- use an internal Registry reference that has been verified for `FROM`;
- point the package manager at a Gateway source;
- configure the internal Git route rewrite for GitHub HTTPS clones;
- rewrite static downloads precisely per `download-gateway-plan.json`;
- not write internal Gateway default addresses into open-source projects; the runtime environment injects internal entry points;
- keep network-failure logs and a locatable task output directory for the first round.

A pre-heat success only proves the corresponding dependency entry point works; it does not substitute for a real task image build. Finally pick representative tasks for a full prebuild smoke, and distinguish the validation scope for base images, packages, Git, direct downloads, and other runtime integrations.

## Static Analysis Boundaries

The tool can recognize Dockerfiles, common install commands, referenced scripts, requirements, some heredocs, explicit `git clone`, and static curl/wget URLs. The following cases usually need manual handling:

- URLs or package names assembled at runtime;
- dependencies of remote scripts executed after download;
- unresolvable build args / environment variables;
- indirect scripts not referenced by the Dockerfile;
- artifacts chosen dynamically in lockfiles or custom tooling;
- private Registries, private Git repositories, and downloads needing business credentials.
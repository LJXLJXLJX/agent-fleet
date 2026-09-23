# YiCloud OpenSandbox Quick Start

This guide runs a local Harbor benchmark framework task in a YiCloud
OpenSandbox instance. The
runner builds or reuses the task image, uploads the agent runtime, executes the
agent and verifier, collects the result, and deletes the instance.

## Prerequisites

- Install Docker with Buildx and log in to the target image registry.
- Prepare a local Harbor benchmark framework dataset whose tasks contain a
  Dockerfile or the
  supported Compose subset.
- Obtain YiCloud API credentials, a project name, and an OpenSandbox
  environment ID.
- Make the model gateway reachable from OpenSandbox.
- For the S3 upload backend, provide an `s3cmd` configuration and a writable
  bucket.

Compose tasks use a constrained service group: all services are created in the
same dedicated environment, receive a managed `/etc/hosts` alias block, and
must pass the capability gate. Shared volumes, fixed IPs, multiple networks,
and privileged/capability requests are rejected before any Sandbox is created.

## Configure

Copy the committed configuration template, then keep all credentials in the
git-ignored local file:

```bash
cp config.env config.local.env
```

Set at least the following values in `config.local.env`:

```bash
BASE_URL=https://model-gateway.example.com
API_KEY=your-model-api-key
MODEL=your-model-id

RL_ENVIRONMENT_TYPE=opensandbox
YICLOUD_PUBLIC_KEY=your-yicloud-public-key
YICLOUD_SECRET_KEY=your-yicloud-secret-key
YICLOUD_PROJECT_NAME=your-project
YICLOUD_SANDBOX_ENVIRONMENT_ID=env-xxxxxxxx-xxx

YICLOUD_HARBOR_HOST=harbor.example.internal
YICLOUD_HARBOR_PROJECT=seta
YICLOUD_HARBOR_USERNAME=your-harbor-username
YICLOUD_HARBOR_PASSWORD=your-harbor-password
# Set to 1 after the host trusts the OCI Registry certificate. Current YiCloud ingress
# is verified with 0.
YICLOUD_HARBOR_TLS_VERIFY=0

YICLOUD_SANDBOX_S3_PROFILE=provider-name
```

After saving the OpenSandbox backend configuration, run setup once from the
repository root. This prepares Go 1.25.4 for cold frontend builds from the
configured `HARBOR_TASK_IMAGE_GOPROXY` or its domestic default:

```bash
./scripts/setup.sh
```

Each provider is an ignored, project-local profile:

```text
.s3-profiles/provider-name/
├── profile.env
└── s3cfg          # optional; development-host write credentials
```

`profile.env` identifies one bucket, credential-free Sandbox read origin, and
immutable object prefix:

```bash
YICLOUD_SANDBOX_S3_BUCKET=your-bucket
YICLOUD_SANDBOX_S3_READ_ORIGIN=http://s3.internal.example/your-bucket
YICLOUD_SANDBOX_S3_PREFIX=agent-fleet-upload/v1
```

Create a separate directory for every S3 provider. Maintainers who publish new
objects may add a sibling `s3cfg` containing least-privilege write credentials;
read-only users should omit that file. The read origin must already address the
bucket and must not contain credentials, query parameters, or fragments. The
bucket policy must allow anonymous `GetObject` from Sandbox networks without
granting anonymous writes.

Agent Fleet computes the immutable object key locally and checks its ordinary
anonymous URL first. An existing object therefore requires no S3 key. Some S3
providers return `403` rather than `404` for a missing object when anonymous
`ListBucket` is intentionally disabled. In that case, a configured writer uses
an authenticated exact-key listing to distinguish a miss from an existing
object whose anonymous-read policy is broken. Writer credentials must therefore
include `ListBucket` restricted to the configured prefix as well as permission
to publish objects there. The `s3cfg` must be a regular file that is not group-
or world-readable. In `auto` mode, a missing object without a safe write
configuration uses the existing HTTP transport instead. In strict `s3` mode,
the same condition is an error.
Switch providers only by changing
`YICLOUD_SANDBOX_S3_PROFILE` in `config.local.env`. Agent Fleet rejects path
traversal, symlinked profile files, unknown or duplicate metadata keys, and
standalone S3 values that conflict with the selected profile. Selecting a
profile defaults `YICLOUD_SANDBOX_UPLOAD_BACKEND` to `auto`: S3 is preferred,
while an S3 staging or Sandbox materialization failure falls back to the
existing authenticated HTTP upload path. Set the value explicitly to `s3`
when failure must be strict. Without a profile, the bucket and read origin may
be configured directly; `YICLOUD_SANDBOX_S3_CONFIG` remains an optional legacy
write configuration.

Every YiCloud OpenSandbox create request uses the provider-required
`["sleep", "infinity"]` entrypoint. After the Sandbox reaches `Running` and
service aliases are installed, Agent Fleet launches the Bundle's resolved
start command through execd. This changes only OpenSandbox startup; task images
and other environment backends retain their original behavior.

Use the immutable environment ID in automation. The runner rejects requests
without an explicit environment ID or exact environment name.

## Run One Task

Start with one worker and one task:

```bash
cd Agents/utils/common/Harbor

AGENT=claude-code \
DATASET_NAME=auto \
DATASET_PATH=/absolute/path/to/Harbor-Dataset \
HARBOR_INCLUDE_TASKS=0 \
TOTAL_WORKERS=1 \
HARBOR_N_CONCURRENT=1 \
bash start.sh
```

The command prints the output and summary paths. For debugging, add
`YICLOUD_SANDBOX_RETAIN_AFTER_TRIAL=1` and delete the retained instance after
inspection.

Image preparation lists the task repository, validates the selected tag
against the local content hash, and publishes a digest reference. The contract
is [image preparation](task_image_manager/task_bundle/README.md#identity-and-cache).
`HARBOR_TASK_IMAGE_VALIDATE_HASH=0` or `--no-validate-image-hash` skips
that tag check for on-demand runs.

## Optional: Prebuild Task Images

For a batch run, publish task images once before starting workers:

```bash
set -a
source config.local.env
set +a

# Required for Dockerfile RUN downloads from upstream release hosts.  Pin the
# development machine's local proxy instead of allowing a shell helper to
# fall back to a forwarded proxy; BuildKit defaults to host networking.
export HARBOR_TASK_IMAGE_BUILD_PROXY_URL=http://127.0.0.1:7890

HARBOR_TASK_IMAGE_PREBUILD_CONCURRENCY=4 \
bash Agents/utils/common/Harbor/task_image_manager/prebuild_dataset.sh \
  /absolute/path/to/Harbor-Dataset seta
```

Datasets that intentionally keep internal builder locations out of their
Dockerfiles may use logical base names and resolve them only during image
preparation:

```bash
HARBOR_TASK_IMAGE_BASE_IMAGE_REGISTRY=harbor.example.internal/agent-fleet-task-base \
bash Agents/utils/common/Harbor/task_image_manager/prebuild_dataset.sh \
  /absolute/path/to/Harbor-Dataset benchmark-project
```

Base-image pinning, digest references, and the local uploaded-Bundle index are
specified in
[image preparation](task_image_manager/build_engine/README.md#build-time-inputs). For a
stable dataset, the local index can skip content hashing:

```bash
HARBOR_TASK_IMAGE_PREBUILD_SKIP_HASH_VERIFICATION=1 \
bash Agents/utils/common/Harbor/task_image_manager/prebuild_dataset.sh \
  /absolute/path/to/Harbor-Dataset seta
```

Leave skip-hash verification at its default `0` to keep local hash validation.
`HARBOR_TASK_IMAGE_PREBUILD_USE_LOCAL_UPLOAD_CACHE=0` bypasses the local index.

APT, `curl`/`wget`, GitHub mirror, and package-index routing are build-time
behavior of image preparation. Set one Gateway root when those routes should
be used:

```bash
export HARBOR_TASK_IMAGE_DOWNLOAD_SOURCE_URL="http://<TRUSTED_SOURCE>/cache"
```

The route format, bypass rules, and the prohibition on calling the Gateway
from task Dockerfiles or ordinary Sandboxes are in
[image preparation](task_image_manager/build_engine/README.md#apt-and-download-interception)
and the [instrumentation frontend](task_image_manager/build_engine/frontend/README.md).
A shared frontend preparation failure stops further task dispatch for that
batch. Package indexes such as pip, npm, Go, Cargo, Rustup, Dart Pub, and
Julia stay on their build-argument settings, documented with the other
build-time inputs.

Prebuild performs a bounded BuildKit cache prune before starting and every 30
minutes while it runs. Defaults are `max-used-space=500GB`,
`min-free-space=300GB`, and `reserved-space=100GB`; all four values are
configurable through `HARBOR_TASK_IMAGE_PREBUILD_GC_*`. Set
`HARBOR_TASK_IMAGE_PREBUILD_GC_INTERVAL_SEC=0` to disable only periodic GC;
the initial prune still runs for every non-dry-run batch. It prunes only unused
BuildKit cache and does not delete images, containers, volumes, or artifacts
already published to the OCI Registry.

Already published content-addressed images are reused in the task-specific
repository, and every current run still gets its per-task Bundle Manifest even
on a local cache hit. Unsupported environment definitions are listed as
skipped in the prebuild report; unsupported runtime capabilities fail before
creation.

## Troubleshooting

- A scheduling timeout or long `Pending` state is a platform capacity issue;
  the log includes the Sandbox ID and latest status.
- An image preparation failure occurs before Sandbox creation. Verify Buildx,
  registry login, and the task Dockerfile.
- An artifact transport failure should be diagnosed separately from agent
  execution. Existing objects require only the anonymous read URL and bucket
  policy; publishing a missing object additionally requires a safe local
  `s3cfg`. Also verify DNS from the relevant host and Sandbox networks.
- A model request failure means the instance started, but its configured model
  gateway is unreachable or rejected the request.

See [image preparation](task_image_manager/README.md) for image naming,
caching, and registry publication. See [Harbor benchmark framework structure](STRUCT.md) for the full
configuration reference.

## Bounded artifact downloads

File downloads read 512 KiB chunks through the existing exec transport, write
straight to a temporary host file, and check the source size and SHA-256 before
replacing the target. Failed, corrupt or cancelled transfers preserve an
existing target and remove the partial file. This requires the image's existing
GNU-style `stat`, `sha256sum`, `dd` and `base64` utilities; no S3 write credentials
or new provider API is required.

Directory downloads create one temporary sandbox tar archive, use the same
bounded transfer, extract with Python's `data` safety filter, and remove the
remote archive in `finally`. Excluded-directory downloads use this path too.
Transport retries keep the existing idempotent exec behavior. A failed chunk
aborts the download after those retries; a later download starts again, rather
than claiming resumability across calls.

`HARBOR_ARTIFACT_DOWNLOAD_CONCURRENCY` defaults to `4`. Independent worker
processes running as the same host user share advisory-lock slots under
`${TMPDIR:-/tmp}/agent-fleet-artifacts-<uid>`; locks release on cancellation or
process death. `HARBOR_ARTIFACT_LOCK_DIR` can select a shared local directory.
All cooperating workers must use the same directory and concurrency setting.
Separate hosts/users or isolated temporary directories have separate limits.
Do not unlink slot files while workers are running.

The limit covers OpenSandbox download and extraction operations, not agent
execution or trajectory conversion. Encoded responses and decoded buffers are
bounded by chunk size. Archive metadata and trajectory conversion can still
consume memory proportional to entry/event counts; filesystem writes can still
occupy reclaimable page cache. Chunking adds requests and SHA-256 adds a remote
read pass, so measure collection throughput on the target provider.

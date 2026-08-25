# YiCloud OpenSandbox Quick Start

This guide runs a local Harbor benchmark framework task in a YiCloud
OpenSandbox instance. The
runner builds or reuses the task image, uploads the agent runtime, executes the
agent and verifier, collects the result, and deletes the instance.

## Prerequisites

- Run `./scripts/setup.sh` once from the repository root.
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

YICLOUD_SANDBOX_UPLOAD_BACKEND=s3
YICLOUD_SANDBOX_S3_CONFIG=/absolute/path/to/s3cfg
YICLOUD_SANDBOX_S3_BUCKET=your-bucket
```

Use the immutable environment ID in automation. The runner rejects requests
without an explicit environment ID or exact environment name.

## Run One Task

Start with one worker and one task:

```bash
cd Agents/utils/common/Harbor

AGENT=claude-code \
DATASET_NAME=auto \
DATASET_PATH=/absolute/path/to/Harbor-Dataset \
INCLUDE_TASKS=0 \
TOTAL_WORKERS=1 \
HARBOR_N_CONCURRENT=1 \
bash start.sh
```

The command prints the output and summary paths. For debugging, add
`YICLOUD_SANDBOX_RETAIN_AFTER_TRIAL=1` and delete the retained instance after
inspection.

## Optional: Prebuild Task Images

For a batch run, publish task images once before starting workers:

```bash
set -a
source config.local.env
set +a

# Required for Dockerfile RUN downloads from upstream release hosts.  Pin the
# development machine's local proxy instead of allowing a shell helper to
# fall back to a forwarded proxy; BuildKit defaults to host networking.
export HARBOR_OPENSANDBOX_BUILD_PROXY_URL=http://127.0.0.1:7890

HARBOR_OPENSANDBOX_PREBUILD_CONCURRENCY=4 \
bash Agents/utils/common/Harbor/prebuild_opensandbox_dataset.sh \
  /absolute/path/to/Harbor-Dataset seta
```

Prebuild performs a bounded BuildKit cache prune before starting and every 30
minutes while it runs. Defaults are `max-used-space=500GB`,
`min-free-space=300GB`, and `reserved-space=100GB`; all four values are
configurable through `HARBOR_OPENSANDBOX_PREBUILD_GC_*`. Set
`HARBOR_OPENSANDBOX_PREBUILD_GC_INTERVAL_SEC=0` to disable GC. It prunes only
unused BuildKit cache and does not delete images, containers, volumes, or
artifacts already published to the OCI Registry.

Already published content-addressed images are reused in the task-specific
repository. Unsupported environment definitions are listed as skipped in the
prebuild report; unsupported runtime capabilities fail before creation.

## Troubleshooting

- A scheduling timeout or long `Pending` state is a platform capacity issue;
  the log includes the Sandbox ID and latest status.
- An image preparation failure occurs before Sandbox creation. Verify Buildx,
  registry login, and the task Dockerfile.
- An upload failure should be diagnosed separately from agent execution. Check
  S3 credentials, bucket access, DNS, and the signed download URL.
- A model request failure means the instance started, but its configured model
  gateway is unreachable or rejected the request.

See [task image management](OPENSANDBOX_IMAGE_MANAGER.md) for image naming,
caching, and registry internals. See [Harbor benchmark framework structure](STRUCT.md) for the full
configuration reference.

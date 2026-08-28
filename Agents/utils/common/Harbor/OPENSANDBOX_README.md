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

YICLOUD_SANDBOX_S3_PROFILE=provider-name
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
anonymous URL first. An existing object therefore requires no S3 key. Only a
confirmed missing object uses the optional `s3cfg`; the file must be a regular
file that is not group- or world-readable. In `auto` mode, a missing object
without a safe write configuration uses the existing HTTP transport instead.
In strict `s3` mode, the same condition is an error.
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
`HARBOR_OPENSANDBOX_PREBUILD_GC_INTERVAL_SEC=0` to disable only periodic GC;
the initial prune still runs for every non-dry-run batch. It prunes only unused
BuildKit cache and does not delete images, containers, volumes, or artifacts
already published to the OCI Registry.

Already published content-addressed images are reused in the task-specific
repository. Unsupported environment definitions are listed as skipped in the
prebuild report; unsupported runtime capabilities fail before creation.

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

See [task image management](OPENSANDBOX_IMAGE_MANAGER.md) for image naming,
caching, and registry internals. See [Harbor benchmark framework structure](STRUCT.md) for the full
configuration reference.

# OpenSandbox Bundle and Image Management

Image preparation — identity, cache, Registry publication, the Bundle manifest,
build-time inputs, and the CLI — is documented in
[image preparation](task_image_manager/README.md).
[`task_image_manager/task_cli.py`](task_image_manager/task_cli.py) is the shared command-line entry point.

The pieces around it are:

- [`compose_bundle.py`](compose_bundle.py) turns a Dockerfile into an implicit
  `main` service, or a Compose file into named services, before preparation
  starts.
- [`harboropik.sh`](harboropik.sh) calls preparation and exports
  `HARBOR_OPENSANDBOX_BUNDLE_MANIFEST` and `HARBOR_OPENSANDBOX_IMAGE_REF`.
- [`task_image_manager/prebuild_dataset.sh`](task_image_manager/prebuild_dataset.sh) prepares
  one Bundle per task. Its BuildKit cache pruning is part of that batch
  launcher, described in
  [OpenSandbox runs](OPENSANDBOX_README.md#optional-prebuild-task-images).

Preparation does not create Sandbox instances or schedule services.
`YiCloudOpenSandboxEnvironment` consumes the schema v2 Bundle, and can read a
schema v1 Bundle. It gates unsupported capabilities before creation, creates
the service group, wires a managed `/etc/hosts` block, checks healthchecks,
and routes default file and exec operations to `main`. `service_exec(name, ...)`
and `stop_service(name)` target one sidecar. Group startup failure and
`stop(delete=True)` delete every recorded Sandbox ID.

An explicit Bundle takes precedence and can supply the main image reference.
An explicit image reference without a Bundle keeps single-image behavior. Both
must be fully qualified references under `YICLOUD_HARBOR_HOST`.

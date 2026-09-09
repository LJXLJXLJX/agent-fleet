# OpenSandbox BuildKit instrumentation frontend

This component adds build-time instrumentation to dataset `RUN` instructions
without editing task Dockerfiles. APT mirror routing is its first consumer.

The frontend is a build tool, not a task base image. BuildKit runs it to lower a
Dockerfile into LLB; the frontend itself contributes no layer to the resulting
task image.

## Design

`upstream.version` pins the official BuildKit source. `build.sh` checks out that
revision, applies `dockerfile2llb.patch`, copies `instrumentation.go` into the
upstream Dockerfile frontend package, and compiles the static
`dockerfile-frontend` executable. `Dockerfile.frontend` packages that executable
in a `scratch` OCI image.

The patch adds one call immediately before upstream `dispatchRun` invokes
`llb.State.Run`. At that point upstream has already handled shell and JSON forms,
heredocs, custom shells, stage environment, and all `RUN` options. The hook does
not parse command text or rewrite the serialized LLB graph.

Every `RUN` receives these build-only inputs:

| Input | Effect |
| --- | --- |
| `PATH` | Prepends `/run/opensandbox-apt/bin` |
| `apt`, `apt-get` | Mount the same wrapper secret under both executable names |
| `source-rewriter.awk` | Mount the APT source rewriter |
| `source-map` | Mount the configured origin-to-mirror mappings |
| `frontend-identity` | Include the frontend digest in the `RUN` cache key |
| `shadow` | Provide invocation-local writable state on tmpfs |

Wrapper, rewriter, and map secret IDs include content digests because BuildKit
does not include secret contents in cache keys. The mounted files, PATH change,
and shadow state exist only while a `RUN` executes. The final image keeps the
Dockerfile's original environment and contains none of these files.

The manager supplies the local OCI layout as a named build context and selects
it with `BUILDKIT_SYNTAX`. This lets unchanged dataset Dockerfiles use the
instrumented frontend.

## Local build and cache

`ensure_frontend()` caches the OCI layout by upstream revision, patch, helper,
build scripts, and host architecture. It verifies cached OCI blobs before reuse
and serializes concurrent first builds with a per-key lock. Missing or corrupt
entries are rebuilt locally; no frontend image is pushed to or resolved from a
registry.

The default cache is `/data/harbor-runs/opensandbox-frontend`. Override it with
`HARBOR_OPENSANDBOX_FRONTEND_CACHE`. A warm cache only needs Docker/buildx. A
cold build also needs Git, GNU timeout, and the pinned Go version installed by
the repository setup flow.

Cold builds honor provider-neutral source settings:

- `HARBOR_OPENSANDBOX_GOPROXY` and `HARBOR_OPENSANDBOX_GOSUMDB` for Go modules;
- optional `HARBOR_OPENSANDBOX_GITHUB_MIRROR_URL` for the BuildKit checkout,
  with official GitHub used when unset.

The prebuild launcher treats frontend preparation as shared infrastructure. A
failure returns exit code 78, stops new task dispatch, and is recorded once for
the batch instead of becoming many unrelated task failures. Fully cached task
batches do not prepare the frontend.

## Runtime boundary

`../opensandbox_apt_runtime/apt-wrapper.sh` and `source-rewriter.awk` implement
APT routing. Normal PATH lookup of `apt` and `apt-get` reaches the mounted
wrapper, which creates shadow source files, invokes the real system binary, and
reconciles indexes without changing authored sources in the image.

The mechanism does not grant privileges or install dependencies. Absolute
`/usr/bin/apt-get`, PATH replacement, `env -i`, direct `execve`, libapt,
python-apt, aptitude, and nala can bypass it. A stage invoking the wrapper must
contain `/bin/bash`. Custom Dockerfile frontends may also be incompatible because
the manager deliberately selects this pinned frontend.

Instrumentation identities invalidate BuildKit `RUN` cache when a build occurs.
They do not bypass the existing immutable task-image lookup; rebuilding an
already published task still uses the normal force-rebuild workflow.

## Validation and upgrades

`tests/test_frontend.py` covers orchestration and corpus preservation.
`scripts/test-lowering.sh` runs the shared corpus through pristine and patched
BuildKit source trees, compares decoded ExecOps and image configuration, and
checks cache-key separation for each runtime input.

```bash
PYTHONPATH=. python -m unittest discover \
  -s Agents/utils/common/Harbor/opensandbox_buildkit_frontend/tests \
  -p test_frontend.py -v

FRONTEND_WORK_DIR=/data/<completed-frontend-work-dir> \
  bash Agents/utils/common/Harbor/opensandbox_buildkit_frontend/scripts/test-lowering.sh
```

For an upstream upgrade, change the tag and commit together, reapply the small
`dispatchRun` hook, review upstream `State.Run` and mount semantics, compile with
the pinned toolchain, and run both checks above. If an upgrade requires hooks in
multiple semantic states or post-processing the LLB graph, reassess the design
instead of expanding this patch mechanically.

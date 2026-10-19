# Harbor environment adapters

This package owns backend selection and the existing YiCloud/qz runtime
implementations. The old modules retain the Harbor import paths as compatibility
imports of the new classes.

`core/config.py` contains the immutable `BackendPlan` recovered and adapted from
`archive/harbor-environment-adapters-phase1`. It records the environment type,
exact Harbor spec. It does not retain credentials
or other runtime configuration, import SDKs, or create remote resources.

## Configuration handoff

1. `env.sh` merges public, local, and caller configuration using the existing
   config loader, then applies rollout/template defaults.
2. `env/environment_config.sh` calls `backend_plan.py` with `python3 -S` so the
   configuration helper cannot activate provider startup hooks prematurely.
3. The helper exports `HARBOR_BACKEND_PLAN` (schema 1 JSON), together with
   `HARBOR_ENVIRONMENT_TYPE` and `HARBOR_ENVIRONMENT_SPEC` for existing consumers.
4. Fixed and registry command builders and backend preflight use those projected
   variables. Re-sourcing `env.sh` reuses the plan while type/spec are unchanged.
   Changes to those legacy inputs produce a new plan.
5. The legacy rollout worker resolves a plan inside each request's subshell.
   Its historical spec override policy is preserved: qz/OpenSandbox and prebuilt
   E2B use their configured adapters; changing backend clears the listener spec;
   a request for the listener's default Docker or ordinary E2B retains a custom
   host spec. Request payloads cannot set import paths or credentials.
6. Shared `sitecustomize.py` consumes the serialized plan. Direct Python callers
   without a plan retain the old Docker default and hook normalization.
   Native rollout consumes the already prepared Harbor TrialConfig, retaining
   its existing Claude Code/OpenSandbox restriction.

`HARBOR_BACKEND_PLAN` is an internal process handoff, not a new configuration
input users need to set. Old variables and `module:Class` specs remain supported.
qz authentication, host patches, templates, verifier injection, and runtime
delivery are unchanged. Provider checks still run in the existing preflight.

## Runtime implementations

`providers/opensandbox_yicloud/adapter.py` composes the extracted internal method
groups into the concrete Harbor `BaseEnvironment`. All method groups share that
one adapter instance and its existing state; they are not pluggable providers or
new lifecycle/transfer interfaces. See [YiCloud responsibilities](providers/opensandbox_yicloud/README.md).

`providers/e2b_series/qz_legacy/adapter.py` preserves the qz implementation. Its old
`qz_e2b_sandbox.py` entrypoint re-exports the class and public qz configuration
helpers. Official E2B continues using Harbor's `E2BEnvironment` directly. See
[E2B series boundaries](providers/e2b_series/README.md).

`e2b_series` groups related implementations inside `providers/`, alongside the
YiCloud provider. It is not a separate top-level category of environments.

This PR covers BackendPlan from phase 4 and provider extraction from phase 5.
Unused `provider`/`implementation` plan fields have been removed. Existing
Harbor capabilities and the provider's lifecycle/transfer implementations remain
the contracts; additional core abstractions require actual consumers. Further
Shell reduction is subsequent work.

Local regression coverage lives in `tests/test_backend_plan.py`, including a
frozen Shell selection fixture and the legacy rollout override policy. Existing
qz, YiCloud, E2B runtime, configuration, command, and rollout suites protect the
callers. `test_environment_adapter_imports.py` uses installed Harbor/E2B APIs in
isolated processes to check that old and new class imports are identical,
concrete Harbor environments, and that importing qz does not patch official E2B.
These tests do not establish live provider availability.

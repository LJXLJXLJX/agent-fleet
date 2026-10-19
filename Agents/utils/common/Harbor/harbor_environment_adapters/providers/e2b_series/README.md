# Existing E2B-compatible implementations

Official E2B continues using Harbor's own `harbor.environments.e2b.E2BEnvironment`
and the official `e2b` SDK. Its prebuilt-template path still uses the existing
`e2b_prebuilt:PrebuiltE2BEnvironment`; no copied official adapter is added.

The qz implementation is preserved in `qz_legacy/adapter.py`. The old
`qz_e2b_sandbox:QzSandboxEnvironment` import path re-exports this same class.
Authentication, endpoint mapping, envd host patch, template selection/task init,
four-hour timeout cap, allocation retry boundary, and existing runtime/verifier
hooks retain their behavior. Import alone does not apply qz SDK patches;
initialization/preflight still own those legacy changes.

Do not use qz-specific patches as a base for new platforms. No AgentENV adapter,
profile, directory, capability declaration, or placeholder is introduced.

For qz changes, run the existing qz and E2B runtime local suites before and after
the change. Fake SDK, mock HTTP, and local Shell tests protect compatibility;
real qz Sandbox creation is not a migration gate and these tests do not claim
live platform availability.

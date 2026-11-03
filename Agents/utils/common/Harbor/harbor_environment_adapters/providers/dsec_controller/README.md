# DSec Controller provider

This provider implements Harbor's environment lifecycle through the public DSec
Controller API. Agent Fleet supplies an image reference, starts the sandbox,
executes commands, transfers files, and closes the sandbox; DSec owns placement,
image loading, EROFS mounts, toolkit composition, and garbage collection.

The scale follow-up keeps that boundary intact while adding asynchronous HTTP
transport and a process-wide create-to-ready admission limit. Set
`DSEC_SANDBOX_START_CONCURRENCY` to tune the limit; the default is `32`.
Resource requests use DSec's unrestricted policy because this integration does
not currently enforce per-sandbox CPU, memory, PID, or storage quotas.

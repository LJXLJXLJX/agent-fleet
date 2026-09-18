# STRUCTS.md — Directory structure and file purposes

Date: 2026-09-16 (created after the Phase 1 directory refactor). This document stays in sync
with the code tree: when files are added, moved, renamed or deleted, this document must be
updated in the same commit (see `AGENTS.md` constraint 2).
Generated/local files (`build/`, `*.egg-info`, `__pycache__/`, `config.local.env` and other
Git-ignored items) are outside the scope of this document; the `tests/` directory does not
maintain per-file descriptions.

```
artifact-cache-gateway/
├── AGENTS.md                     # Agent work constraints for this repository (README mandatory rules, red lines, common commands)
├── STRUCTS.md                    # This document: directory tree and file purposes
├── README.md                     # Project overview: capabilities, client entry points, cache semantics
├── setup.cfg                     # setuptools config: packages list, four console_scripts entry points
├── pyproject.toml                # PEP 517 build declaration (setuptools backend)
├── Dockerfile                    # Base image build (local/dev use; hosted via deploy/Dockerfile.hosted)
├── .dockerignore / .gitignore    # Exclusion rules for Docker builds and Git
│
├── config/                       # Server-side source configuration (see the directory README)
│   └── sources.json              #   Source manifest: upstream/validation/publish policy across 9 ecosystems and 13 kinds
│
├── deploy/                       # hosted-mode server deployment (see the directory README)
│   ├── Dockerfile.hosted         #   Hosted image build (assembles the server on top of the base image)
│   ├── compose.yaml              #   Compose deploy description
│   ├── hosted-entrypoint.sh      #   Container entry script
│   ├── hosted_supervisor.py      #   Process supervision: starts and watches the gateway and its subordinate processes
│   └── install_mihomo.py         #   Server-side upstream proxy component install
│
├── docs/                         # Project documentation (see the directory README)
│   ├── developer-guide.md        #   Developer guide: structure, modifications and verification
│   ├── user-guide.md             #   User guide: client onboarding for each ecosystem
│   ├── operator-guide.md         #   Operator guide: deployment, source review, S3, failure handling
│   └── dataset-preparation.md    #   Dataset preparation toolchain usage
│
├── scripts/                      # Helper scripts (see the directory README)
│   ├── start-gateway.sh          #   Start the Gateway service process locally/in a container
│   └── migrate_source_names.py   #   One-time data migration for a historical source-naming change (no new ones expected)
│
└── src/                          # artifact_cache_gateway package (package_dir mapping, see src/README.md)
    ├── __init__.py               # Package marker
    ├── __main__.py               # python -m artifact_cache_gateway entry → cli/main.py
    │
    ├── core/                     # Base layer: no business dependencies (see directory README; ecosystems runtime hooks have no gateway imports but keep TYPE_CHECKING upward type references and field-semantic dependencies)
    │   ├── config/               #   Source configuration package (formerly config.py; __init__ re-exports ConfigError/GatewayConfig/SourceConfig/UpstreamConfig and loaders)
    │   │   ├── __init__.py       #     Public-name re-exports
    │   │   ├── errors.py         #     ConfigError (re-export; defined in ../exceptions.py)
    │   │   ├── _shared.py        #     _origin (re-export point of ecosystems/_origin.py)
    │   │   ├── upstream.py       #     UpstreamConfig and upstream parsing helpers
    │   │   ├── source.py         #     SourceConfig, kind/ecosystem definitions, build_url/fetch_candidates
    │   │   └── gateway.py        #     GatewayConfig, default_config, load_config and other loaders
    │   ├── exceptions.py         #   Shared core-layer exception leaves (ConfigError defined here; re-exported by config/errors.py.
    │   │                           #   Leaf modules guarantee no cycles for any ecosystems↔config import order, 2026-09-17)
    │   ├── ecosystems/           #   The only ecosystem plugin directory (finalized on 2026-09-17: kinds/ + gateway/ecosystems/ merged; former validators/rewrites/inventory/engine scatter migrated in)
    │   │   ├── base.py           #     EcosystemHandler (all-optional hooks) and default semantics
    │   │   ├── __init__.py       #     Aggregates the HANDLER constant, building the kind/ecosystem dual index once
    │   │   ├── _origin.py        #     _origin (re-exported here by config/_shared.py)
    │   │   ├── pip/node/go/cargo/dart/julia/download/apt.py  # All hooks per ecosystem + HANDLER constant
    │   ├── source_naming.py      #   Source naming and route parsing (parse_*_route, frozen_download naming)
    │   ├── logging_utils.py      #   Logging initialization utilities
    │   └── local_env.py          #   Local environment probing
    │
    ├── storage/                  # Storage contract layer: depends only on core, no gateway dependency (Phase 3 decoupling on 2026-09-17; layer guard in tests/test_storage_contract.py)
    │   ├── base.py               #   CacheEntry, Storage protocol, StorageError (storage abstraction definitions)
    │   ├── inventory.py          #   CacheObject/InventoryListing data contracts (migrated from gateway/inventory.py)
    │   ├── request_stats.py      #   RequestStatsSession and schema 1-4 validation (migrated from gateway/request_stats.py)
    │   ├── gpfs.py               #   FileStorage: local/GPFS file-tree storage (url_key/blob_path/metadata_path)
    │   └── s3.py                 #   S3Storage: S3 object storage (blob_key/metadata_key/inventory_key)
    │
    ├── gateway/                  # Gateway runtime (server side, see the directory README)
    │   ├── engine/               #   Gateway cache engine package (formerly engine.py; __init__ re-exports Gateway/CacheResult)
    │   │   ├── result.py         #     CacheResult
    │   │   ├── _common.py        #     _HIT_RATE_LOGGER
    │   │   ├── _stats.py         #     RequestStats: standalone request-stats component (state/lock/checkpoint thread/shutdown;
    │   │   │                       #     composed and held by Gateway, delegated via record/stats/close; persistence contract in storage/request_stats.py)
    │   │   ├── _inventory.py     #     _InventoryMixin (inventory documents and backfill)
    │   │   ├── _failures.py      #     _FailureMixin (recent-failure tracking)
    │   │   └── core.py           #     Gateway(_InventoryMixin, _FailureMixin) + composed RequestStats:
    │   │                           #     __init__, resolve*, publish policy (red lines)
    │   ├── fetcher.py            #   Fetcher: origin downloads (retries/proxy/cache key/TTL/Range)
    │   ├── server.py             #   HTTP server: routing, web UI mounting, status and health checks
    │   ├── rewrites.py           #   Common HTML-index rewrite backbone (kind-specific hooks via ecosystem prepare_html)
    │   ├── git_http.py           #   Git Smart HTTP request-body parsing (chunked/gzip)
    │   ├── inventory.py          #   cache_object construction (ecosystem hook dispatch; data contracts in storage/inventory.py)
    │   ├── request_stats.py      #   ecosystem → stats module-name grouping and empty_modules (session/schema in storage/request_stats.py)
    │   ├── status.py             #   Health/status endpoint logic
    │   └── services/             #   Server-side ecosystem adapters with standalone implementations (other ecosystems are proxied by the engine+fetcher generic layer)
    │       ├── apt.py            #     APT repository gateway (formerly apt_gateway.py)
    │       ├── git.py            #     GitHub bare mirror (formerly git_mirror.py)
    │       └── image.py          #     OCI image mirror plan (formerly image_mirror.py)
    │
    ├── harbor_tasks/             # Harbor task dataset toolchain (see the directory README)
    │   ├── analyzer/             #   Dataset analyzer (split from dataset_analyzer.py; __init__ carries analyze + re-exports)
    │   │   ├── __init__.py       #     Pipeline orchestration analyze() and all public-name re-exports
    │   │   ├── models.py         #     Data model dataclasses and stable-id helpers
    │   │   ├── shell.py          #     Shell lexing core (heredocs/logical lines/command splitting)
    │   │   ├── dockerfile.py     #     Dockerfile parsing and build-context mapping
    │   │   ├── apt.py            #     APT dependency and repository-declaration recognition
    │   │   ├── external.py       #     External build-input (URL/git clone) recognition
    │   │   ├── packages.py       #     Install-command recognition and option parsing
    │   │   ├── report.py         #     Markdown report and write_outputs persistence
    │   │   └── cli.py            #     parse_args/main (module docstring is the CLI description)
    │   └── preparer/             #   Preparation-plan generation and warm-up (formerly preparation/)
    │       ├── cli.py            #     artifact-cache-prepare entry (main dispatches to subcommand modules)
    │       ├── _defaults.py      #     Default path constants
    │       ├── parser.py         #     Argument parsing (parser construction and shared argument groups)
    │       ├── settings.py       #     Run-settings loading and analysis-plan generation
    │       ├── commands.py       #     analyze / mirror-images / prepare subcommands
    │       ├── warm_commands.py  #     warm-* / configure-* maintenance subcommands
    │       ├── models.py         #     Plan/result data models
    │       ├── orchestrator.py   #     Warm-up orchestration (warm_packages and so on)
    │       ├── report.py         #     Reviewable report generation
    │       ├── direct_download/  #     Frozen-download plans and caching
    │       │   ├── plan.py       #       download-gateway-plan generation (versioned gateway_path)
    │       │   └── cache.py      #       Warm-cache writes for direct-download objects
    │       └── providers/        #     Per-ecosystem probe/warm-up during preparation (a separate concern from the server-side ecosystems)
    │           ├── base.py       #       Provider protocol (ProbeResult/WarmResult and so on)
    │           ├── apt/          #       APT provider package (formerly apt.py; resolver_script.sh resource)
    │           │   ├── environments.py / resolver.py / repository.py
    │           │   ├── probe.py (_ProbeMixin) / warm.py (_WarmMixin) / provider.py
    │           │   └── resolver_script.sh
    │           ├── pip/          #       pip provider package (formerly pip.py; resolver_script.py resource)
    │           │   ├── environments.py / resolver.py / provider.py
    │           │   └── resolver_script.py
    │           ├── npm.py        #       npm provider (not yet packaged)
    │           └── npm_resolver.js  #    npm resolver script resource
    │
    ├── ui/                       # Operations web interface (mounted by server.py)
    │   ├── webui.py              #   WebUIAsset/webui_asset (loads static assets via importlib.resources)
    │   └── static/               #   UI static assets (index.html / styles.css / app.js)
    │
    └── cli/                      # console command implementations (see the directory README)
        ├── main.py               #   artifact-cache-gateway (formerly cli.py)
        ├── inventory.py          #   artifact-cache-inventory (formerly inventory_cli.py)
        └── stats.py              #   artifact-cache-stats (formerly stats_cli.py)
```

## Compatibility red lines at a glance

The following functions determine the already-persisted data layout; they may only be moved
verbatim / have their import paths changed, with no behavior change allowed (golden tests live
in `tests/`): `FileStorage.url_key/blob_path/metadata_path`, `S3Storage`'s
`blob_key/metadata_key/inventory_key/inventory_prefix`, all functions in
`core/source_naming.py`, `SourceConfig.build_url`, and the frozen/transparent publish policies
and `_should_publish` in `gateway/engine/core.py`.
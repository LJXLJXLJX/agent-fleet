# core/ecosystems/

The single ecosystem plugin directory (core layer; finalized 2026-09-17: the former
`core/kinds/` + `gateway/ecosystems/` two layers were merged into a single registry).
All hooks are pure functions and duck-type source/result at runtime; runtime dependencies are
limited to the stdlib and the `core/exceptions.py` leaf module (`core/config/errors.py` is only a
re-export of `ConfigError`); SourceConfig/CacheResult/CacheEntry are referenced only under
TYPE_CHECKING and do not import gateway runtime types.

- `base.py`: `EcosystemHandler` frozen dataclass + default semantics (see table below).
- `__init__.py`: aggregates the `HANDLER` constants from each module, building both the
  `KIND_HANDLERS` / `ECOSYSTEM_HANDLERS` indexes in one pass; exports `handler_for_kind`,
  `handler_for_ecosystem`, and the convenience functions `always_publish(source)`,
  `display_name(source, request_url)`.
- `pip.py` / `node.py` / `go.py` / `cargo.py` / `dart.py` / `julia.py` /
  `download.py` / `apt.py`: all hooks for each ecosystem; each file ends by exporting a single
  `HANDLER = EcosystemHandler(...)` constant (no register side effects).
- `_origin.py`: the `_origin` pure function (re-exported here from `config/_shared.py`).

## Hook reference

| Hook | Signature | Default | Call site |
| --- | --- | --- | --- |
| `validate_path` | `(source, decoded) -> str \| None` | `None` (no validation) | `config/source.py build_url`: raises ConfigError on a returned error message (verbatim message) |
| `validate_config` | `(source, sources) -> str \| None` | `None` | `config/gateway.py __post_init__`: cross-source constraints (called once per former trigger location) |
| `prepare_html` | `(source, rewritten) -> bytes` | `None` | `gateway/rewrites.py rewrite_index`: kind pre-processing before HTML trunk rewriting |
| `rewrite_matches` | `(result, entry) -> bool` | always `False` | `gateway/server.py _serve_result_body`: decides kind-specialized rewriting |
| `rewrite` | `(content, result, *, gateway_origin, archive_source=None) -> bytes` | `None` | Applied when the above returns true |
| `rewrite_size_limit` | `int` | `0` | byte limit for rewriting |
| `inventory_fields` | `(source, decoded_path, filename, content_type) -> dict` | `None` (trunk default fields) | `gateway/inventory.py cache_object` |
| `display_name` | `(source, request_url) -> str \| None` | `None` (falls back to `source.name`) | `gateway/engine/core.py source_display_name` |
| `always_publish` | `(source) -> bool` | always `False` | `gateway/engine/core.py` publish path (skips `_should_publish`) |

Multi-kind ecosystems (go/cargo) dispatch inside their module by `source.kind`
(e.g. `_validate_path`), and `HANDLER.kinds` lists all kinds; ecosystems without hooks at the
kind level (e.g. pypi generic's path admission is carried by the build_url trunk generic
allowlist) leave the corresponding fields at their defaults. **Do not create empty shell modules
for hook-less kinds** — `generic`/`static-objects`/`apt-repository` having no handler is a normal
state; apt's dynamic routing service is implemented in `gateway/services/apt.py`, while this
directory's `apt.py` only holds hooks.

## Adding an ecosystem guide

**Boilerplate** (a hypothetical ecosystem filling only `validate_path` and `inventory_fields`;
a copy-and-fill skeleton):

```python
"""myeco ecosystem hooks."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .base import EcosystemHandler

if TYPE_CHECKING:
    from ..config.source import SourceConfig

_MY_PACKAGE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


def _validate_path(source: SourceConfig, decoded: str) -> str | None:
    segments = decoded.split("/")
    if len(segments) != 2 or not _MY_PACKAGE.fullmatch(segments[0]):
        return "upstream path does not match the myeco protocol allowlist"
    return None


def _inventory_fields(
    source: SourceConfig, decoded_path: str, filename: str, content_type: str
) -> dict[str, str | None]:
    package, _, version = decoded_path.partition("/")
    return {"package": package, "version": version or "@unversioned",
            "object_type": "artifact"}


HANDLER = EcosystemHandler(
    name="myeco",
    kinds=("myeco-registry",),
    ecosystems=("myeco",),
    validate_path=_validate_path,
    inventory_fields=_inventory_fields,
)
```

**Checklist**:

1. Copy the boilerplate to `core/ecosystems/<name>.py`, filling only the hooks you need
   (leave the rest at defaults) — that is the only new code
2. Add a `<name>.HANDLER` line to the `_HANDLERS` tuple in `__init__.py`
3. Add the enumerations to `_SOURCE_KINDS` / `_SOURCE_ECOSYSTEMS` in `config/source.py`
   (when adding a new kind)
4. Add source data to `config/sources.json`
5. Optional: `gateway/services/` (ecosystems with a standalone service implementation,
   like apt/git/image), `harbor_tasks/preparer/providers/` (dataset tooling support)

# gateway/services/

Server-side ecosystem adaptations: only services with standby implementations live here. The
remaining ecosystems (pypi/npm/go/cargo/dart/julia/download/generic, etc.) have no standalone
module and are served by the generic proxy of `engine.py` + `fetcher.py` driven by `SourceConfig`,
with the differences concentrated in the kind definitions and path validation in `core/config.py`.

- `apt.py`: prebuild APT repository plan and dynamic apt routing (formerly `apt_gateway.py`).
- `git.py`: the shared public GitHub bare mirror (formerly `git_mirror.py`).
- `image.py`: the shared public base OCI image mirror plan (formerly `image_mirror.py`).

# providers/

Per-ecosystem probe/warm-up providers for the preparation phase (used only by the preparer;
they are a separate thing from the server-side ecosystem adaptations in `gateway/services/`).

- `base.py`: the provider protocol (ProbeResult/WarmResult and so on come from `../models.py`).
- `apt/`: the APT provider package (formerly `apt.py`; the resolver script is a package-internal
  resource file).
- `pip/`: the pip provider package (formerly `pip.py`; the resolver script is a package-internal
  resource file).
- `npm.py`: the npm provider (not yet package-ized); `npm_resolver.js` is a same-directory
  resource file (loaded via importlib.resources).
- `__init__.py`: re-exports `AptProvider, NpmProvider, PipProvider`.

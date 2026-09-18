# ui/

Read-only Web UI. `webui.py` loads the static resources under `static/`
(index.html/styles.css/app.js, byte-identical to the original embedded literals) via
`importlib.resources`, served by `gateway/server.py` by path; it does not depend back on the
gateway runtime.

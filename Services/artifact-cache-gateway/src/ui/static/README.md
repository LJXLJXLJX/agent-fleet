# ui/static/

Static resources of the operations Web UI (loaded byte-for-byte via `importlib.resources` by
`../webui.py`; their content is byte-identical to the original embedded `_INDEX`/`_STYLES`/`_APP`
literals, with the hash guard in `tests/test_extracted_resources.py`).

- `index.html`: UI entry page (`/ui/`).
- `styles.css`: styles (`/ui/styles.css`).
- `app.js`: dashboard frontend logic (`/ui/app.js`).

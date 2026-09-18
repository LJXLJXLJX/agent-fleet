from __future__ import annotations

import importlib.resources
from dataclasses import dataclass


@dataclass(frozen=True)
class WebUIAsset:
    content_type: str
    body: bytes
    cache_control: str = "no-store"


_INDEX = (
    importlib.resources.files(__package__)
    .joinpath("static", "index.html")
    .read_bytes()
)


_STYLES = (
    importlib.resources.files(__package__)
    .joinpath("static", "styles.css")
    .read_bytes()
)


_APP = (
    importlib.resources.files(__package__)
    .joinpath("static", "app.js")
    .read_bytes()
)


_ASSETS = {
    "/ui/": WebUIAsset("text/html; charset=utf-8", _INDEX),
    "/ui/styles.css": WebUIAsset("text/css; charset=utf-8", _STYLES),
    "/ui/app.js": WebUIAsset("text/javascript; charset=utf-8", _APP),
}


def webui_asset(path: str) -> WebUIAsset | None:
    return _ASSETS.get(path)

from __future__ import annotations

from pathlib import Path


def read_text(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"Template not found: {path}")
    return path.read_text(encoding="utf-8")


def render_literal(template_text: str, **replacements: str) -> str:
    rendered = template_text
    for key, value in replacements.items():
        rendered = rendered.replace("{" + key + "}", value)
    return rendered


def toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')

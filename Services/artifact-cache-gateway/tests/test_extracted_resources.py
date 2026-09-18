from __future__ import annotations

import hashlib
import importlib
import unittest

MODULE_PREFIX = "artifact_cache_gateway"


def _script_sha256(dotted: str) -> str:
    module = importlib.import_module(f"{MODULE_PREFIX}.{dotted}")
    return hashlib.sha256(module._RESOLVER_SCRIPT.encode("utf-8")).hexdigest()


def _asset_sha256(name: str) -> str:
    importlib.import_module(f"{MODULE_PREFIX}.ui.webui")
    from artifact_cache_gateway.ui import webui

    return hashlib.sha256(getattr(webui, name)).hexdigest()


class ExtractedResolverScriptTest(unittest.TestCase):
    """Hash guard for the extracted resolver script resources (sha256 pinned against pre/post refactor)."""

    def test_apt_resolver_script_unchanged(self) -> None:
        self.assertEqual(
            _script_sha256(
                "harbor_tasks.preparer.providers.apt.resolver"
            ),
            "7c71d1d719c991e019736d4705d25c3d237770745de6d04b11c763d8ca649fd0",
        )

    def test_pip_resolver_script_unchanged(self) -> None:
        self.assertEqual(
            _script_sha256(
                "harbor_tasks.preparer.providers.pip.resolver"
            ),
            "3f86673638ddaa13adce56674ad5c4e12cc34034f9cd5fb57e95077920026cac",
        )

    def test_npm_resolver_script_unchanged(self) -> None:
        self.assertEqual(
            _script_sha256("harbor_tasks.preparer.providers.npm"),
            "248435609e8d60e5ddcb2614e1a69eb725ea74d03efbb0d2f9ca6db27a2b38fb",
        )


class ExtractedWebUIAssetTest(unittest.TestCase):
    """Hash guard for the extracted ui/static asset (sha256 pinned against pre/post refactor)."""

    def test_index_html_unchanged(self) -> None:
        self.assertEqual(
            _asset_sha256("_INDEX"),
            "c17ac7052c77f943683f942c22add2ac36a07093b6d592e0ec69c95e4265f77e",
        )

    def test_styles_css_unchanged(self) -> None:
        self.assertEqual(
            _asset_sha256("_STYLES"),
            "b674ad556e5061617834d7f7b9bb5073bb71166d3fbb1ae005e5aea0eaa7e0f2",
        )

    def test_app_js_unchanged(self) -> None:
        self.assertEqual(
            _asset_sha256("_APP"),
            "ebdffbaf2cf35c91ba79ff13336d2479354ec88a120c051d0b986493cf803981",
        )


if __name__ == "__main__":
    unittest.main()

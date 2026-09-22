"""Task image Bundle representation; preparation and persistence live outside."""

from .manifest import PreparedBundle, assemble_bundle_manifest

__all__ = ["PreparedBundle", "assemble_bundle_manifest"]

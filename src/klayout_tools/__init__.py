"""klayout-tools: tools for AI agents to work with IC layout."""

from importlib.metadata import version as _version

from klayout_tools.ziputils import ZipSmugglingError, safe_extract

__version__ = _version("klayout-tools")

__all__ = ["ZipSmugglingError", "__version__", "safe_extract"]

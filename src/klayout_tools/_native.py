"""Shared native-extension loader used across the ``klt`` verb modules.

``yield_sensitivity.py``, ``yield_analysis.py``, ``congestion.py``,
``mom.py``, and ``sta.py`` each defined their own near-identical
``_load_native()`` -- a ``try: import klt_<x>_native / except ImportError:
raise <X>Error(...)`` shape, differing only in the native module's name,
the ``*Error`` class raised, the ``native/<x>/`` build directory, the
alternate install command mentioned in the hint, and (for two of the five)
a trailing docs link. ``_load_native_extension`` below factors that literal
prefix out the same way ``_paths.py``'s ``_load_request_json`` /
``_load_spec_json`` do for the request/spec-loading duplication -- each
caller still owns its own ``_load_native()`` wrapper (so call sites and
existing test mocks/patches targeting ``<module>._load_native`` do not need
to change) and just forwards to this helper with its own parameters.
"""

from __future__ import annotations

import importlib
from typing import Any


def _load_native_extension(
    module_name: str,
    error_cls: type[Exception],
    build_dir: str,
    install_hint: str,
    docs_link: str | None = None,
) -> Any:
    """Import ``module_name`` (a native Rust extension), raising
    ``error_cls`` with a build-instructions message if it is not installed.

    ``build_dir`` is the ``native/<x>/`` directory ``maturin develop
    --release`` should be run from; ``install_hint`` is the alternate
    install command (e.g. ``uv sync --group yield`` or ``pip install
    ./native/mom``) quoted in the message's ``(or ...)`` clause;
    ``docs_link`` -- when given -- is appended as a trailing ``; see
    <docs_link>``, matching the two callers whose message links a docs page.
    """
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:
        message = (
            f"the {module_name} extension is not installed -- from a repo "
            f"checkout, run `maturin develop --release` inside {build_dir} "
            f"(or `{install_hint}`)"
        )
        if docs_link is not None:
            message += f"; see {docs_link}"
        raise error_cls(message) from exc

"""Resolve model inputs for local simulation, staging, and OSDI preloads."""

from __future__ import annotations

import os
from typing import Any

from . import pdk
from ._paths import resolve_relative
from .sim_errors import SimError


def resolve_lib_ref(lib: str, models: dict[str, Any], request_dir: str) -> str:
    """Resolve a library against its PDK variant or the request directory.

    Flat PDK installs use the root itself. Without a PDK, expand environment
    variables and user paths. The caller checks existence for its own field.
    """
    pdk_variant = models.get("pdk")
    pdk_root = models.get("pdk_root")
    if pdk_variant is not None or pdk_root is not None:
        try:
            resolution = pdk.find_pdk(variant=pdk_variant, root=pdk_root)
        except pdk.PdkNotFoundError as exc:
            raise SimError(str(exc)) from exc
        variant_dir = pdk.variant_dir_of(resolution) or resolution["root"]
        return lib if os.path.isabs(lib) else os.path.join(variant_dir, lib)
    return resolve_relative(lib, request_dir)

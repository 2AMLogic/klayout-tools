"""PDK-native DRC *family main script* resolution (issue #4002).

Split out of :mod:`klayout_tools.pdk` to keep that module under its per-file
size ceiling; :func:`pdk.drc_deck_file` keeps the variant-named-deck contract.
"""

from __future__ import annotations

import os
from typing import Any

from . import pdk

#: Family main scripts that need ``-rd`` globals to select a variant
#: (issue #4002): script name -> (minimum standalone KLayout version, name of
#: the ``-rd`` global that selects the variant). The floor is the PDK's own
#: README note (parallel runners need 0.30.9); KLayout 0.28.16 was observed
#: aborting this deck with "Heap lost: the DeepShapeStore container no longer
#: exists".
_NATIVE_MAIN_SCRIPTS: dict[str, tuple[str, str]] = {
    "gf180mcu.drc": ("0.30.9", "variant"),
}


def native_drc_main_script(
    variant: str | None = None, root: str | None = None
) -> dict[str, Any] | None:
    """Resolve a PDK-native DRC *family main script* that
    :func:`.pdk.drc_deck_file` does not find (issue #4002).

    gf180mcu ships ``tech/drc/gf180mcu.drc``: one ready-to-run script for all
    metal stacks, which reads a ``variant`` ``-rd`` global (``gf180mcuD``,
    ...). Returns ``{"deck_file", "deck_vars", "min_klayout_version",
    "kind", "variant"}`` -- ``deck_vars`` being the defaults the caller must
    pass as ``-rd`` -- or ``None`` when the variant ships no such script.
    Raises :class:`.pdk.PdkNotFoundError` as :func:`.pdk.find_pdk` does.
    """
    info = pdk.find_pdk(variant=variant, root=root)
    klayout_dir = info["assets"]["klayout"]
    if klayout_dir is None:
        return None
    drc_dir = pdk._klayout_drc_dir(klayout_dir)
    if drc_dir is None:
        return None
    for name, (min_version, var_name) in _NATIVE_MAIN_SCRIPTS.items():
        candidate = os.path.join(drc_dir, name)
        if os.path.isfile(candidate) and str(info["variant"]).startswith(
            name.removesuffix(".drc")
        ):
            return {
                "kind": "pdk_main_script",
                "deck_file": candidate,
                "deck_vars": {var_name: str(info["variant"])},
                "min_klayout_version": min_version,
                "variant": info["variant"],
            }
    return None

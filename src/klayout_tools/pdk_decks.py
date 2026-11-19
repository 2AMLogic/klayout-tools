"""`klt pdk decks`: list a PDK's KLayout DRC decks and LVS runsets (issues
#3816, #5827).

Split out of :mod:`klayout_tools.pdk`, which keeps a thin
:func:`klayout_tools.pdk.drc_decks` wrapper so the public library path is
unchanged.
"""

from __future__ import annotations

import os
from typing import Any

from .pdk import (
    _klayout_drc_dir,
    _klayout_lvs_dir,
    drc_deck_file,
    find_pdk,
    lvs_deck_file,
)

#: File extensions `klt pdk decks` treats as KLayout DRC decks; anything
#: else in the ``drc/`` directory (Python drivers, ...) is not listed.
_DRC_DECK_KINDS = {".lydrc": "lydrc", ".drc": "drc"}

#: A ``*.rb.drc`` file is a Ruby helper that merely carries the ``.drc``
#: extension (e.g. sky130's ``zeroarea.rb.drc``); it is reported with
#: ``kind: "helper"`` rather than hidden.
_DRC_HELPER_SUFFIX = ".rb.drc"


def drc_decks(variant: str | None = None, root: str | None = None) -> dict[str, Any]:
    """List the KLayout DRC decks and LVS runsets a variant ships, with the
    ``-rd`` globals each consumes (issues #3816, #5827).

    Scans ``libs.tech/klayout/drc/`` (or the flat-install ``tech/drc/``) for
    ``.lydrc`` / ``.drc`` files, name-sorted. ``*.rb.drc`` files are listed
    with ``kind: "helper"`` and no globals scan; other extensions (Python
    drivers etc.) are not listed. ``is_default`` marks the file
    :func:`drc_deck_file` resolves, which is what ``klt drc --pdk`` runs.

    The directly-runnable ``*.lvs`` runsets under ``libs.tech/klayout/lvs/``
    (or the flat-install ``tech/lvs/``) follow the DRC entries with
    ``kind: "lvs"``; ``is_default`` marks the one :func:`lvs_deck_file`
    resolves (what ``klt lvs --engine klayout-runset --pdk`` runs). Their
    ``globals`` omit the variables that engine sets itself
    (``lvs_runset.RESERVED_RUNSET_VARS``). ``*.lylvs`` wrappers, drivers and
    sub-directories are not listed. A caller that only wants DRC decks
    filters on ``kind``.
    ``globals`` is the best-effort static scan of
    :func:`klayout_tools.drc_klayout_engine.scan_deck_globals` -- a heuristic;
    a missing entry is not proof the deck ignores a variable.

    Returns::

        {
            "schema_version": 1,
            "pdk": <variant>,
            "root": <absolute install root>,
            "decks": [
                {"path": str, "name": str,
                 "kind": "lydrc"|"drc"|"helper"|"lvs",
                 "is_default": bool,
                 "globals": [{"name": str, "required_by_caller": bool,
                              "default": str | None}, ...]},
                ...
            ],
        }

    A variant with neither a ``drc/`` nor an ``lvs/`` directory yields an
    empty ``decks`` list.
    Raises :class:`PdkNotFoundError` when no PDK install resolves.
    """
    # Imported lazily: the engine module imports this one.
    from .drc_klayout_engine import scan_deck_globals
    from .lvs_runset import RESERVED_RUNSET_VARS

    info = find_pdk(variant=variant, root=root)
    klayout_dir = info["assets"]["klayout"]
    drc_dir = _klayout_drc_dir(klayout_dir) if klayout_dir is not None else None
    default = drc_deck_file(variant=variant, root=root)

    decks: list[dict[str, Any]] = []
    for name in sorted(os.listdir(drc_dir)) if drc_dir is not None else []:
        path = os.path.join(drc_dir or "", name)
        ext = os.path.splitext(name)[1]
        if ext not in _DRC_DECK_KINDS or not os.path.isfile(path):
            continue
        helper = name.endswith(_DRC_HELPER_SUFFIX)
        decks.append(
            {
                "path": path,
                "name": name,
                "kind": "helper" if helper else _DRC_DECK_KINDS[ext],
                "is_default": default is not None and os.path.samefile(path, default),
                "globals": [] if helper else scan_deck_globals(path),
            }
        )

    lvs_dir = _klayout_lvs_dir(klayout_dir) if klayout_dir is not None else None
    lvs_default = lvs_deck_file(variant=variant, root=root)
    for name in sorted(os.listdir(lvs_dir)) if lvs_dir is not None else []:
        path = os.path.join(lvs_dir or "", name)
        if os.path.splitext(name)[1] != ".lvs" or not os.path.isfile(path):
            continue
        decks.append(
            {
                "path": path,
                "name": name,
                "kind": "lvs",
                "is_default": lvs_default is not None
                and os.path.samefile(path, lvs_default),
                "globals": scan_deck_globals(path, extra_ignored=RESERVED_RUNSET_VARS),
            }
        )

    return {
        "schema_version": 1,
        "pdk": info["variant"],
        "root": info["root"],
        "decks": decks,
    }

"""The off-host echo check for ``options.ngspice_options`` (issue #3111).

Moved verbatim out of :mod:`klayout_tools.sim` (issue #6778's size-ratchet
release valve: that module's per-file ceiling forbids net growth, and the
regrade's merged solver options lean on this check) -- ``sim`` re-exports it
unchanged, so ``klayout_tools.sim.check_ngspice_options_echo`` and
``sim_batch``'s lazy ``from .sim import`` keep working.
"""

from __future__ import annotations

import json
from typing import Any

from .sim_errors import SimError


def check_ngspice_options_echo(request: dict[str, Any], report: dict[str, Any]) -> None:
    """Fail loudly when an off-host report did not honour
    ``options.ngspice_options`` (issue #3111).

    A runner that predates the field drops it silently; its report then
    omits ``environment.ngspice_options``. Comparing the echo with the
    declared map turns that into a named :class:`SimError`.
    """
    declared = (request.get("options") or {}).get("ngspice_options") or {}
    if not declared:
        return
    echoed = (report.get("environment") or {}).get("ngspice_options") or {}
    for name in declared:
        if echoed.get(name) != declared[name] or name not in echoed:
            raise SimError(
                f"off-host runner did not honour options.ngspice_options."
                f"{name}: declared {json.dumps(declared[name])}, runner "
                f"echoed {json.dumps(echoed.get(name))} (the runner is "
                "likely older than this client and dropped the option)"
            )
    if set(echoed) != set(declared):
        raise SimError(
            "off-host runner echoed options.ngspice_options "
            f"{json.dumps(echoed, sort_keys=True)}, which differs from the "
            f"declared {json.dumps(declared, sort_keys=True)}"
        )

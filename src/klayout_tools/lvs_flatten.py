"""Keep-list netlist flatten helper for ``klt lvs`` (issue #5322).

Split out of :mod:`klayout_tools.lvs_combine` to keep that file under its
per-file size ceiling.
"""

from __future__ import annotations

import fnmatch
from typing import Any


def flatten_keeping(netlist: Any, keep: list[str] | None) -> tuple[int, int, str]:
    """Flatten ``netlist`` in place, optionally sparing circuits named by ``keep``.

    With no ``keep`` this is exactly ``netlist.flatten()``. Otherwise every
    circuit that is *called* by another circuit is flattened into its callers,
    except those whose name matches a ``keep`` glob (case-insensitive
    ``fnmatch``) -- e.g. pin-only stubs of abstracted cells. Top circuits are
    never flattened away (there is nothing to inline them into).

    Returns ``(circuits_before, circuits_after, note)``; ``note`` is the
    disclosure suffix for the ``topology.flattened`` warning
    (``" (kept un-flattened: A, B)"``), or ``""`` when nothing was kept.
    """

    before = sum(1 for _ in netlist.each_circuit())
    if not keep:
        netlist.flatten()
        return before, sum(1 for _ in netlist.each_circuit()), ""
    patterns = [g.lower() for g in keep]
    kept: list[str] = []
    doomed = []
    for circuit in netlist.each_circuit():
        if any(fnmatch.fnmatchcase(circuit.name.lower(), g) for g in patterns):
            kept.append(circuit.name)
        elif any(True for _ in circuit.each_ref()):
            doomed.append(circuit)  # called by someone: not a top circuit
    for circuit in doomed:
        netlist.flatten_circuit(circuit)
    note = f" (kept un-flattened: {', '.join(sorted(kept))})" if kept else ""
    return before, sum(1 for _ in netlist.each_circuit()), note

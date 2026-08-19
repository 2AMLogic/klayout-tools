"""Native-Rust gate-level static timing analysis (``klt_statime_native``,
``native/statime/``, issue #809 -- Epic #704 Phase 1c) backing ``klt
synthesize``'s integrated ``sta`` report (issue #925, Epic #704 Phase 3).

Pure library, mirroring ``mom.py``'s convention: :func:`compute_critical_path`
returns plain Python data and never prints. ``native/statime/README.md``
documents the engine itself (NLDM liberty parsing, rise/fall-aware timing
graph, critical-path reporting) and the accuracy result this module's
caller reproduces through the integrated path (``docs/cli/synthesize.md``'s
``sta`` section). The Python<->Rust boundary is a single ``#[pyfunction]``,
``klt_statime_native.critical_path_json`` (``native/statime/src/lib.rs``),
taking plain scalar/string args and returning a JSON string -- this
module's job is only to load that extension, call it, and reshape its
response into the documented contract.

**Boundary condition**: :data:`DEFAULT_INPUT_TRANSITION_NS`/
:data:`DEFAULT_OUTPUT_LOAD_PF` are the *same* uniform values the spike's own
corpus comparison used (``native/statime/README.md``'s "How the comparison
was run" -- no SDC, no ``create_clock``, matching OpenSTA's own
``-unconstrained`` report mode). These are deliberately **not** derived from
``synthesize.py``'s ``_ABC_CONSTR_INPUTS`` table: that table feeds ABC's own
``set_driving_cell``/``set_load`` (a driving-*cell name* plus a load in
*femtofarads*), a different knob in different units from this engine's
``--input-transition-ns``/``--output-load-pf`` (a transition time in *ns*
and a load in *pF*) -- the two are not interchangeable, and substituting one
for the other would silently misrepresent what boundary condition was
actually used. See issue #925's curator note and
``native/statime/README.md``'s "Known simplifications" #2 for the full
caveat this module inherits unmodified, not resolves.
"""

from __future__ import annotations

import json
from typing import Any

from ._native import _load_native_extension

#: Matches the standalone ``klt-statime critical-path`` CLI's own defaults
#: (``native/statime/src/main.rs``) and the boundary condition
#: ``native/statime/README.md``'s accuracy comparison ran with -- see this
#: module's docstring for why these are not derived from any ABC-side table.
DEFAULT_INPUT_TRANSITION_NS = 0.05
DEFAULT_OUTPUT_LOAD_PF = 0.03

#: Echoed in the response's ``sta.source`` field -- names this report's
#: provenance so a caller can tell it apart from ``timing`` (ABC's own
#: ``stime -p`` pre-layout estimate, a narrower and differently-computed
#: number; see ``docs/cli/synthesize.md``'s ``timing`` section) without
#: guessing, mirroring ``_read_abc_timing``'s own ``source: "abc_stime"``
#: convention.
SOURCE = "klt_statime_native"


class StaError(Exception):
    """Raised when a critical-path analysis cannot be produced: the
    ``klt_statime_native`` extension is not installed, or the native engine
    itself could not analyze the given netlist/liberty pair (e.g. a cell
    type absent from the liberty). Callers that want ``klt synthesize``'s
    additive ``sta`` field to degrade to ``None`` rather than fail the whole
    run should catch this -- see ``synthesize.py``'s ``_read_sta_timing``.
    """


def _load_native() -> Any:
    return _load_native_extension(
        "klt_statime_native",
        StaError,
        "native/statime/",
        "pip install ./native/statime",
    )


def compute_critical_path(
    netlist_path: str,
    liberty_path: str,
    top: str,
    *,
    input_transition_ns: float = DEFAULT_INPUT_TRANSITION_NS,
    output_load_pf: float = DEFAULT_OUTPUT_LOAD_PF,
) -> dict[str, Any]:
    """Run ``klt_statime_native``'s gate-level static-timing analysis over
    ``netlist_path`` (a ``write_verilog -noattr``-produced structural
    netlist -- ``klt synthesize``'s own ``netlist_path``) against
    ``liberty_path``, analyzing module ``top``.

    Returns a dict shaped for ``klt synthesize``'s response ``sta`` field
    (see ``docs/cli/synthesize.md``):

    - ``source`` (:data:`SOURCE`), ``input_transition_ns``,
      ``output_load_pf`` -- this call's own boundary condition, echoed for
      provenance (mirrors ``timing``'s own ``source``/``wire_load``
      convention).
    - ``top``, ``num_cells``, ``num_nets`` -- echoed from the native
      engine's own ``StaResult`` (``native/statime/src/sta.rs``).
    - ``worst_path`` -- the globally worst path (any startpoint/endpoint
      kind -- register-to-register, register-to-port, or port-to-port),
      each with ``startpoint``/``startpoint_kind``/``endpoint``/
      ``endpoint_kind``/``delay_ns`` and a full per-hop ``hops`` breakdown
      (``point``, ``cell``, ``edge``, ``arrival_ns``, ``slew_ns``) -- the
      "limiting path's cells" this stage exists to report.
    - ``worst_reg_to_reg_path`` -- the worst *pure* register-to-register
      path, same shape, or ``None`` for a purely combinational design (this
      engine's own ``mult8`` corpus fixture is one such design).

    Raises :class:`StaError` for a missing extension, an unreadable
    liberty/netlist file, or an analysis failure (e.g. a cell type in the
    netlist that has no liberty entry) -- never a fabricated result.
    """
    native = _load_native()
    try:
        response_json = native.critical_path_json(
            netlist_path,
            liberty_path,
            top,
            float(input_transition_ns),
            float(output_load_pf),
        )
    except ValueError as exc:
        raise StaError(str(exc)) from exc
    result = json.loads(response_json)

    return {
        "source": SOURCE,
        "input_transition_ns": float(input_transition_ns),
        "output_load_pf": float(output_load_pf),
        "top": result["top"],
        "num_cells": result["num_cells"],
        "num_nets": result["num_nets"],
        "worst_path": result["worst_path"],
        "worst_reg_to_reg_path": result.get("worst_reg_to_reg_path"),
    }

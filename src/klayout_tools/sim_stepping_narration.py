"""ngspice stepping narration, and which engine diagnostics block ``on_unreached``.

ngspice narrates its gmin/source-stepping attempts (``Warning: singular
matrix``, ``... stepping failed``) even on a run that goes on to complete, and
the classifier reports that narration at ``error`` severity until
``sim._recovered_from_stepping`` forgives it. That forgiveness waits on every
measurement returning a value, so an ``out of interval`` window miss beside the
narration used to deadlock with the ``on_unreached: "null"`` opt-in, which
waited on the narration being non-error (issue #7906). The shared predicate
here breaks that cycle for the scalar guard (``sim._is_unreached``) and its
step-scoped twin (``sim_step_evidence.is_step_unreached``) alike.
"""

from __future__ import annotations

import re
from typing import Any

#: The two diagnostic codes that are stepping narration rather than a verdict:
#: ``sim._recovered_from_stepping`` downgrades exactly these when the run
#: recovered, and :func:`blocks_not_reached` forgives exactly these.
STEPPING_CODES: tuple[str, str] = ("singular_matrix", "nonconvergence")

#: ngspice's own terminal trailer when an analysis never actually completed
#: (e.g. ``tran simulation(s) aborted``) -- verified against ngspice 46's
#: output for a genuinely unrecoverable singular matrix/nonconvergence (see
#: issue #205). Its presence always means "no trustworthy result", regardless
#: of what measurement values happen to be parseable.
SIMULATION_ABORTED_RE = re.compile(r"simulation\(s\)\s+aborted", re.IGNORECASE)


def blocks_not_reached(diagnostics: list[dict[str, Any]], log_text: str) -> bool:
    """True when an error-severity engine diagnostic means the analysis did not
    complete, so a window miss must not grade ``not_reached``.

    Every error blocks (timeout, ``ngspice_fatal``, ``netlist``,
    ``timestep_too_small``, ...) except stepping narration
    (:data:`STEPPING_CODES`) from a log without the engine's abort trailer
    (:data:`SIMULATION_ABORTED_RE`): that is the exact case
    ``_recovered_from_stepping`` would forgive were the window miss not itself
    the one value-less measurement. The trailer is read from the log because
    its ``analysis_aborted`` diagnostic is only appended after the
    measurements are graded.
    """
    aborted = SIMULATION_ABORTED_RE.search(log_text or "") is not None
    return any(
        d["severity"] == "error" and (aborted or d["code"] not in STEPPING_CODES)
        for d in diagnostics
    )

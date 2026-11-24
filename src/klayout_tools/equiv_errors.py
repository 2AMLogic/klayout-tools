"""The shared :class:`EquivError` of :mod:`klayout_tools.equiv`.

Defined here (not in ``equiv.py``) so the sibling modules ``equiv_replay`` and
``equiv_stage`` can raise it without importing ``equiv`` (#4032);
``klayout_tools.equiv.EquivError`` remains the public path.
"""

from __future__ import annotations


class EquivError(Exception):
    """Raised when an equivalence check cannot even be attempted or
    completed to a verdict: a missing/malformed request, an unresolvable/
    unreadable RTL source, an unsupported engine, a design that contains
    sequential elements (out of this MVP's combinational-only scope), a
    Yosys elaboration/miter-construction error, or a missing ``yosys``
    binary.

    A run that *did* complete to a verdict -- ``"equivalent"``,
    ``"counterexample"``, or ``"inconclusive"`` (solver/process timeout) --
    is never an error; see :func:`run_equiv` and ``docs/cli/equiv.md``'s
    "Exit codes" section.
    """

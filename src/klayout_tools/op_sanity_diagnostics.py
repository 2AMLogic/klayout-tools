"""Diagnostic construction split out of :mod:`klayout_tools.op_sanity`.

Issue #5644: ``op_sanity.py`` is pinned by the ``file-size-baseline.json``
ratchet, and giving the unexpanded-instance warning a model-library-aware
wording would have grown it past its ceiling -- so the construction moved
here instead (a pure move; only the wording itself is new behaviour).
"""

from __future__ import annotations

_UNEXPANDED_HEAD = (
    "could not expand subcircuit instance {name}: its definition was not "
    "found in the netlist or its `.include`d files and it is not a "
    "recognised MOS model"
)


def unexpanded_instance_diagnostics(
    unexpanded: list[str], models_lib: str | None
) -> list[dict[str, str]]:
    """One ``unsupported_hierarchy`` warning per unexpanded ``X`` instance.

    The hierarchy walk reads only the netlist body and its ``.include``
    closure -- never the model library resolved from ``models.lib``, which
    is where an open PDK ships its non-MOS device primitives (a resistor
    as a ``.subckt`` wrapper, say). When a ``models_lib`` was resolved the
    warning says the definition may live there instead of implying the
    netlist alone was searched, so a design whose operating point is *set*
    by an unlinted passive does not read as silently "clean". Without a
    resolved ``models_lib`` the original wording is kept verbatim.
    """
    if models_lib:
        tail = (
            "; if it is a PDK device primitive its definition may live in "
            "the model library (models.lib), which this lint's hierarchy "
            "walk does not read, so any devices inside it were not linted."
        )
    else:
        tail = ", so any devices inside it were not linted."
    return [
        {
            "severity": "warning",
            "code": "unsupported_hierarchy",
            "message": _UNEXPANDED_HEAD.format(name=name) + tail,
        }
        for name in unexpanded
    ]

"""``options.osdi_preload`` resolution for ``klt characterize`` (issues #2513, #4911).

Split out of :mod:`klayout_tools.characterize` to keep that module under its
size ceiling.
"""

from __future__ import annotations

from typing import Any

from . import sim, sim_staging


def resolve_osdi_preload(
    request: dict[str, Any],
    spec: dict[str, Any],
    request_dir: str,
    backend: str | None = None,
) -> tuple[str, ...]:
    """Resolve ``options.osdi_preload``: OSDI shared libraries the simulation
    must ``pre_osdi`` before the circuit is parsed.

    ngspice can only instantiate a Verilog-A compact model through an OSDI
    shared library, and several open PDKs ship their core devices that way --
    IHP's ``sg13g2`` MOSFETs are PSP103 Verilog-A, compiled to
    ``libs.tech/ngspice/osdi/*.osdi`` by
    ``scripts/fetch-sg13g2-sim-toolchain.sh`` (see ``pdks/README.md``).
    Without the preload every device in the cell's netlist fails with
    ``Unable to find definition of model ...``.

    Delegates to ``klt sim``'s own ``options.osdi_preload`` resolver (issue
    #2513): same shape, same resolution rule, same up-front existence check,
    run before any testbench is written so a bad path is named against *this*
    request. Resolved absolute paths are forwarded into the generated
    ``klt sim`` request's ``options.osdi_preload``.

    Issue #4911: on an off-host backend (``remote``/``batch``) an entry the
    executing host resolves itself -- a literal ``$PDK_ROOT/<rel>`` or a path
    under ``models.pdk_root`` (see :func:`sim_staging.runner_resolved_osdi`)
    -- is neither expanded nor existence-checked here, and is forwarded
    **unexpanded**. Declared order is preserved across the mixed list.
    """
    from .characterize import CharacterizeError

    skip: dict[int, str] = {}
    if (backend or request.get("backend")) in sim._OFFHOST_BACKENDS:
        skip = sim_staging.runner_resolved_osdi(
            {"models": request.get("models"), "options": spec}, request_dir
        )
    try:
        resolved = iter(
            sim._resolve_osdi_preload(
                spec,
                request_dir,
                skip_indices=frozenset(skip),
                pdk_root=sim._osdi_pdk_root_pin(request),
            )
        )
    except sim.SimError as exc:
        raise CharacterizeError(str(exc)) from exc
    if not skip:
        return tuple(resolved)
    return tuple(
        entry if index in skip else next(resolved)
        for index, entry in enumerate(spec["osdi_preload"])
    )

"""``models.pdk_root`` pin handling for ``options.osdi_preload`` (issue #5227).

Shared by ``sim``, ``op_sanity`` and ``characterize_osdi`` so every local OSDI
preload resolution applies the pin the same way the batch runner does.
"""

from __future__ import annotations

import os
from typing import Any

from ._paths import _resolve_relative
from .sim_errors import SimError
from .sim_staging import _PDK_ROOT_LITERAL_PREFIXES


def osdi_pdk_root_pin(request: dict[str, Any]) -> str | None:
    """``models.pdk_root`` (the ``--pdk-root`` override included) when set."""
    pin = (request.get("models") or {}).get("pdk_root")
    return pin if isinstance(pin, str) and pin else None


def expand_pdk_root_literal(entry: str, pdk_root: str | None) -> str:
    """Replace a leading literal ``$PDK_ROOT/`` / ``${PDK_ROOT}/`` by the pin.

    The pin wins over the process environment. With no pin, or an entry
    without the prefix, ``entry`` is returned unchanged.
    """
    if pdk_root:
        for prefix in _PDK_ROOT_LITERAL_PREFIXES:
            if entry.startswith(prefix):
                return os.path.join(pdk_root, entry[len(prefix) :])
    return entry


def resolve_osdi_preload(
    options: dict[str, Any],
    request_dir: str,
    skip_indices: frozenset[int] = frozenset(),
    pdk_root: str | None = None,
) -> tuple[str, ...]:
    """Resolve ``options.osdi_preload`` (issue #2513) to absolute paths of
    compiled OSDI shared libraries the generated deck must ``pre_osdi``.

    ngspice can only instantiate a Verilog-A compact model through an OSDI
    shared library, loaded with the *control* command ``pre_osdi <file>``
    (``pre_``-prefixed commands run before the circuit is parsed). Several
    open PDKs ship their core devices that way -- IHP's ``sg13g2`` MOSFETs
    are PSP103 Verilog-A, compiled to ``libs.tech/ngspice/osdi/*.osdi`` by
    ``scripts/fetch-sg13g2-sim-toolchain.sh`` (see ``pdks/README.md``) --
    and without the preload every device fails with ``Unable to find
    definition of model ...``. :func:`_write_corner_deck` emits one
    ``pre_osdi`` line per entry, in declared order, at the top of the
    ``.control`` block it already generates, so a caller's netlist body
    never needs a ``.control`` block of its own (docs/cli/sim.md's "Netlist
    convention").

    Same resolution rule as ``models.lib``'s literal-path shape: env vars
    and ``~`` are expanded, a relative path is joined against the request
    file's own directory. Every entry is existence-checked **here**, before
    any corner is dispatched -- the same posture as
    :func:`_resolve_models_lib`'s ``model library not found`` -- rather than
    left to surface as a per-corner ngspice error deep inside the run.

    ``skip_indices`` (issue #3819) are entries the executing host resolves
    itself (see :func:`sim_staging.runner_resolved_osdi`): validated as
    paths but neither expanded nor existence-checked, and omitted from the
    result.

    ``pdk_root`` (issue #5227): the request's ``models.pdk_root`` pin, which
    wins over ``$PDK_ROOT`` in the environment (see ``sim_osdi_pin``).

    Returns ``()`` when the option is absent or an empty list, which leaves
    every generated deck byte-identical to before this issue.
    """
    raw = options.get("osdi_preload")
    if raw is None:
        return ()
    if isinstance(raw, str) or not isinstance(raw, (list, tuple)):
        raise SimError(
            "request.options.osdi_preload must be an array of paths to "
            "compiled `.osdi` shared libraries"
        )
    resolved: list[str] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, str) or not entry.strip():
            raise SimError(
                f"request.options.osdi_preload[{index}] must be a non-empty path"
            )
        if index in skip_indices:
            continue
        path = _resolve_relative(expand_pdk_root_literal(entry, pdk_root), request_dir)
        if not os.path.isfile(path):
            raise SimError(f"osdi_preload file not found: {path}")
        resolved.append(path)
    return tuple(resolved)

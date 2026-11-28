"""``models.pdk_root`` pin handling for ``options.osdi_preload`` (issue #5227).

Shared by ``sim``, ``op_sanity`` and ``characterize_osdi`` so every local OSDI
preload resolution applies the pin the same way the batch runner does.
"""

from __future__ import annotations

import ntpath
import os
import posixpath
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from ._paths import _resolve_relative
from .sim_errors import SimError
from .sim_staging import _PDK_ROOT_LITERAL_PREFIXES

#: ``(normpath, pure path class)`` for each path convention an executing host
#: may use. A backslash separates components only under the Windows convention
#: and is an ordinary filename character under POSIX, so the two count different
#: directory components for a mixed-separator reference.
_PATH_CONVENTIONS = (
    (posixpath.normpath, PurePosixPath),
    (ntpath.normpath, PureWindowsPath),
)


def _leaves_variant(ref: str) -> bool:
    """Whether ``ref`` is anchored or starts with a parent component under
    *either* path convention.

    A portable request is resolved by whichever host executes it, and
    :func:`_preload_ref` hands that host ``os.path.normpath(ref)``. Checking
    only one convention would pass a reference that normalizes to ``.`` under
    one and ``..`` under the other (``models\\device/../..``).
    """
    for normpath, pure_path in _PATH_CONVENTIONS:
        normalized = pure_path(normpath(ref))
        if normalized.anchor or normalized.parts[:1] == ("..",):
            return True
    return False


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


def _preload_ref(entry: Any, field: str, pdk_variant: str | None) -> str:
    if isinstance(entry, str) and entry.strip():
        return entry
    if not isinstance(entry, dict):
        raise SimError(f"{field} must be a non-empty path or a pdk_relative object")
    if entry.keys() != {"pdk_relative"}:
        raise SimError(f"{field} must have exactly one key, 'pdk_relative'")
    ref = entry["pdk_relative"]
    if not isinstance(ref, str) or not ref.strip():
        raise SimError(f"{field}.pdk_relative must be a non-empty relative path")
    # A portable request must stay relative under either host's path rules.
    if _leaves_variant(ref):
        raise SimError(
            f"{field}.pdk_relative must stay within the selected PDK variant"
        )
    if not isinstance(pdk_variant, str) or not pdk_variant.strip():
        raise SimError(f"{field}.pdk_relative requires models.pdk")
    return os.path.normpath(ref)


def rebase_osdi_preload(request: dict[str, Any], request_dir: str) -> None:
    """Anchor file inputs in a relocated request copy to their original directory.

    Literal PDK-root paths must reach the host that loads them. Leave malformed
    entries in place so the normal simulator validation can diagnose them.
    """
    options = request.get("options")
    if not isinstance(options, dict):
        return
    entries = options.get("osdi_preload")
    if not isinstance(entries, list):
        return
    for index, entry in enumerate(entries):
        if (
            isinstance(entry, str)
            and entry.strip()
            and not entry.startswith(_PDK_ROOT_LITERAL_PREFIXES)
        ):
            entries[index] = _resolve_relative(entry, request_dir)


def resolve_osdi_preload(
    options: dict[str, Any],
    request_dir: str,
    skip_indices: frozenset[int] = frozenset(),
    pdk_root: str | None = None,
    pdk_variant: str | None = None,
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

    Strings expand environment variables and ``~`` relative to the request
    directory. A ``pdk_relative`` reference uses the selected PDK variant
    directory and the same lookup as a corner section library. Each entry
    is existence-checked before a corner is dispatched, as with
    :func:`resolve_models_lib`'s ``model library not found`` -- rather than
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
        field = f"request.options.osdi_preload[{index}]"
        ref = _preload_ref(entry, field, pdk_variant)
        if index in skip_indices:
            continue
        if isinstance(entry, dict):
            from .sim_model_paths import resolve_lib_ref

            try:
                path = resolve_lib_ref(
                    ref, {"pdk": pdk_variant, "pdk_root": pdk_root}, request_dir
                )
            except SimError as exc:
                raise SimError(f"{field}: {exc}") from exc
        else:
            path = _resolve_relative(
                expand_pdk_root_literal(ref, pdk_root), request_dir
            )
        if not os.path.isfile(path):
            raise SimError(f"{field}: osdi_preload file not found: {path}")
        resolved.append(path)
    return tuple(resolved)


def resolve_request_osdi(
    request: dict[str, Any],
    request_dir: str,
    skip_indices: frozenset[int] = frozenset(),
) -> tuple[str, ...]:
    """Resolve the declared preload list using this request's PDK selection."""
    return resolve_osdi_preload(
        request.get("options") or {},
        request_dir,
        skip_indices,
        osdi_pdk_root_pin(request),
        (request.get("models") or {}).get("pdk"),
    )

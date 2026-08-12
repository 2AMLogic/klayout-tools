"""Quasi-static RLC extraction via a Method-of-Moments / PEEC solver
(``klt mom``, issues #718 and #797, Phases 0/1 of the MoM epic #701).

Pure library: :func:`run_mom` returns plain Python data (a JSON-serialisable
``dict``) and never prints -- serialisation and human-readable formatting
live in ``cli/mom_cmd.py``, matching every other ``klt`` verb (see
``layers.py``'s docstring on the same convention).

Geometry extraction from the GDSII/OASIS layout uses ``klayout.db`` in batch
mode only (headless, CI-safe). The numerically hot part -- surface
discretisation, potential-coefficient matrix fill, and the linear solve for
the capacitance matrix -- runs in the ``klt_mom_native`` Rust extension
(``native/mom/``, this repo's first Rust component); this module's job is
only to turn a GDS layer + stackup spec into the JSON request that extension
expects, and to turn its JSON response into this command's documented
payload. See ``docs/cli/mom.md`` for the full spec-file schema, the native
extension's JSON contract, and build instructions.
"""

from __future__ import annotations

import json
import os
from typing import Any

from ._layout import load_layout, select_top_cells

#: Mirrors ``native/mom/src/contract.rs``'s ``DEFAULT_PANEL_SIZE_UM`` -- kept
#: in sync manually (no shared source of truth across the Rust/Python
#: boundary for a single scalar constant); a spec omitting ``panel_size_um``
#: gets this value both here (for the reported field) and in the native
#: solver (which defaults the same way if the request omits the field).
DEFAULT_PANEL_SIZE_UM = 0.5

#: Mirrors ``native/mom/src/contract.rs``'s ``DEFAULT_FILAMENT_SUBDIVISIONS``
#: -- kept in sync manually, exactly like :data:`DEFAULT_PANEL_SIZE_UM`.
#: Only consulted when the spec asks for the PEEC (inductance/resistance)
#: path by declaring a conductivity.
DEFAULT_FILAMENT_SUBDIVISIONS = 1

#: Bumped ``1 -> 2`` by issue #797, which added ``inductance_matrix_nh``,
#: ``resistance_ohm`` and ``filament_count`` to the report. Kept in sync
#: manually with ``native/mom/src/contract.rs``'s
#: ``RESPONSE_SCHEMA_VERSION``.
SCHEMA_VERSION = 2


class MomError(Exception):
    """Raised when ``klt mom`` cannot run: a bad layout/spec file, a stackup
    entry that matches no shapes, the native extension not being built, or a
    solver-level failure surfaced from the Rust core (e.g. a singular
    potential-coefficient matrix).

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback -- see ``docs/json-contract.md``.
    """


def _load_native() -> Any:
    try:
        import klt_mom_native
    except ImportError as exc:
        raise MomError(
            "the klt_mom_native extension is not installed -- from a repo "
            "checkout, run `maturin develop --release` inside native/mom/ "
            "(or `pip install ./native/mom`); see "
            "docs/cli/mom.md#building-the-native-extension"
        ) from exc
    return klt_mom_native


def _load_spec(spec_path: str) -> dict[str, Any]:
    if not os.path.exists(spec_path):
        raise MomError(f"spec file not found: {spec_path}")
    if os.path.isdir(spec_path):
        raise MomError(f"not a file: {spec_path}")
    try:
        with open(spec_path) as f:
            spec = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise MomError(f"could not read spec '{spec_path}': {exc}") from exc
    if not isinstance(spec, dict):
        raise MomError(f"spec '{spec_path}' must be a JSON object")
    return spec


def _parse_layer_datatype(raw: str, spec_path: str) -> tuple[int, int]:
    parts = raw.split("/")
    malformed = MomError(
        f"spec '{spec_path}': stackup entry 'layer' must be '<layer>/<datatype>' "
        f"with integer layer/datatype (got {raw!r})"
    )
    if len(parts) != 2:
        raise malformed
    try:
        return int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise malformed from exc


def _stackup_conductivity(
    entry: dict[str, Any], name: str, spec_path: str, seen: dict[str, float | None]
) -> None:
    """Record (and cross-check) one stackup entry's ``conductivity_S_per_m``.

    Conductivity is a property of the *conductor*, but the spec file is
    keyed by stackup entry (layer), and several entries may name the same
    conductor. Two entries disagreeing about one conductor's conductivity is
    a spec bug, not something to silently resolve, so it is rejected.
    """
    raw = entry.get("conductivity_S_per_m")
    value: float | None
    if raw is None:
        value = None
    else:
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise MomError(
                f"spec '{spec_path}': stackup entry's 'conductivity_S_per_m' must "
                f"be a number (got {raw!r})"
            ) from exc
        if value <= 0.0:
            raise MomError(
                f"spec '{spec_path}': conductor {name!r} has a non-positive "
                f"'conductivity_S_per_m' ({value})"
            )
    if name in seen and seen[name] != value:
        raise MomError(
            f"spec '{spec_path}': conductor {name!r} is given two different "
            f"'conductivity_S_per_m' values ({seen[name]!r} and {value!r}) -- "
            "conductivity is a per-conductor property, so every stackup entry "
            "naming a conductor must agree"
        )
    seen[name] = value


def _stackup_boxes(
    layout: Any, top_cell: Any, spec: dict[str, Any], spec_path: str
) -> tuple[list[str], dict[str, list[dict[str, float]]], dict[str, float | None]]:
    """Read every stackup entry's GDS shapes into per-conductor boxes (um).

    Multiple stackup entries may name the same ``conductor`` -- their boxes
    are merged into one electrical node (this is how a multi-layer
    conductor, e.g. a coax outer shield's four wall segments, is expressed;
    see ``docs/cli/mom.md``'s "Spec file" section). Returns conductor names
    in first-seen order (stable, so the response's row/column order is
    deterministic), the box lists keyed by name, and the per-conductor
    conductivity (``None`` where the spec did not declare one).
    """
    dbu = layout.dbu
    order: list[str] = []
    boxes: dict[str, list[dict[str, float]]] = {}
    conductivity: dict[str, float | None] = {}

    for entry in spec["stackup"]:
        for key in ("layer", "conductor", "z0_um", "z1_um"):
            if key not in entry:
                raise MomError(
                    f"spec '{spec_path}': stackup entry missing {key!r}: {entry!r}"
                )
        layer_datatype = _parse_layer_datatype(str(entry["layer"]), spec_path)
        name = str(entry["conductor"])
        if name not in boxes:
            boxes[name] = []
            order.append(name)
        _stackup_conductivity(entry, name, spec_path, conductivity)

        layer_index = layout.find_layer(*layer_datatype)
        if layer_index is None:
            # Layer declared in the spec but absent from this particular
            # layout -- not an error by itself (a shared stackup spec may
            # list layers that a given fixture doesn't use); the "every
            # conductor got at least one shape" check below is what catches
            # a genuine mismatch.
            continue

        it = top_cell.begin_shapes_rec(layer_index)
        while not it.at_end():
            box = it.shape().bbox().transformed(it.trans())
            boxes[name].append(
                {
                    "x0_um": box.left * dbu,
                    "y0_um": box.bottom * dbu,
                    "x1_um": box.right * dbu,
                    "y1_um": box.top * dbu,
                    "z0_um": float(entry["z0_um"]),
                    "z1_um": float(entry["z1_um"]),
                }
            )
            it.next()

    empty = [name for name in order if not boxes[name]]
    if empty:
        raise MomError(
            f"spec '{spec_path}': conductor(s) {empty!r} matched no shapes -- "
            "check the stackup's layer/datatype numbers against the layout"
        )
    return order, boxes, conductivity


def run_mom(
    file: str,
    spec_path: str,
    *,
    top: str | None = None,
) -> dict[str, Any]:
    """Run ``klt mom``'s quasi-static extraction end to end.

    ``file`` is a GDSII/OASIS layout; ``spec_path`` is a JSON file with:

    - ``stackup`` (required, non-empty array): entries of
      ``{"layer": "<layer>/<datatype>", "conductor": "<name>",
      "z0_um": <float>, "z1_um": <float>}``, mapping each GDS layer's shapes
      to an electrical conductor's z-extent. Several entries may share a
      ``conductor`` name to merge shapes on different GDS layers into one
      electrical node. An entry may additionally carry
      ``conductivity_S_per_m`` (see below).
    - ``background_permittivity`` (required, float): relative permittivity
      of the uniform dielectric surrounding every conductor -- the MVP
      solves a single homogeneous medium (see docs/cli/mom.md's "Scope and
      limitations").
    - ``panel_size_um`` (optional, float): target discretisation panel edge
      length in micrometers; defaults to :data:`DEFAULT_PANEL_SIZE_UM`.
    - ``filament_subdivisions`` (optional, int): sub-filaments per axis for
      the PEEC solve; defaults to :data:`DEFAULT_FILAMENT_SUBDIVISIONS`.
      Ignored unless the PEEC path is requested.

    **Inductance/resistance (PEEC) is opt-in**: declaring
    ``conductivity_S_per_m`` on any stackup entry switches the solve into
    PEEC mode, in which the partial-inductance matrix (``inductance_matrix_nh``)
    and per-conductor DC resistance (``resistance_ohm``) are returned
    alongside the capacitance matrix. Every conductor must then declare one.
    With no conductivity anywhere, the capacitance-only solve runs exactly as
    before and those fields come back ``None``.

    ``top`` selects the top cell to discretise when the stream has more than
    one (required in that case, matching ``select_top_cells``'s convention
    -- see ``docs/cli/layers.md``'s ``--top``).

    Returns a dict matching the documented ``klt mom`` JSON schema (see
    ``docs/cli/mom.md``), including ``schema_version``.
    """
    native = _load_native()
    spec = _load_spec(spec_path)

    if (
        "stackup" not in spec
        or not isinstance(spec["stackup"], list)
        or not spec["stackup"]
    ):
        raise MomError(f"spec '{spec_path}' must have a non-empty 'stackup' array")
    if "background_permittivity" not in spec:
        raise MomError(f"spec '{spec_path}' must set 'background_permittivity'")

    layout = load_layout(file, MomError)
    top_cells = select_top_cells(layout, top, MomError)
    if len(top_cells) != 1:
        raise MomError(
            f"klt mom needs exactly one top cell to discretise ({len(top_cells)} "
            f"found in '{file}'); pass --top to select one"
        )

    conductor_order, boxes, conductivity = _stackup_boxes(
        layout, top_cells[0], spec, spec_path
    )

    panel_size_um = float(spec.get("panel_size_um", DEFAULT_PANEL_SIZE_UM))
    peec = any(conductivity.get(name) is not None for name in conductor_order)
    filament_subdivisions = _filament_subdivisions(spec, spec_path)

    conductors: list[dict[str, Any]] = []
    for name in conductor_order:
        conductor: dict[str, Any] = {"name": name, "boxes": boxes[name]}
        if conductivity.get(name) is not None:
            conductor["conductivity_S_per_m"] = conductivity[name]
        conductors.append(conductor)

    request: dict[str, Any] = {
        "background_permittivity": float(spec["background_permittivity"]),
        "panel_size_um": panel_size_um,
        "conductors": conductors,
    }
    if peec:
        request["filament_subdivisions"] = filament_subdivisions

    try:
        response_json = native.solve_mom_json(json.dumps(request))
    except ValueError as exc:
        raise MomError(str(exc)) from exc
    response = json.loads(response_json)

    return {
        "schema_version": SCHEMA_VERSION,
        "file": file,
        "spec": spec_path,
        "background_permittivity": request["background_permittivity"],
        "panel_size_um": panel_size_um,
        # Echoed only when it was actually consulted, so a capacitance-only
        # report never implies a PEEC knob was in play.
        "filament_subdivisions": filament_subdivisions if peec else None,
        "conductors": response["conductors"],
        "capacitance_matrix_ff": response["capacitance_matrix_ff"],
        # `null` on a capacitance-only solve (no conductivity in the spec) --
        # the fields are always present so a consumer can key on them
        # unconditionally. See docs/cli/mom.md's "JSON schema".
        "inductance_matrix_nh": response["inductance_matrix_nh"],
        "resistance_ohm": response["resistance_ohm"],
        "panel_count": response["panel_count"],
        "filament_count": response["filament_count"],
        # Non-fatal physicality diagnostics from the solver (empty on a
        # well-resolved solve) -- see docs/cli/mom.md's "Warnings".
        "warnings": response["warnings"],
    }


def _filament_subdivisions(spec: dict[str, Any], spec_path: str) -> int:
    raw = spec.get("filament_subdivisions", DEFAULT_FILAMENT_SUBDIVISIONS)
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise MomError(
            f"spec '{spec_path}': 'filament_subdivisions' must be an integer "
            f"(got {raw!r})"
        ) from exc
    if value < 1:
        raise MomError(
            f"spec '{spec_path}': 'filament_subdivisions' must be at least 1 "
            f"(got {value})"
        )
    return value

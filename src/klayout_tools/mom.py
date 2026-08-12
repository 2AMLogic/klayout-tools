"""Quasi-static capacitance/inductance/resistance extraction via a
Method-of-Moments solver (``klt mom``, issue #718, Phase 0/1 of the MoM epic
#701; PEEC inductance/resistance added by issue #797, Phase 1a).

Pure library: :func:`run_mom` returns plain Python data (a JSON-serialisable
``dict``) and never prints -- serialisation and human-readable formatting
live in ``cli/mom_cmd.py``, matching every other ``klt`` verb (see
``layers.py``'s docstring on the same convention).

Geometry extraction from the GDSII/OASIS layout uses ``klayout.db`` in batch
mode only (headless, CI-safe). The numerically hot parts -- surface
discretisation + potential-coefficient matrix fill for capacitance, and
bar/filament discretisation + PEEC partial-inductance/resistance fill for
inductance -- run in the ``klt_mom_native`` Rust extension (``native/mom/``,
this repo's first Rust component); this module's job is only to turn a GDS
layer + stackup spec into the JSON request that extension expects, and to
turn its JSON response into this command's documented payload. See
``docs/cli/mom.md`` for the full spec-file schema, the native extension's
JSON contract, and build instructions.
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

#: Mirrors ``native/mom/src/contract.rs``'s ``DEFAULT_FILAMENT_SIZE_UM`` --
#: same manual-sync convention as ``DEFAULT_PANEL_SIZE_UM`` above. Only
#: consulted when the spec sets ``compute_inductance: true``.
DEFAULT_FILAMENT_SIZE_UM = 1.0

#: Mirrors ``native/mom/src/contract.rs``'s ``DEFAULT_SEGMENT_SIZE_UM`` --
#: same manual-sync convention as the other two defaults above. Only
#: consulted when the spec sets a non-empty ``frequencies_hz`` (issue #893).
DEFAULT_SEGMENT_SIZE_UM = 5.0

#: Mirrors ``native/mom/src/contract.rs``'s
#: ``DEFAULT_PORT_REFERENCE_IMPEDANCE_OHM`` -- same manual-sync convention
#: as the other defaults above. Only consulted for a ``ports`` entry that
#: omits ``reference_impedance_ohm`` (issue #894, Phase 2b of the MoM epic
#: #701).
DEFAULT_PORT_REFERENCE_IMPEDANCE_OHM = 50.0

#: `1` -- capacitance-only (issue #718/#719).
#: `2` -- adds the PEEC inductance/resistance fields (issue #797), each
#:        present only when the spec sets ``compute_inductance: true`` --
#:        see ``native/mom/src/contract.rs``'s own schema-version note.
#:        Issue #893's full-wave sweep fields (present only when the spec
#:        sets a non-empty ``frequencies_hz``) are added without a further
#:        bump -- purely additive, per ``docs/json-contract.md``'s envelope
#:        policy (see ``native/mom/src/contract.rs``'s
#:        ``RESPONSE_SCHEMA_VERSION`` doc for the full note). Issue #894's
#:        ``ports``/``s_parameters`` fields (present only when the spec sets
#:        exactly two ``ports``) are added under the same "no bump" policy.
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


def _stackup_boxes(
    layout: Any, top_cell: Any, spec: dict[str, Any], spec_path: str
) -> tuple[list[str], dict[str, list[dict[str, float]]], dict[str, float | None]]:
    """Read every stackup entry's GDS shapes into per-conductor boxes (um).

    Multiple stackup entries may name the same ``conductor`` -- their boxes
    are merged into one electrical node (this is how a multi-layer
    conductor, e.g. a coax outer shield's four wall segments, is expressed;
    see ``docs/cli/mom.md``'s "Spec file" section). Returns conductor names
    in first-seen order (stable, so the response's row/column order is
    deterministic), the box lists keyed by name, and each conductor's
    ``conductivity_S_per_m`` (``None`` if no stackup entry set it -- only
    required when the spec's ``compute_inductance`` is set, checked by the
    native solver).
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
            conductivity[name] = None

        if "conductivity_S_per_m" in entry:
            value = float(entry["conductivity_S_per_m"])
            existing = conductivity[name]
            if existing is not None and existing != value:
                raise MomError(
                    f"spec '{spec_path}': conductor {name!r} has conflicting "
                    f"conductivity_S_per_m values ({existing!r} vs {value!r}) across "
                    "its stackup entries -- a conductor's conductivity must agree "
                    "everywhere it is named"
                )
            conductivity[name] = value

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


def _parse_ports(spec: dict[str, Any], spec_path: str) -> list[dict[str, float]]:
    """Parse the spec's optional ``ports`` array (issue #894), resolving each
    entry's ``reference_impedance_ohm`` default. Returns an empty list when
    the spec omits ``ports`` (the original contract, byte-for-byte
    unaffected). Raises :class:`MomError` for a malformed entry -- the
    native solver is the source of truth for the *value* checks (position
    within the modeled bar span, port count, ascending order); this only
    validates the JSON shape itself.
    """
    raw = spec.get("ports", [])
    if not isinstance(raw, list):
        raise MomError(f"spec '{spec_path}': 'ports' must be an array")
    ports = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict) or "position_um" not in entry:
            raise MomError(
                f"spec '{spec_path}': ports[{i}] must be an object with a "
                "'position_um' field"
            )
        ports.append(
            {
                "position_um": float(entry["position_um"]),
                "reference_impedance_ohm": float(
                    entry.get(
                        "reference_impedance_ohm",
                        DEFAULT_PORT_REFERENCE_IMPEDANCE_OHM,
                    )
                ),
            }
        )
    return ports


def solve_capacitance_matrix(
    conductors: list[dict[str, Any]],
    background_permittivity: float,
    *,
    panel_size_um: float = DEFAULT_PANEL_SIZE_UM,
) -> dict[str, Any]:
    """Solve the Maxwell capacitance matrix for box geometry already in
    memory -- the programmatic sibling of :func:`run_mom` (issue #798).

    ``run_mom`` is file-oriented: it reads a GDS layout plus a stackup spec
    from disk and returns this command's own documented envelope
    (``schema_version``/``file``/``spec``/...). A caller that already has
    conductor geometry as plain Python data (e.g. `klt extract`'s
    ``--mom-net`` cross-check, which derives idealised box geometry from an
    extracted net's own per-layer regions rather than re-reading a GDS file)
    does not want that round trip -- it wants the native solver's answer
    directly.

    ``conductors`` -- ``[{"name": str, "boxes": [{"x0_um", "y0_um",
    "x1_um", "y1_um", "z0_um", "z1_um"}, ...]}, ...]``, exactly
    ``solve_mom_json``'s own ``conductors`` request field (see
    ``native/mom/src/contract.rs``'s ``MomRequest``) -- the same shape
    :func:`run_mom` builds internally from a stackup spec's matched GDS
    shapes.

    Returns the native solver's response dict verbatim: ``{"conductors",
    "capacitance_matrix_ff", "panel_count", "warnings"}`` -- deliberately
    *not* wrapped in :func:`run_mom`'s ``schema_version``/``file``/``spec``
    envelope, since those fields describe a file-based invocation this
    function never makes; a programmatic caller builds its own envelope
    around this result.

    Raises :class:`MomError` for a missing/unbuilt extension or a
    solver-level failure (a singular potential-coefficient matrix, or the
    panel-count guard), matching :func:`run_mom`.
    """
    native = _load_native()
    request = {
        "background_permittivity": float(background_permittivity),
        "panel_size_um": float(panel_size_um),
        "conductors": conductors,
    }
    try:
        response_json = native.solve_mom_json(json.dumps(request))
    except ValueError as exc:
        raise MomError(str(exc)) from exc
    return json.loads(response_json)


def run_mom(
    file: str,
    spec_path: str,
    *,
    top: str | None = None,
) -> dict[str, Any]:
    """Run ``klt mom``'s quasi-static capacitance (and, optionally, PEEC
    inductance/resistance) extraction end to end.

    ``file`` is a GDSII/OASIS layout; ``spec_path`` is a JSON file with:

    - ``stackup`` (required, non-empty array): entries of
      ``{"layer": "<layer>/<datatype>", "conductor": "<name>",
      "z0_um": <float>, "z1_um": <float>, "conductivity_S_per_m": <float>}``
      (the last is optional -- required only when ``compute_inductance`` is
      set), mapping each GDS layer's shapes to an electrical conductor's
      z-extent. Several entries may share a ``conductor`` name to merge
      shapes on different GDS layers into one electrical node.
    - ``background_permittivity`` (required, float): relative permittivity
      of the uniform dielectric surrounding every conductor -- the MVP
      solves a single homogeneous medium (see docs/cli/mom.md's "Scope and
      limitations").
    - ``panel_size_um`` (optional, float): target discretisation panel edge
      length in micrometers; defaults to :data:`DEFAULT_PANEL_SIZE_UM`.
    - ``compute_inductance`` (optional, bool, default ``false``): opt into
      the PEEC partial-inductance/DC-resistance solve alongside capacitance.
      See docs/cli/mom.md's "PEEC inductance/resistance" section for the
      MVP's bar-shaped-conductor restrictions.
    - ``filament_size_um`` (optional, float): target PEEC cross-section
      filament edge length in micrometers; defaults to
      :data:`DEFAULT_FILAMENT_SIZE_UM`. Only consulted when
      ``compute_inductance`` is set.
    - ``frequencies_hz`` (optional, array of float, default empty): opt into
      the frequency-domain, full-wave (retarded Green's function)
      partial-impedance sweep alongside capacitance -- issue #893, Phase 2a
      of the Method-of-Moments epic #701. Each entry is a frequency in Hz
      (must be positive and finite). Every conductor must satisfy the same
      bar-shaped-conductor MVP restriction ``compute_inductance`` uses (see
      docs/cli/mom.md's "Full-wave frequency sweep" section), independent of
      whether ``compute_inductance`` is also set.
    - ``segment_size_um`` (optional, float): target axial mesh segment edge
      length in micrometers for the full-wave solve; defaults to
      :data:`DEFAULT_SEGMENT_SIZE_UM`. Only consulted when
      ``frequencies_hz`` is non-empty.
    - ``ports`` (optional, array, default empty): opt into port definition +
      de-embedding -- issue #894, Phase 2b of the Method-of-Moments epic
      #701. Each entry is ``{"position_um": <float>,
      "reference_impedance_ohm": <float>}`` (the latter optional, defaults
      to :data:`DEFAULT_PORT_REFERENCE_IMPEDANCE_OHM`). When non-empty, must
      have **exactly two** entries (the MVP's canonical two-port case), and
      requires a non-empty ``frequencies_hz`` and exactly two conductors
      satisfying the full-wave solve's own bar-shaped-conductor restriction.
      See docs/cli/mom.md's "Port definition and de-embedding" section.

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
    compute_inductance = bool(spec.get("compute_inductance", False))
    filament_size_um = float(spec.get("filament_size_um", DEFAULT_FILAMENT_SIZE_UM))
    frequencies_hz = [float(f) for f in spec.get("frequencies_hz", [])]
    segment_size_um = float(spec.get("segment_size_um", DEFAULT_SEGMENT_SIZE_UM))
    ports = _parse_ports(spec, spec_path)
    request = {
        "background_permittivity": float(spec["background_permittivity"]),
        "panel_size_um": panel_size_um,
        "compute_inductance": compute_inductance,
        "filament_size_um": filament_size_um,
        "frequencies_hz": frequencies_hz,
        "segment_size_um": segment_size_um,
        "ports": ports,
        "conductors": [
            {
                "name": name,
                "boxes": boxes[name],
                "conductivity_s_per_m": conductivity[name],
            }
            for name in conductor_order
        ],
    }

    try:
        response_json = native.solve_mom_json(json.dumps(request))
    except ValueError as exc:
        raise MomError(str(exc)) from exc
    response = json.loads(response_json)

    result = {
        "schema_version": SCHEMA_VERSION,
        "file": file,
        "spec": spec_path,
        "background_permittivity": request["background_permittivity"],
        "panel_size_um": panel_size_um,
        "conductors": response["conductors"],
        "capacitance_matrix_ff": response["capacitance_matrix_ff"],
        "panel_count": response["panel_count"],
        # Non-fatal physicality diagnostics from the solver (empty on a
        # well-resolved solve) -- see docs/cli/mom.md's "Warnings".
        "warnings": response["warnings"],
    }
    if compute_inductance:
        # Only present when requested -- mirrors the native contract's
        # Option<...>/None-omitted convention (see contract.rs's
        # MomResponse doc), so a capacitance-only spec's output shape is
        # completely unchanged from before this feature existed.
        result["filament_size_um"] = filament_size_um
        result["inductance_matrix_nh"] = response["inductance_matrix_nh"]
        result["resistance_ohm"] = response["resistance_ohm"]
        result["filament_count"] = response["filament_count"]
    if frequencies_hz:
        # Only present when requested -- same None-omitted convention as
        # compute_inductance above (issue #893).
        result["segment_size_um"] = segment_size_um
        result["full_wave_segment_count"] = response["full_wave_segment_count"]
        result["full_wave_sweep"] = response["full_wave_sweep"]
    if ports:
        # Only present when requested (issue #894) -- echoes the resolved
        # port config (defaults applied) for provenance; each
        # `full_wave_sweep` entry above already carries its own
        # `s_parameters` from the native response.
        result["ports"] = ports
    return result

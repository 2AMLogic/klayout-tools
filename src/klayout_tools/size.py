"""``klt size``: solve a single device's operating point from a gm/Id target
and a current budget, with ``ngspice`` on the real PDK models as the
evaluator -- never a closed-form surrogate as the final word.

Phase 0 (issue #721) of the analog-sizing epic #705: define the request/
response interface, wire ``ngspice`` in as the in-loop evaluator (reusing
the model-library resolution and subprocess-invocation conventions
``sim.py`` already established, per that module's own docstring and
``docs/cli/sim.md``'s "Model library resolution"/"Engine" sections), and
solve the single-device gm/Id MVP: given a fixed channel length, a target
current, and a target gm/Id, find the channel width that hits it.

Why this is not a thin wrapper around :func:`klayout_tools.sim.run_sim`
------------------------------------------------------------------------
``run_sim`` evaluates a **caller-supplied netlist** against a **caller-
supplied ``.meas`` card list** over a PVT corner matrix -- it is not shaped
for what this module needs: a bespoke, dynamically generated single-device
testbench (diode-connected bias, swept across many candidate widths) whose
"measurement" is the MOSFET's own internal small-signal operating-point
state (``gm``, ``id``, ``vgs``, ``vth``, ``vdsat``), which ngspice exposes
only via ``@<device>[<param>]`` vector access inside a ``.control`` block --
there is no ``.MEASURE OP`` (``sim.py``'s own ``_validate_meas_card`` already
documents why) and no sweep variable for a ``.meas dc``/``.meas tran`` card
to search over here. What *is* reused, rather than reimplemented, is the
model-library resolution (:func:`klayout_tools.sim._resolve_models_lib`,
PDK-aware, identical ``models.pdk``/``models.lib``/``models.pdk_root``
shape) and the ``ngspice -b <deck> -o <log>`` subprocess-invocation/timeout/
engine-version-extraction pattern ``sim.py``'s own ``_run_corner`` uses.

Device convention (sky130-first, per CLAUDE.md)
------------------------------------------------
A request names a PDK subcircuit (e.g. ``sky130_fd_pr__nfet_01v8``), called
as an ``X`` element the way every sky130/gf180 PDK deck this repo already
targets expects (see ``sim.py``'s ``_SUBCKT_FAMILY_KEYWORDS`` comment) --
never a bare SPICE-native ``M`` element bound straight to a ``.model`` card.
The subcircuit must accept ``l``/``w``/``nf``/``mult`` parameters, matching
the ``sky130_fd_pr__*`` convention (confirmed against the installed sky130A
PDK while building this module). ngspice's internal op-point vector for a
device instantiated this way lives at ``@m.<instance>.<inner-name>[<param>]``,
where ``<inner-name>`` is, by the same sky130 convention, ``m`` prefixed
onto the subcircuit's own name (e.g. ``msky130_fd_pr__nfet_01v8``) --
:func:`_default_op_point_element` derives this default; ``device.
op_point_element`` overrides it for a PDK that does not follow the
convention.

Method: gm/Id lookup via a diode-connected bias sweep
------------------------------------------------------
The classical gm/Id sizing methodology (Silveira/Flandre/Jespers) looks up
current density (``Id/W``) for a target gm/Id from a pre-characterized curve
at a *fixed* ``Vds``. This MVP uses the simpler, closely related
diode-connected variant instead (``Vds = Vgs``, gate tied to drain): an ideal
current source fixes ``Id`` exactly, ngspice's own DC operating-point solver
finds the ``Vgs`` (and therefore ``gm``) the device settles at for a given
width -- so the only free variable driving the search is ``W``, and no
feedback/regulation loop is needed to hold an independently chosen ``Vds``
against the swept device. ``gm/Id`` at fixed ``Id`` is monotonically
increasing in ``W`` (larger ``W`` -> lower current density -> weaker
inversion -> higher ``gm/Id``, verified empirically against the installed
sky130A PDK's ``sky130_fd_pr__nfet_01v8``/``__pfet_01v8`` while building this
module), which is what makes a bracket-and-interpolate search well-posed.
Decoupling ``Vds`` from ``Vgs`` (the fully general fixed-``Vds`` lookup-table
method) is a documented known limitation -- see ``docs/cli/size.md``'s
"Known limitations".

Two-invocation-per-request design (performance)
-------------------------------------------------
A real PDK's combined-corner ``ngspice`` model library is large (every
device family in one file) -- parsing it costs the overwhelming majority of
a single ``ngspice -b`` invocation's wall-clock time (tens of seconds,
independent of how many operating points are then evaluated; measured
against the installed sky130A PDK while building this module). Naively
re-invoking ``ngspice`` once per candidate width (a classic bisection) would
pay that parse cost on every iteration. Instead, this module pays it
**once**: the whole coarse, log-spaced width sweep (:data:`DEFAULT_SWEEP_POINTS`
points by default) runs inside a *single* ``ngspice`` invocation, using
``alterparam``/``reset`` (re-elaborate the already-parsed circuit with a new
``.param`` value -- cheap, no re-read from disk) between each ``op`` point.
A second, single-point invocation then *confirms* the interpolated answer
(never trusts the coarse grid or the interpolation alone as the final
word) -- see :func:`run_size`.

Corner *set* input, one sizing corner (issue #729)
----------------------------------------------------
A request declares either the original single ``request.corner`` object
(``process``/``vdd_v``/``temperature_c`` scalars, unchanged) or a corner
*set* via ``request.corners``, reusing ``sim.py``'s own
``corners.process``/``corners.temperature_c`` axis semantics verbatim
(:func:`klayout_tools.sim._expand_corners`) rather than inventing a third
spelling -- ``vdd_v`` stays a single scalar across the whole set, since this
command biases one fixed supply rather than sweeping it. Exactly **one**
declared point, ``corners.sizing`` (defaulting to the first point on each
axis), is the *sizing* corner: the width search (the sweep + confirm
described above) runs only there, because re-solving per corner would
return a different width per corner, which is not a device. The confirmed
width is then *verified* -- a fresh single-point ngspice confirmation, no
further search -- at every other declared corner, reporting each corner's
own operating point, margins, and ``pass``/``fail``/``error`` status
(:func:`_parse_corner_set`, :func:`run_size`). The sizing corner's status
alone sets the response's aggregate ``status`` unless the request opts in
via ``targets.hold_across_corners: true``; an evaluator error at *any*
declared corner always makes the aggregate ``status`` ``"error"``, mirroring
``klt sim``'s own error > fail > pass precedence -- see
``docs/cli/size.md``'s "Corner sets" section for the full response shape.
"""

from __future__ import annotations

import math
import os
import re
import subprocess
import tempfile
from typing import Any

from ._paths import _load_request_json, validate_request_shape
from ._provenance import build_provenance
from .pdk import PdkNotFoundError, find_pdk
from .sim import SimError, _expand_corners, _resolve_models_lib

#: Bumped only on a non-additive (breaking) change to this command's own
#: JSON shape -- see docs/json-contract.md.
SCHEMA_VERSION = 1

#: ``ngspice`` is the only implemented engine, mirroring ``sim.py``.
SUPPORTED_ENGINES = ("ngspice",)

#: A request names which terminal the target current flows through and
#: which node ties to the supply -- see this module's docstring's "Method"
#: section for the diode-connected bias each kind implies.
SUPPORTED_DEVICE_KINDS = ("nmos", "pmos")

#: Points in the coarse, log-spaced width sweep run inside the single
#: sweep invocation (see this module's docstring). Overridable via
#: ``request.options.sweep_points``.
DEFAULT_SWEEP_POINTS = 25

#: Minimum sweep points accepted -- fewer than this and a monotonic bracket
#: search has too little resolution to be meaningful.
MIN_SWEEP_POINTS = 5

#: Per-invocation ngspice wall-clock budget. Generous by default because
#: the sweep invocation pays a real PDK's full model-library parse cost
#: once (tens of seconds) plus one cheap ``op`` per sweep point -- see this
#: module's docstring.
DEFAULT_TIMEOUT_S = 180

#: Default relative tolerance on the confirmed operating point's ``gm/Id``
#: against ``request.target.gm_id`` -- overridable via
#: ``request.tolerance.gm_id_rel``.
DEFAULT_GM_ID_TOLERANCE = 0.03

_MARKER_RE = re.compile(r"^KLT_SIZE_POINT\s+(\d+)\s+(\S+)\s*$")
_OP_VALUE_RE = re.compile(r"^@\S+\[(\w+)\]\s*=\s*([-+0-9.eEgGnN]+)\s*$")
_ENGINE_VERSION_RE = re.compile(r"ngspice-([\w.]+)")
_FATAL_LOG_RE = re.compile(
    r"fatal error|could not find a valid modelname|no simulations run|"
    r"simulation\(s\)\s+aborted",
    re.IGNORECASE,
)

#: Op-point vectors requested from ngspice for every point -- ``vth``/
#: ``vdsat`` are not exposed by every compact model (e.g. a bare SPICE
#: level=1 device omits them, verified while building this module's test
#: fixtures), so parsing tolerates their absence; ``gm``/``id`` are required
#: for the point to be usable at all (see ``run_size``'s ``valid_points``
#: filter).
_OP_PARAMS = ("gm", "id", "vgs", "vth", "vdsat")

#: Response-field name each ``_OP_PARAMS`` entry parses into on a point dict.
_OP_PARAM_KEYS = {
    "gm": "gm_s",
    "id": "id_a",
    "vgs": "vgs_v",
    "vth": "vth_v",
    "vdsat": "vdsat_v",
}


class SizeError(Exception):
    """Raised when a sizing request cannot even be attempted: a missing/
    malformed request file, an unresolvable model library, an unsupported
    engine/device kind, or a request whose device bounds are nonsensical.

    Distinct from a request that ran but could not meet its target (reported
    as ``status: "fail"``) or whose evaluator itself errored/timed out
    (``status: "error"``) -- both of those are documented outcomes, not
    exceptions, mirroring ``sim.py``'s ``SimError`` split.
    """


def load_request(request_path: str) -> dict[str, Any]:
    """Read and minimally validate a ``klt size`` request JSON file.

    Raises :class:`SizeError` if the file is missing/unreadable, not valid
    JSON, or missing a required top-level field (``device``, ``models``,
    ``target``).
    """
    request = _load_request_json(request_path, SizeError)
    return validate_request_shape(
        request,
        "request file",
        error_cls=SizeError,
        required_fields=("device", "models", "target"),
    )


def _require_positive_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SizeError(f"{field} must be a number")
    if value <= 0:
        raise SizeError(f"{field} must be positive")
    return float(value)


def _default_op_point_element(model: str) -> str:
    """The sky130-convention internal instance path suffix for a PDK
    subcircuit's own wrapped MOSFET -- ``m`` prefixed onto the subcircuit
    name (e.g. ``sky130_fd_pr__nfet_01v8`` -> ``msky130_fd_pr__nfet_01v8``),
    verified against the installed sky130A PDK's ``__nfet_01v8``/
    ``__pfet_01v8`` subcircuits while building this module. Override via
    ``device.op_point_element`` for a PDK that does not follow it.
    """
    return f"m{model}"


def _parse_device(device: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(device, dict):
        raise SizeError("request.device must be a JSON object")

    kind = device.get("kind")
    if kind not in SUPPORTED_DEVICE_KINDS:
        raise SizeError(
            f"request.device.kind must be one of {SUPPORTED_DEVICE_KINDS} "
            f"(got {kind!r})"
        )

    model = device.get("model")
    if not isinstance(model, str) or not model:
        raise SizeError("request.device.model is required (subcircuit name)")

    l_um = _require_positive_number(device.get("l_um"), "request.device.l_um")
    w_min_um = _require_positive_number(
        device.get("w_min_um"), "request.device.w_min_um"
    )
    w_max_um = _require_positive_number(
        device.get("w_max_um"), "request.device.w_max_um"
    )
    if w_max_um <= w_min_um:
        raise SizeError("request.device.w_max_um must be greater than w_min_um")

    nf = device.get("nf", 1)
    mult = device.get("mult", 1)
    for value, field in ((nf, "request.device.nf"), (mult, "request.device.mult")):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise SizeError(f"{field} must be a positive number")

    op_point_element = device.get("op_point_element") or _default_op_point_element(
        model
    )
    if not isinstance(op_point_element, str) or not op_point_element:
        raise SizeError("request.device.op_point_element must be a non-empty string")

    return {
        "kind": kind,
        "model": model,
        "l_um": l_um,
        "w_min_um": w_min_um,
        "w_max_um": w_max_um,
        "nf": nf,
        "mult": mult,
        "op_point_element": op_point_element,
    }


def _parse_corner(corner: dict[str, Any]) -> dict[str, Any]:
    corner = corner or {}
    if not isinstance(corner, dict):
        raise SizeError("request.corner must be a JSON object")
    process = corner.get("process", "tt")
    if not isinstance(process, str) or not process:
        raise SizeError("request.corner.process must be a non-empty string")
    vdd = corner.get("vdd_v")
    if vdd is None:
        raise SizeError("request.corner.vdd_v is required")
    vdd = _require_positive_number(vdd, "request.corner.vdd_v")
    temperature_c = corner.get("temperature_c", 27)
    if isinstance(temperature_c, bool) or not isinstance(temperature_c, (int, float)):
        raise SizeError("request.corner.temperature_c must be a number")
    return {"process": process, "vdd_v": vdd, "temperature_c": float(temperature_c)}


def _corner_label(process: str, temperature_c: float) -> str:
    """Deterministic display label for one corner point, e.g. ``"tt/27C"``
    -- mirrors ``klt sim``'s own ``CornerPoint.corner_id`` (this module's
    docstring's "Corner set input" section), minus the supply-voltage
    segment (``vdd_v`` is a single scalar across the whole set here, never
    swept per corner)."""
    temp_label = f"{temperature_c:g}"
    return f"{process}/{temp_label}C"


def _parse_corner_set(request: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """Parse the request's corner declaration into a list of expanded
    corner-point dicts (each shaped like :func:`_parse_corner`'s output,
    plus a ``corner_id`` label and, for a process *bundle* entry, a
    ``process_sections`` list) and the index of the declared sizing corner
    within that list.

    Accepts either the original single-corner ``request.corner`` object
    (unchanged) or the new corner-*set* ``request.corners`` object -- never
    both. See this module's docstring's "Corner set input" section.
    """
    legacy_corner = request.get("corner")
    corner_set = request.get("corners")
    if legacy_corner is not None and corner_set is not None:
        raise SizeError(
            "request must declare either 'corner' (single) or 'corners' "
            "(a set), not both"
        )

    if corner_set is None:
        parsed = _parse_corner(legacy_corner or {})
        parsed["corner_id"] = _corner_label(
            parsed["process"], parsed["temperature_c"]
        )
        return [parsed], 0

    if not isinstance(corner_set, dict):
        raise SizeError("request.corners must be a JSON object")

    vdd = corner_set.get("vdd_v")
    if vdd is None:
        raise SizeError("request.corners.vdd_v is required")
    vdd = _require_positive_number(vdd, "request.corners.vdd_v")

    exclude_spec = corner_set.get("exclude") or []
    if not isinstance(exclude_spec, list):
        raise SizeError("request.corners.exclude must be a list")

    axes_spec = {
        "process": corner_set.get("process"),
        "temperature_c": corner_set.get("temperature_c"),
    }
    try:
        points = _expand_corners(axes_spec, exclude_spec)
    except SimError as exc:
        raise SizeError(str(exc)) from exc
    if not points:
        raise SizeError(
            "request.corners produced no corner points (check 'exclude')"
        )

    parsed_points: list[dict[str, Any]] = []
    for point in points:
        process = point.process if point.process is not None else "tt"
        entry: dict[str, Any] = {
            "process": process,
            "vdd_v": vdd,
            "temperature_c": float(point.temperature_c),
        }
        if point.process_sections is not None:
            entry["process_sections"] = list(point.process_sections)
        entry["corner_id"] = _corner_label(process, entry["temperature_c"])
        parsed_points.append(entry)

    sizing_index = _resolve_sizing_index(parsed_points, corner_set.get("sizing"))
    return parsed_points, sizing_index


def _resolve_sizing_index(points: list[dict[str, Any]], sizing_spec: Any) -> int:
    """Resolve ``corners.sizing`` to an index into ``points``. Defaults to
    the first declared point on each axis (index 0 -- :func:`_expand_corners`
    expands process (outer) x temperature (inner), in declaration order, so
    the first expanded point already *is* "first point on each axis")."""
    if sizing_spec is None:
        return 0
    if not isinstance(sizing_spec, dict):
        raise SizeError("request.corners.sizing must be a JSON object")

    candidates = list(range(len(points)))
    process = sizing_spec.get("process")
    if process is not None:
        candidates = [i for i in candidates if points[i]["process"] == process]
    temperature_c = sizing_spec.get("temperature_c")
    if temperature_c is not None:
        if isinstance(temperature_c, bool) or not isinstance(
            temperature_c, (int, float)
        ):
            raise SizeError("request.corners.sizing.temperature_c must be a number")
        candidates = [
            i
            for i in candidates
            if points[i]["temperature_c"] == float(temperature_c)
        ]
    if not candidates:
        raise SizeError(
            "request.corners.sizing does not match any declared corner point"
        )
    return candidates[0]


def _corner_public(corner: dict[str, Any]) -> dict[str, Any]:
    """The response-facing shape for one corner-point dict: the original
    ``process``/``vdd_v``/``temperature_c`` fields (unchanged, so the
    top-level ``corner`` field stays backward compatible) plus the additive
    ``corner_id`` label and, for a process bundle, ``process_sections``."""
    out = {
        "corner_id": corner["corner_id"],
        "process": corner["process"],
        "vdd_v": corner["vdd_v"],
        "temperature_c": corner["temperature_c"],
    }
    if "process_sections" in corner:
        out["process_sections"] = corner["process_sections"]
    return out


def _corner_set_echo(
    corner_points: list[dict[str, Any]], sizing_index: int, hold_across_corners: bool
) -> dict[str, Any]:
    """The ``corners`` response block's input-echo portion (``declared``/
    ``sizing``/``hold_across_corners``) -- present on every response,
    including an early evaluator-error one, per this module's "no stated
    method is rejected" precedent. ``results`` is filled in by the caller
    once (if) the per-corner verification pass actually runs."""
    return {
        "declared": [_corner_public(c) for c in corner_points],
        "sizing": _corner_public(corner_points[sizing_index]),
        "hold_across_corners": hold_across_corners,
        "results": None,
    }


def _parse_hold_across_corners(request: dict[str, Any]) -> bool:
    """``request.targets.hold_across_corners`` -- opts a non-sizing declared
    corner's ``fail`` into the aggregate ``status`` (see this module's
    docstring's "Corner set input" section). Defaults to ``False``: a
    multi-corner request does not fail by construction just because
    ``gm/Id`` genuinely drifts with process/temperature away from the
    sizing corner."""
    targets_block = request.get("targets")
    if targets_block is None:
        return False
    if not isinstance(targets_block, dict):
        raise SizeError("request.targets must be a JSON object")
    value = targets_block.get("hold_across_corners", False)
    if not isinstance(value, bool):
        raise SizeError("request.targets.hold_across_corners must be a boolean")
    return value


def _parse_target(target: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(target, dict):
        raise SizeError("request.target must be a JSON object")
    id_a = _require_positive_number(target.get("id_a"), "request.target.id_a")
    gm_id = _require_positive_number(target.get("gm_id"), "request.target.gm_id")
    return {"id_a": id_a, "gm_id": gm_id}


def run_size(
    request_path: str,
    *,
    artifacts_dir: str | None = None,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """Solve ``request.device``'s width from ``request.target``'s gm/Id and
    current budget, at exactly one declared *sizing* corner, with ``ngspice``
    scoring every candidate against the real PDK models named by
    ``request.models`` -- then verify the solved width's operating point
    and per-target margins across every corner declared in
    ``request.corner``/``request.corners`` (see this module's docstring's
    "Corner set input" section).

    ``artifacts_dir`` overrides where the generated decks/logs are written
    when ``options.keep_artifacts`` is true; defaults to a ``.klt/size/``
    directory next to the request file, mirroring ``klt sim``'s ``.klt/sim/``
    convention. ``timeout_s`` overrides ``options.timeout_s`` (the ``--
    timeout-s`` CLI flag path), same precedence rule as ``klt sim``'s own
    CLI-overrides-request-field fields.

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/size.md``). Raises :class:`SizeError` for anything that
    prevents the search from starting at all (bad request, unresolvable
    model library, unsupported engine/device kind) -- once the sweep starts,
    the response always carries a ``status`` and a populated ``method``
    (see this module's docstring and the "no stated method is rejected"
    acceptance criterion, issue #721).
    """
    request = load_request(request_path)
    request_dir = os.path.dirname(os.path.abspath(request_path))

    engine = request.get("engine", "ngspice")
    if engine not in SUPPORTED_ENGINES:
        raise SizeError(
            f"unsupported engine '{engine}' (supported: {', '.join(SUPPORTED_ENGINES)})"
        )

    device = _parse_device(request["device"])
    corner_points, sizing_index = _parse_corner_set(request)
    sizing_corner = corner_points[sizing_index]
    target = _parse_target(request["target"])
    hold_across_corners = _parse_hold_across_corners(request)
    corners_echo = _corner_set_echo(corner_points, sizing_index, hold_across_corners)

    models = request.get("models") or {}
    try:
        models_lib = _resolve_models_lib(models, request_dir)
    except SimError as exc:
        raise SizeError(str(exc)) from exc

    provenance_pdk: dict[str, Any] | None = None
    if models.get("pdk") or models.get("pdk_root"):
        try:
            provenance_pdk = find_pdk(
                variant=models.get("pdk"), root=models.get("pdk_root")
            )
        except PdkNotFoundError:
            provenance_pdk = None

    tolerance = request.get("tolerance") or {}
    gm_id_rel_tol = tolerance.get("gm_id_rel", DEFAULT_GM_ID_TOLERANCE)
    gm_id_rel_tol = _require_positive_number(
        gm_id_rel_tol, "request.tolerance.gm_id_rel"
    )

    options = request.get("options") or {}
    sweep_points = options.get("sweep_points", DEFAULT_SWEEP_POINTS)
    if (
        isinstance(sweep_points, bool)
        or not isinstance(sweep_points, int)
        or sweep_points < MIN_SWEEP_POINTS
    ):
        raise SizeError(
            f"request.options.sweep_points must be an integer >= {MIN_SWEEP_POINTS}"
        )

    timeout_s = timeout_s if timeout_s is not None else options.get("timeout_s")
    timeout_s = DEFAULT_TIMEOUT_S if timeout_s is None else timeout_s
    timeout_s = _require_positive_number(timeout_s, "request.options.timeout_s")

    keep_artifacts = bool(options.get("keep_artifacts", False))
    if artifacts_dir is None:
        artifacts_dir = os.path.join(request_dir, ".klt", "size")

    if keep_artifacts:
        work_dir = artifacts_dir
        os.makedirs(work_dir, exist_ok=True)
    else:
        work_dir = tempfile.mkdtemp(prefix="klt-size-")

    w_grid = _log_space(device["w_min_um"], device["w_max_um"], sweep_points)

    sweep_points_result, sweep_engine_version, sweep_log, sweep_diagnostic = _run_sweep(
        device=device,
        corner=sizing_corner,
        target=target,
        models_lib=models_lib,
        w_values=w_grid,
        work_dir=work_dir,
        deck_name="sweep.cir",
        log_name="sweep.log",
        timeout_s=timeout_s,
    )

    valid_points = [
        p
        for p in sweep_points_result
        if p["gm_s"] is not None and p["id_a"] not in (None, 0.0)
    ]
    for p in valid_points:
        p["gm_id"] = abs(p["gm_s"]) / abs(p["id_a"])

    engine_version = sweep_engine_version
    environment = {
        "engine": "ngspice",
        "engine_version": engine_version,
        "models_lib": models_lib,
    }
    provenance = build_provenance(
        deck_name=os.path.basename(models_lib),
        deck_path=models_lib,
        pdk=provenance_pdk,
    )

    if len(valid_points) < 2:
        payload = _error_payload(
            device=device,
            corner=sizing_corner,
            corners=corners_echo,
            target=target,
            environment=environment,
            provenance=provenance,
            reason=(
                "ngspice did not return usable operating-point data for at "
                "least 2 of the swept widths"
                + (f": {sweep_diagnostic}" if sweep_diagnostic else "")
                + (f" (see sweep log: {sweep_log})" if keep_artifacts else "")
            ),
        )
        if not keep_artifacts:
            _cleanup_dir(work_dir)
        return payload

    bracket, feasibility_note = _find_bracket(valid_points, target["gm_id"])

    if bracket is None:
        # Infeasible within [w_min, w_max]: report the boundary point closest
        # to the target as the best achievable result, never silently "pass".
        boundary = min(valid_points, key=lambda p: abs(p["gm_id"] - target["gm_id"]))
        w_star = boundary["w_um"]
    else:
        lo, hi = bracket
        w_star = _log_interp(
            lo["w_um"], lo["gm_id"], hi["w_um"], hi["gm_id"], target["gm_id"]
        )

    confirm_points, confirm_engine_version, confirm_log, confirm_diagnostic = (
        _run_sweep(
            device=device,
            corner=sizing_corner,
            target=target,
            models_lib=models_lib,
            w_values=[w_star],
            work_dir=work_dir,
            deck_name="confirm.cir",
            log_name="confirm.log",
            timeout_s=timeout_s,
        )
    )
    if confirm_engine_version is not None:
        environment["engine_version"] = confirm_engine_version

    confirmed = confirm_points[0] if confirm_points else None
    if confirmed is None or confirmed["gm_s"] is None or not confirmed["id_a"]:
        payload = _error_payload(
            device=device,
            corner=sizing_corner,
            corners=corners_echo,
            target=target,
            environment=environment,
            provenance=provenance,
            reason=(
                "ngspice confirmation run at the interpolated width did not "
                "return usable operating-point data"
                + (f": {confirm_diagnostic}" if confirm_diagnostic else "")
                + (f" (see confirm log: {confirm_log})" if keep_artifacts else "")
            ),
        )
        if not keep_artifacts:
            _cleanup_dir(work_dir)
        return payload

    operating_point, confirmed_gm_id, vov, vov_is_approx = _op_point_from_confirmed(
        confirmed, device
    )
    gm_id_rel_error = (confirmed_gm_id - target["gm_id"]) / target["gm_id"]
    id_rel_error = (abs(confirmed["id_a"]) - target["id_a"]) / target["id_a"]
    inversion_level = operating_point["inversion_level"]

    feasible = bracket is not None
    within_tolerance = abs(gm_id_rel_error) <= gm_id_rel_tol
    sizing_status = "pass" if (feasible and within_tolerance) else "fail"

    rationale_parts = [
        f"gm/Id lookup via a diode-connected bias sweep (Vds=Vgs): {device['kind']} "
        f"'{device['model']}' at L={device['l_um']}um, current fixed at "
        f"{target['id_a']:.6g}A by an ideal bias source, width swept from "
        f"{device['w_min_um']}um to {device['w_max_um']}um "
        f"({len(sweep_points_result)} points) to bracket the target gm/Id="
        f"{target['gm_id']:.6g}, then confirmed by a fresh ngspice operating-"
        f"point run at the interpolated width (never trusting the sweep grid "
        f"or interpolation alone)."
    ]
    if not feasible:
        rationale_parts.append(
            f"Target gm/Id={target['gm_id']:.6g} is not reachable within "
            f"[{device['w_min_um']}, {device['w_max_um']}]um at this "
            f"current -- {feasibility_note}; reporting the closest boundary "
            "point actually achieved instead of extrapolating."
        )
    if vov is not None and not vov_is_approx:
        rationale_parts.append(
            f"Inversion level '{inversion_level}' from Vov=Vgs-Vth="
            f"{vov:.4g}V (Vov<=0: weak; 0<Vov<0.1V: moderate; "
            "Vov>=0.1V: strong -- a standard rule-of-thumb threshold, not a "
            "precise inversion-coefficient computation)."
        )
    elif vov is not None and vov_is_approx:
        rationale_parts.append(
            f"Inversion level '{inversion_level}' from Vov~=Vdsat="
            f"{vov:.4g}V (Vth not exposed by this device model; Vdsat "
            "approximates Vov for a square-law device, same thresholds as "
            "above -- less accurate for a model where the two genuinely "
            "diverge)."
        )
    else:
        rationale_parts.append(
            "Inversion level could not be classified: the device model did "
            "not expose Vth or Vdsat at this operating point."
        )

    method = {
        "name": "gm/Id lookup via diode-connected bias sweep",
        "bias": "diode-connected (gate tied to drain, Vds=Vgs)",
        "rationale": " ".join(rationale_parts),
        "sweep_points": len(sweep_points_result),
        "valid_sweep_points": len(valid_points),
        "bracket_w_um": (
            [bracket[0]["w_um"], bracket[1]["w_um"]] if bracket is not None else None
        ),
        "interpolated_w_um": w_star,
        "feasible": feasible,
    }

    # Verify the solved width across every OTHER declared corner -- a fresh
    # single-point confirmation each, no further search (issue #729). The
    # sizing corner's own entry is already fully computed above.
    corner_results: list[dict[str, Any]] = [None] * len(corner_points)  # type: ignore[list-item]
    corner_results[sizing_index] = {
        "corner_id": sizing_corner["corner_id"],
        "process": sizing_corner["process"],
        "temperature_c": sizing_corner["temperature_c"],
        "is_sizing": True,
        "status": sizing_status,
        "operating_point": operating_point,
        "margins": {
            "gm_id_rel_error": gm_id_rel_error,
            "id_rel_error": id_rel_error,
        },
        "diagnostic": None,
    }
    for index, other_corner in enumerate(corner_points):
        if index == sizing_index:
            continue
        corner_results[index] = _verify_corner(
            device=device,
            corner=other_corner,
            target=target,
            models_lib=models_lib,
            w_star=w_star,
            work_dir=work_dir,
            deck_name=f"verify_{index}.cir",
            log_name=f"verify_{index}.log",
            timeout_s=timeout_s,
            gm_id_rel_tol=gm_id_rel_tol,
        )
    corners_echo["results"] = corner_results

    # error > fail > pass, mirroring `klt sim`'s own aggregate precedence: an
    # evaluator error at *any* declared corner always wins. Otherwise the
    # sizing corner's own status sets the aggregate -- a non-sizing corner's
    # "fail" is reported spread, not aggregate-failing, unless the request
    # opts in via `targets.hold_across_corners`.
    if any(result["status"] == "error" for result in corner_results):
        status = "error"
    elif sizing_status == "fail":
        status = "fail"
    elif hold_across_corners and any(
        not result["is_sizing"] and result["status"] == "fail"
        for result in corner_results
    ):
        status = "fail"
    else:
        status = "pass"

    payload = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "device": device,
        "corner": _corner_public(sizing_corner),
        "corners": corners_echo,
        "target": target,
        "tolerance": {"gm_id_rel": gm_id_rel_tol},
        "operating_point": operating_point,
        "margins": {
            "gm_id_rel_error": gm_id_rel_error,
            "id_rel_error": id_rel_error,
        },
        "method": method,
        "environment": environment,
        "provenance": provenance,
    }

    if keep_artifacts:
        payload["environment"]["artifacts_dir"] = work_dir
    else:
        _cleanup_dir(work_dir)

    return payload


def _op_point_from_confirmed(
    confirmed: dict[str, Any], device: dict[str, Any]
) -> tuple[dict[str, Any], float, float | None, bool]:
    """Build the ``operating_point`` response shape from one confirmed
    sweep-log point -- shared by the sizing corner's own confirmation and
    every other declared corner's verification run (:func:`_verify_corner`).

    Returns ``(operating_point, gm_id, vov, vov_is_approx)`` -- the latter
    two are exposed separately because the sizing corner's rationale text
    (built by its caller) needs them past what ``operating_point`` itself
    carries.
    """
    gm_id = abs(confirmed["gm_s"]) / abs(confirmed["id_a"])
    vgs = confirmed["vgs_v"]
    vth = confirmed["vth_v"]
    vdsat = confirmed["vdsat_v"]
    vov_is_approx = False
    if vgs is not None and vth is not None:
        vov = vgs - vth
    elif vdsat is not None:
        # Fallback for a compact model that does not expose Vth via its
        # internal op-point vectors (e.g. a bare SPICE level=1 device, see
        # this module's test fixtures): Vdsat approximates Vov for a simple
        # square-law model in saturation. Flagged as approximate in the
        # rationale below -- not accurate for a model where Vdsat and Vov
        # genuinely diverge (e.g. velocity-saturated short-channel BSIM).
        vov = vdsat
        vov_is_approx = True
    else:
        vov = None
    operating_point = {
        "w_um": confirmed["w_um"],
        "l_um": device["l_um"],
        "nf": device["nf"],
        "mult": device["mult"],
        "id_a": abs(confirmed["id_a"]),
        "gm_s": abs(confirmed["gm_s"]),
        "gm_id": gm_id,
        "vgs_v": vgs,
        "vth_v": vth,
        "vov_v": vov,
        "vdsat_v": vdsat,
        "inversion_level": _classify_inversion(vov),
    }
    return operating_point, gm_id, vov, vov_is_approx


def _verify_corner(
    *,
    device: dict[str, Any],
    corner: dict[str, Any],
    target: dict[str, Any],
    models_lib: str,
    w_star: float,
    work_dir: str,
    deck_name: str,
    log_name: str,
    timeout_s: float,
    gm_id_rel_tol: float,
) -> dict[str, Any]:
    """Verify the sizing corner's solved width ``w_star`` at one other
    declared ``corner``: a single-point confirmation, no further search
    (issue #729's "verify the solved width... across every declared
    corner"). Never raises -- an evaluator failure at this corner yields a
    ``"error"`` entry, exactly like the sizing corner's own confirmation
    failure does via :func:`_error_payload`.
    """
    points, _engine_version, _log_path, diagnostic = _run_sweep(
        device=device,
        corner=corner,
        target=target,
        models_lib=models_lib,
        w_values=[w_star],
        work_dir=work_dir,
        deck_name=deck_name,
        log_name=log_name,
        timeout_s=timeout_s,
    )
    confirmed = points[0] if points else None
    if confirmed is None or confirmed["gm_s"] is None or not confirmed["id_a"]:
        return {
            "corner_id": corner["corner_id"],
            "process": corner["process"],
            "temperature_c": corner["temperature_c"],
            "is_sizing": False,
            "status": "error",
            "operating_point": None,
            "margins": None,
            "diagnostic": (
                "ngspice verification run at the sizing width did not "
                "return usable operating-point data"
                + (f": {diagnostic}" if diagnostic else "")
            ),
        }

    operating_point, gm_id, _vov, _vov_is_approx = _op_point_from_confirmed(
        confirmed, device
    )
    gm_id_rel_error = (gm_id - target["gm_id"]) / target["gm_id"]
    id_rel_error = (abs(confirmed["id_a"]) - target["id_a"]) / target["id_a"]
    status = "pass" if abs(gm_id_rel_error) <= gm_id_rel_tol else "fail"
    return {
        "corner_id": corner["corner_id"],
        "process": corner["process"],
        "temperature_c": corner["temperature_c"],
        "is_sizing": False,
        "status": status,
        "operating_point": operating_point,
        "margins": {
            "gm_id_rel_error": gm_id_rel_error,
            "id_rel_error": id_rel_error,
        },
        "diagnostic": None,
    }


def _error_payload(
    *,
    device: dict[str, Any],
    corner: dict[str, Any],
    corners: dict[str, Any],
    target: dict[str, Any],
    environment: dict[str, Any],
    provenance: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    """Build the ``status: "error"`` response shape -- the evaluator itself
    could not produce a trustworthy result at the sizing corner, so no other
    declared corner is verified either (there is no solved width to check).
    ``method`` is still populated (per this module's docstring, "no stated
    method is rejected" applies to every status, not just a successful
    one); ``corners.declared``/``corners.sizing`` are still echoed,
    ``corners.results`` stays ``None`` (never evaluated).
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "error",
        "device": device,
        "corner": _corner_public(corner),
        "corners": corners,
        "target": target,
        "tolerance": None,
        "operating_point": None,
        "margins": None,
        "method": {
            "name": "gm/Id lookup via diode-connected bias sweep",
            "bias": "diode-connected (gate tied to drain, Vds=Vgs)",
            "rationale": f"Evaluator error: {reason}",
            "sweep_points": None,
            "valid_sweep_points": None,
            "bracket_w_um": None,
            "interpolated_w_um": None,
            "feasible": None,
        },
        "environment": environment,
        "provenance": provenance,
    }


def _classify_inversion(vov: float | None) -> str:
    if vov is None:
        return "unknown"
    if vov <= 0:
        return "weak"
    if vov < 0.1:
        return "moderate"
    return "strong"


def _log_space(lo: float, hi: float, n: int) -> list[float]:
    """``n`` log-spaced points from ``lo`` to ``hi`` inclusive."""
    if n == 1:
        return [lo]
    log_lo, log_hi = math.log(lo), math.log(hi)
    step = (log_hi - log_lo) / (n - 1)
    return [math.exp(log_lo + i * step) for i in range(n)]


def _log_interp(
    w_lo: float, g_lo: float, w_hi: float, g_hi: float, target: float
) -> float:
    """Interpolate the width at which ``gm/Id`` crosses ``target``, linear in
    ``ln(w)`` between the two bracketing sweep points."""
    if g_hi == g_lo:
        return w_lo
    frac = (target - g_lo) / (g_hi - g_lo)
    log_w = math.log(w_lo) + frac * (math.log(w_hi) - math.log(w_lo))
    return math.exp(log_w)


def _find_bracket(
    points: list[dict[str, Any]], target_gm_id: float
) -> tuple[tuple[dict[str, Any], dict[str, Any]] | None, str]:
    """Find the adjacent pair of (width-sorted) points whose ``gm_id``
    brackets ``target_gm_id``. ``gm/Id`` is monotonically increasing in
    width at fixed current (see this module's docstring) -- returns
    ``(None, note)`` when the target falls outside the swept range instead
    of extrapolating.
    """
    ordered = sorted(points, key=lambda p: p["w_um"])
    if target_gm_id < ordered[0]["gm_id"]:
        return None, (
            f"target is below the lowest achievable gm/Id in the swept range "
            f"({ordered[0]['gm_id']:.4g} at w_min={ordered[0]['w_um']:.4g}um) -- "
            "gm/Id only increases with width at fixed current, so no width "
            "in bounds reaches a lower value; lower w_min_um or accept a "
            "larger gm/Id target"
        )
    if target_gm_id > ordered[-1]["gm_id"]:
        return None, (
            f"target exceeds the highest achievable gm/Id in the swept range "
            f"({ordered[-1]['gm_id']:.4g} at w_max={ordered[-1]['w_um']:.4g}um) "
            "-- gm/Id only increases with width at fixed current, so no "
            "width in bounds reaches a higher value; raise w_max_um or "
            "accept a smaller gm/Id target"
        )
    for lo, hi in zip(ordered, ordered[1:], strict=False):
        if lo["gm_id"] <= target_gm_id <= hi["gm_id"]:
            return (lo, hi), ""
    # Non-monotonic sweep data (a real but non-ideal model can wobble at the
    # edges of validity) -- fall back to "not bracketed" rather than
    # picking an arbitrary pair.
    return None, "sweep data was not monotonic across the requested range"


def _write_sweep_deck(
    *,
    deck_path: str,
    device: dict[str, Any],
    corner: dict[str, Any],
    target: dict[str, Any],
    models_lib: str,
    w_values: list[float],
) -> None:
    """Generate a single ngspice deck: one ``.lib`` parse, then one
    ``op``-analysis point per ``w_values`` entry, using ``alterparam``/
    ``reset`` between points (cheap re-elaboration, no re-parse of the model
    library -- see this module's docstring's "Two-invocation-per-request
    design").
    """
    kind = device["kind"]
    model = device["model"]
    op = device["op_point_element"]
    id_a = target["id_a"]
    vdd = corner["vdd_v"]
    l_um = device["l_um"]
    nf = device["nf"]
    mult = device["mult"]

    lines = ["* klt size -- generated device sweep deck, do not edit"]
    lines.append(f".param w_sweep={w_values[0]!r}")
    process_sections = corner.get("process_sections")
    if process_sections:
        # Multi-section corner bundle (e.g. gf180mcu's per-device-family
        # `.lib` cards) -- one line per declared section, in order, matching
        # `sim.py`'s own `_write_corner_deck` handling of the same shape.
        for section in process_sections:
            lines.append(f".lib {models_lib} {section}")
    else:
        lines.append(f".lib {models_lib} {corner['process']}")
    lines.append(f"Vdd vdd 0 DC {vdd!r}")
    x_params = f"l={l_um!r} w={{w_sweep}} nf={nf!r} mult={mult!r}"
    if kind == "nmos":
        lines.append(f"Idc vdd drain DC {id_a!r}")
        lines.append(f"X1 drain drain 0 0 {model} {x_params}")
    else:
        lines.append(f"Idc drain 0 DC {id_a!r}")
        lines.append(f"X1 drain drain vdd vdd {model} {x_params}")
    # A top-level dot-card, like `sim.py`'s own corner deck -- `.temp` is not
    # a `.control`-block command. `reset` (used between sweep points below)
    # re-elaborates the circuit from this same parsed source, so the
    # temperature carries through every point without repeating the card.
    lines.append(f".temp {corner['temperature_c']!r}")

    lines.append(".control")
    for index, w_value in enumerate(w_values):
        if index > 0:
            lines.append(f"alterparam w_sweep={w_value!r}")
            lines.append("reset")
        lines.append("op")
        lines.append(f"echo KLT_SIZE_POINT {index} {w_value!r}")
        for param in _OP_PARAMS:
            lines.append(f"print @m.x1.{op}[{param}]")
    lines.append("quit")
    lines.append(".endc")
    lines.append(".end")

    with open(deck_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def _run_sweep(
    *,
    device: dict[str, Any],
    corner: dict[str, Any],
    target: dict[str, Any],
    models_lib: str,
    w_values: list[float],
    work_dir: str,
    deck_name: str,
    log_name: str,
    timeout_s: float,
) -> tuple[list[dict[str, Any]], str | None, str, str | None]:
    """Run one ngspice invocation sweeping ``w_values``, returning
    ``(points, engine_version, log_path, diagnostic)``. ``points`` always has
    exactly ``len(w_values)`` entries, in order; a value ngspice did not
    print for a given point is ``None`` in that point's dict rather than the
    point being dropped, so callers can tell "ran but incomplete" from
    "never ran". ``diagnostic`` is the first fatal-error line found in the
    log (see :data:`_FATAL_LOG_RE`), or ``None`` if none was found -- folded
    into the error payload's ``method.rationale`` when the run is otherwise
    unusable. Never raises -- a launch failure or timeout yields ``gm_s:
    None`` for every point and a synthetic ``diagnostic`` instead.
    """
    deck_path = os.path.join(work_dir, deck_name)
    log_path = os.path.join(work_dir, log_name)
    _write_sweep_deck(
        deck_path=deck_path,
        device=device,
        corner=corner,
        target=target,
        models_lib=models_lib,
        w_values=w_values,
    )

    engine_version: str | None = None
    launch_diagnostic: str | None = None
    try:
        completed = subprocess.run(
            ["ngspice", "-b", deck_path, "-o", log_path],
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        match = _ENGINE_VERSION_RE.search(completed.stdout or "")
        engine_version = match.group(1) if match else None
    except subprocess.TimeoutExpired:
        launch_diagnostic = f"ngspice did not complete within {timeout_s}s, killed"
    except FileNotFoundError as exc:
        launch_diagnostic = f"could not launch ngspice: {exc}"

    log_text = ""
    if os.path.isfile(log_path):
        try:
            with open(log_path, encoding="utf-8", errors="replace") as handle:
                log_text = handle.read()
        except OSError:
            log_text = ""

    diagnostic = launch_diagnostic
    if diagnostic is None:
        fatal_match = _FATAL_LOG_RE.search(log_text)
        if fatal_match:
            diagnostic = _line_containing(log_text, fatal_match.start())

    points = _parse_sweep_log(log_text, len(w_values), w_values)
    return points, engine_version, log_path, diagnostic


def _parse_sweep_log(
    log_text: str, expected_count: int, w_values: list[float]
) -> list[dict[str, Any]]:
    points: list[dict[str, Any] | None] = [None] * expected_count
    current_index: int | None = None
    current_values: dict[str, float] = {}

    def _flush() -> None:
        if current_index is None:
            return
        entry: dict[str, Any] = {"w_um": w_values[current_index]}
        for param in _OP_PARAMS:
            key = _OP_PARAM_KEYS[param]
            entry[key] = current_values.get(param)
        points[current_index] = entry

    for raw_line in log_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        marker = _MARKER_RE.match(line)
        if marker:
            _flush()
            current_index = int(marker.group(1))
            current_values = {}
            continue
        value_match = _OP_VALUE_RE.match(line)
        if value_match and current_index is not None:
            key, raw_value = value_match.groups()
            try:
                current_values[key] = float(raw_value)
            except ValueError:
                continue
    _flush()

    return [
        p
        if p is not None
        else {
            "w_um": w_values[i],
            "gm_s": None,
            "id_a": None,
            "vgs_v": None,
            "vth_v": None,
            "vdsat_v": None,
        }
        for i, p in enumerate(points)
    ]


def _line_containing(text: str, offset: int) -> str:
    start = text.rfind("\n", 0, offset) + 1
    end = text.find("\n", offset)
    if end == -1:
        end = len(text)
    return text[start:end].strip()


def _cleanup_dir(path: str) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)

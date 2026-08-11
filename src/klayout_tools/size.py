"""``klt size``: solve device operating points from gm/Id targets and a
current budget, with ``ngspice`` on the real PDK models as the evaluator --
never a closed-form surrogate as the final word. A request declares either a
single ``device`` (Phase 0, everything up to "Corner *set* input" below) or
a coupled ``topology`` (Phase 1, see "Coupled multi-device sizing" below).

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

Worst-corner margin objective (issue #769)
--------------------------------------------
The above ("sizing_corner", the default) optimizes for exactly one nominal
corner and only *reports* the spread elsewhere -- a device sized that way
can still be the worst-margin choice across the declared PVT set. Setting
``request.corners.objective: "worst_case_margin"`` instead searches for the
single width that maximizes the *worst* per-corner gm/Id margin across
*every* declared corner (:func:`_run_worst_case_margin`).

Every declared corner's ``gm_id(w)`` curve is monotonically increasing in
width at fixed current (this module's own documented, empirically verified
assumption -- see "Method" above), so each corner's relative error
``error_c(w) = (gm_id_c(w) - target) / target`` is monotonically increasing
too, which makes both ``U(w) = max_c error_c(w)`` and ``L(w) = min_c
error_c(w)`` monotonically non-decreasing in ``w`` (the max/min of a finite
family of non-decreasing functions is itself non-decreasing). The width
that minimizes the worst-case absolute error, ``max_c |error_c(w)| =
max(U(w), -L(w))``, is therefore exactly the unique zero crossing of
``h(w) = U(w) + L(w)`` (or a bound of ``[w_min_um, w_max_um]`` when ``h``
never crosses zero within it -- every declared corner sits on the same side
of the target throughout the searchable range). A plain bisection on
``h(ln w)`` finds that crossing to :data:`_WORST_CASE_BISECTION_ITERS`
digits of precision in pure Python against each corner's own already-swept
curve -- no additional ngspice invocation per bisection step.

Unlike the ``"sizing_corner"`` objective (one full-grid sweep, plus one
single-point verification per *other* declared corner: ``N+1`` invocations
total for ``N`` corners), this objective needs every corner's own full-grid
curve to search against, so it costs one full-grid sweep invocation *per
declared corner* plus one single-point confirmation invocation per corner
at the converged width: ``2N`` invocations. The top-level ``corner``/
``operating_point``/``margins`` fields mirror the *worst-margin* corner
among those confirmations under this objective (not the declared
``corners.sizing`` corner, which is still echoed and still flagged
``is_sizing`` in ``corners.results`` for reference) -- see
``docs/cli/size.md``'s "Worst-corner margin objective" section.

A single declared corner makes ``"sizing_corner"`` and ``"worst_case_margin"``
mathematically equivalent, but this module never routes a single-corner
request through the new code path unless the request explicitly opts in via
``corners.objective`` -- the legacy ``request.corner`` shape has no
``objective`` field at all, so a single-corner request is bit-for-bit
unaffected by this feature (issue #769's regression requirement).

Coupled multi-device sizing (issue #768)
------------------------------------------
Everything above sizes **one** device in isolation. A request that declares
``topology`` instead of ``device`` sizes a whole coupled analog cell in one
call: a source-coupled differential pair, its current-mirror load, and the
tail current source that biases both branches -- the "5T OTA" cell
(``kb/entries/five-transistor-ota.json``; this repo's own hand-sized sky130
canary at ``examples/design-pipeline/ota_5t.spice`` is exactly it).
``topology`` and ``device`` are mutually exclusive; :func:`run_size`
enforces "exactly one" and dispatches to :func:`_run_topology_size`.

These three roles cannot be sized correctly by three independent
single-device runs, which is the whole point of this phase: the tail current
sets both branches' bias by KCL, and the mirror's own diode-connected
``Vgs`` *is* the pair's actual ``Vds``, so each role's operating point
depends on the others' widths. Candidate points are therefore evaluated
against the **real coupled netlist** in ngspice (see
:func:`_write_topology_deck` for the exact connectivity), never against a
diode-connected surrogate for the pair or the mirror.

The search is: size the tail independently as the diode-connected bias
replica it physically is (:func:`_solve_tail_device`), then converge the
pair and mirror widths against the coupled deck by a fixed-point iteration
(:func:`_run_topology_size`), then confirm both final widths with a fresh
joint ngspice run. Response shape, request fields, and known limitations
(NMOS input pair only; single ``request.corner`` only) are documented in
``docs/cli/size.md``'s "Coupled multi-device sizing" section.
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

#: ``request.corners.objective`` -- ``"sizing_corner"`` (default, unchanged
#: since issue #729) solves the width at exactly one declared corner and
#: verifies elsewhere; ``"worst_case_margin"`` (issue #769) instead searches
#: for the width that maximizes the worst per-corner gm/Id margin across
#: every declared corner -- see this module's docstring's "Worst-corner
#: margin objective" section.
SUPPORTED_OBJECTIVES = ("sizing_corner", "worst_case_margin")
DEFAULT_OBJECTIVE = "sizing_corner"

#: Bisection iterations for the worst-case-margin width search. Each step
#: only interpolates against already-swept per-corner curves (no additional
#: ngspice invocation), so this is cheap to make tight -- 60 steps gives
#: ~2^-60 relative precision on the search interval, far finer than any
#: physically meaningful width resolution.
_WORST_CASE_BISECTION_ITERS = 60

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

#: ``request.topology.kind`` -- the only coupled multi-device topology this
#: phase implements: a source-coupled differential pair, biased by a single
#: tail current source, loaded by a current mirror (the "5T OTA" -- see
#: ``kb/entries/five-transistor-ota.json`` and this repo's own hand-sized
#: sky130 canary, ``examples/design-pipeline/``). See this module's
#: docstring's "Coupled multi-device sizing" section.
SUPPORTED_TOPOLOGY_KINDS = ("diff_pair_mirror_tail",)

#: ``request.topology.pair.kind`` -- only an NMOS input pair (PMOS mirror
#: load, NMOS tail) is implemented in this phase; a PMOS input pair (mirror
#: image: PMOS pair, NMOS mirror load, PMOS tail sourced from Vdd) is a
#: documented known limitation, not a silent gap -- see
#: ``docs/cli/size.md``'s "Known limitations" for the coupled topology.
SUPPORTED_PAIR_KINDS = ("nmos",)

#: Fixed-point iterations for the coupled pair/mirror width search (see
#: ``_run_topology_size``'s docstring) -- each round costs one full-grid
#: pair sweep plus one full-grid mirror sweep against the *real* coupled
#: circuit (never a diode-connected surrogate for either device), so this
#: stays small; the loop also exits early once both widths stop moving
#: (see ``_TOPOLOGY_CONVERGENCE_REL``).
_TOPOLOGY_FIXED_POINT_ITERS = 3

#: Early-exit threshold for the fixed-point loop above: stop once neither
#: solved width moved by more than this fraction of its own value between
#: rounds.
_TOPOLOGY_CONVERGENCE_REL = 1e-3

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
        # `device`/`topology` are mutually exclusive alternatives (see
        # run_size's dispatch and this module's docstring's "Coupled
        # multi-device sizing" section) -- neither is unconditionally
        # required here so the shared shape check can't reject one path in
        # favour of the other; run_size itself enforces "exactly one".
        required_fields=("models", "target"),
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
        parsed["corner_id"] = _corner_label(parsed["process"], parsed["temperature_c"])
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
        raise SizeError("request.corners produced no corner points (check 'exclude')")

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
            i for i in candidates if points[i]["temperature_c"] == float(temperature_c)
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
    corner_points: list[dict[str, Any]],
    sizing_index: int,
    hold_across_corners: bool,
    objective: str,
) -> dict[str, Any]:
    """The ``corners`` response block's input-echo portion (``declared``/
    ``sizing``/``objective``/``hold_across_corners``) -- present on every
    response, including an early evaluator-error one, per this module's "no
    stated method is rejected" precedent. ``results`` is filled in by the
    caller once (if) the per-corner verification pass actually runs;
    ``worst_case`` is filled in only by the ``"worst_case_margin"``
    objective's own search (see :func:`_run_worst_case_margin`) -- always
    ``None`` under the default ``"sizing_corner"`` objective."""
    return {
        "declared": [_corner_public(c) for c in corner_points],
        "sizing": _corner_public(corner_points[sizing_index]),
        "objective": objective,
        "hold_across_corners": hold_across_corners,
        "results": None,
        "worst_case": None,
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


def _parse_objective(request: dict[str, Any]) -> str:
    """``request.corners.objective`` -- selects the width-search strategy
    across a declared corner set (see this module's docstring's "Worst-
    corner margin objective" section). Only meaningful inside
    ``request.corners`` (a corner *set*) -- the legacy single-``corner``
    request shape has no equivalent field, so it always resolves to the
    default (issue #769's regression requirement: a single declared corner
    behaves exactly as it did before this field existed)."""
    corners_spec = request.get("corners")
    if not isinstance(corners_spec, dict):
        return DEFAULT_OBJECTIVE
    objective = corners_spec.get("objective", DEFAULT_OBJECTIVE)
    if objective not in SUPPORTED_OBJECTIVES:
        raise SizeError(
            f"request.corners.objective must be one of {SUPPORTED_OBJECTIVES} "
            f"(got {objective!r})"
        )
    return objective


def _parse_target(target: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(target, dict):
        raise SizeError("request.target must be a JSON object")
    id_a = _require_positive_number(target.get("id_a"), "request.target.id_a")
    gm_id = _require_positive_number(target.get("gm_id"), "request.target.gm_id")
    return {"id_a": id_a, "gm_id": gm_id}


# --------------------------------------------------------------------------- #
# Coupled multi-device sizing (`request.topology`, issue #768, Phase 1 of the
# analog-sizing epic #705): a differential pair + current-mirror load + tail
# current source, sized as one problem rather than three independent
# single-device sweeps -- see `_run_topology_size`'s docstring for the method.
# --------------------------------------------------------------------------- #


def _parse_topology_role(spec: Any, prefix: str, kind: str) -> dict[str, Any]:
    """Parse one topology role's device declaration (``pair``/``mirror``/
    ``tail``) by delegating to :func:`_parse_device` with ``kind`` injected --
    a role never states its own ``kind`` (it is implied by the topology's
    ``pair.kind`` -- see :func:`_parse_topology`), but every other field
    (``model``/``l_um``/``w_min_um``/``w_max_um``/``nf``/``mult``/
    ``op_point_element``) is identical in shape and validation to a
    single-device request's ``device`` block, so this reuses that validation
    verbatim rather than duplicating it.
    """
    if not isinstance(spec, dict):
        raise SizeError(f"{prefix} must be a JSON object")
    merged = dict(spec)
    merged["kind"] = kind
    try:
        return _parse_device(merged)
    except SizeError as exc:
        raise SizeError(f"{prefix}: {exc}") from exc


def _parse_topology(topology: Any) -> dict[str, Any]:
    if not isinstance(topology, dict):
        raise SizeError("request.topology must be a JSON object")

    kind = topology.get("kind", SUPPORTED_TOPOLOGY_KINDS[0])
    if kind not in SUPPORTED_TOPOLOGY_KINDS:
        raise SizeError(
            f"request.topology.kind must be one of {SUPPORTED_TOPOLOGY_KINDS} "
            f"(got {kind!r})"
        )

    vcm_v = topology.get("vcm_v")
    if vcm_v is None:
        raise SizeError("request.topology.vcm_v is required")
    if isinstance(vcm_v, bool) or not isinstance(vcm_v, (int, float)):
        raise SizeError("request.topology.vcm_v must be a number")
    vcm_v = float(vcm_v)

    pair_spec = topology.get("pair")
    if not isinstance(pair_spec, dict):
        raise SizeError("request.topology.pair must be a JSON object")
    pair_kind = pair_spec.get("kind", "nmos")
    if pair_kind not in SUPPORTED_PAIR_KINDS:
        raise SizeError(
            f"request.topology.pair.kind must be one of {SUPPORTED_PAIR_KINDS} "
            f"(got {pair_kind!r}) -- only an NMOS input pair (PMOS mirror "
            "load, NMOS tail) is implemented in this phase; see docs/cli/"
            "size.md's 'Known limitations'"
        )
    mirror_kind = "pmos" if pair_kind == "nmos" else "nmos"
    tail_kind = pair_kind

    pair = _parse_topology_role(pair_spec, "request.topology.pair", pair_kind)

    mirror_spec = topology.get("mirror")
    mirror = _parse_topology_role(mirror_spec, "request.topology.mirror", mirror_kind)
    ratio = mirror_spec.get("ratio", 1.0) if isinstance(mirror_spec, dict) else 1.0
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or ratio <= 0:
        raise SizeError("request.topology.mirror.ratio must be a positive number")
    mirror["ratio"] = float(ratio)

    tail_spec = topology.get("tail")
    tail = _parse_topology_role(tail_spec, "request.topology.tail", tail_kind)

    return {
        "kind": kind,
        "vcm_v": vcm_v,
        "pair": pair,
        "mirror": mirror,
        "tail": tail,
    }


def _parse_topology_target(target: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(target, dict):
        raise SizeError("request.target must be a JSON object")
    id_tail_a = _require_positive_number(
        target.get("id_tail_a"), "request.target.id_tail_a"
    )
    pair_gm_id = _require_positive_number(
        target.get("pair_gm_id"), "request.target.pair_gm_id"
    )
    mirror_gm_id = _require_positive_number(
        target.get("mirror_gm_id"), "request.target.mirror_gm_id"
    )
    tail_gm_id = _require_positive_number(
        target.get("tail_gm_id"), "request.target.tail_gm_id"
    )
    return {
        "id_tail_a": id_tail_a,
        "pair_gm_id": pair_gm_id,
        "mirror_gm_id": mirror_gm_id,
        "tail_gm_id": tail_gm_id,
    }


def _topology_role_public(role: dict[str, Any]) -> dict[str, Any]:
    """Response-facing echo of one parsed topology role -- same fields
    :func:`_parse_device` already returns for a single-device ``device``
    block, plus ``ratio`` when present (the mirror role only)."""
    out = {
        "kind": role["kind"],
        "model": role["model"],
        "l_um": role["l_um"],
        "w_min_um": role["w_min_um"],
        "w_max_um": role["w_max_um"],
        "nf": role["nf"],
        "mult": role["mult"],
        "op_point_element": role["op_point_element"],
    }
    if "ratio" in role:
        out["ratio"] = role["ratio"]
    return out


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

    has_device = "device" in request
    has_topology = "topology" in request
    if has_device and has_topology:
        raise SizeError(
            "request must declare either 'device' (single-device sizing) or "
            "'topology' (coupled multi-device sizing), not both"
        )
    if has_topology:
        return _run_topology_size(
            request,
            request_dir=request_dir,
            artifacts_dir=artifacts_dir,
            timeout_s=timeout_s,
        )
    if not has_device:
        raise SizeError(
            "request must declare either 'device' (single-device sizing) or "
            "'topology' (coupled multi-device sizing)"
        )

    device = _parse_device(request["device"])
    corner_points, sizing_index = _parse_corner_set(request)
    sizing_corner = corner_points[sizing_index]
    target = _parse_target(request["target"])
    hold_across_corners = _parse_hold_across_corners(request)
    objective = _parse_objective(request)
    corners_echo = _corner_set_echo(
        corner_points, sizing_index, hold_across_corners, objective
    )

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

    provenance = build_provenance(
        deck_name=os.path.basename(models_lib),
        deck_path=models_lib,
        pdk=provenance_pdk,
    )

    if objective == "worst_case_margin":
        payload = _run_worst_case_margin(
            device=device,
            corner_points=corner_points,
            sizing_index=sizing_index,
            target=target,
            models_lib=models_lib,
            gm_id_rel_tol=gm_id_rel_tol,
            w_grid=w_grid,
            work_dir=work_dir,
            timeout_s=timeout_s,
            corners_echo=corners_echo,
            provenance=provenance,
        )
        if keep_artifacts:
            payload["environment"]["artifacts_dir"] = work_dir
        else:
            _cleanup_dir(work_dir)
        return payload

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
        #
        # Magnitude, not the raw value: ngspice reports a PMOS's `vgs`/`vth`
        # op-point vectors as *magnitudes* (both positive, so the primary
        # `vgs - vth` branch above already yields a positive overdrive for
        # either polarity), but reports `vdsat` *signed* (negative for a
        # PMOS). Taking the raw value here would put every PMOS that falls
        # back to Vdsat into `_classify_inversion`'s `<= 0` bucket -- i.e.
        # reported as "weak" no matter how hard it is driven -- purely
        # because of a sign convention that differs between the two vectors
        # ngspice exposes. Latent until #768's coupled topology, whose
        # current-mirror role is always a PMOS when the input pair is NMOS.
        vov = abs(vdsat)
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
    is_sizing: bool = False,
) -> dict[str, Any]:
    """Verify a solved width ``w_star`` at one declared ``corner``: a
    single-point confirmation, no further search (issue #729's "verify the
    solved width... across every declared corner"). Never raises -- an
    evaluator failure at this corner yields an ``"error"`` entry, exactly
    like the sizing corner's own confirmation failure does via
    :func:`_error_payload`.

    ``is_sizing`` defaults to ``False`` (the ``"sizing_corner"`` objective's
    own usage -- called once per *other* declared corner, the sizing
    corner's own entry is built inline by :func:`run_size`). The
    ``"worst_case_margin"`` objective (:func:`_run_worst_case_margin`) calls
    this uniformly for *every* declared corner instead, passing
    ``is_sizing=True`` only for the one matching the request's declared
    ``corners.sizing`` -- a purely informational echo under that objective,
    since the width search itself no longer targets one corner specially.
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
            "is_sizing": is_sizing,
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
        "is_sizing": is_sizing,
        "status": status,
        "operating_point": operating_point,
        "margins": {
            "gm_id_rel_error": gm_id_rel_error,
            "id_rel_error": id_rel_error,
        },
        "diagnostic": None,
    }


def _interp_gm_id_at_width(points: list[dict[str, Any]], w_um: float) -> float:
    """Log-linear interpolate (or clamp at a boundary) the ``gm_id`` value
    at an arbitrary width from one corner's own valid sweep points -- the
    forward counterpart of :func:`_log_interp` (which solves the inverse:
    the width at which a target ``gm_id`` is hit). ``points`` must be
    sorted by ``w_um`` ascending and non-empty.
    """
    if w_um <= points[0]["w_um"]:
        return points[0]["gm_id"]
    if w_um >= points[-1]["w_um"]:
        return points[-1]["gm_id"]
    for lo, hi in zip(points, points[1:], strict=False):
        if lo["w_um"] <= w_um <= hi["w_um"]:
            if hi["w_um"] == lo["w_um"]:
                return lo["gm_id"]
            frac = (math.log(w_um) - math.log(lo["w_um"])) / (
                math.log(hi["w_um"]) - math.log(lo["w_um"])
            )
            return lo["gm_id"] + frac * (hi["gm_id"] - lo["gm_id"])
    return points[-1][
        "gm_id"
    ]  # pragma: no cover -- unreachable given the bounds checks above


def _run_worst_case_margin(
    *,
    device: dict[str, Any],
    corner_points: list[dict[str, Any]],
    sizing_index: int,
    target: dict[str, Any],
    models_lib: str,
    gm_id_rel_tol: float,
    w_grid: list[float],
    work_dir: str,
    timeout_s: float,
    corners_echo: dict[str, Any],
    provenance: dict[str, Any],
) -> dict[str, Any]:
    """``request.corners.objective: "worst_case_margin"`` (issue #769): find
    the single width that maximizes the *worst* per-corner gm/Id margin
    across every declared corner -- see this module's docstring's "Worst-
    corner margin objective" section for the minimax-bisection method and
    its cost relative to the default ``"sizing_corner"`` objective
    (:func:`run_size`, unchanged).
    """
    corner_curves: dict[int, list[dict[str, Any]]] = {}
    corner_diagnostics: dict[int, tuple[str, str | None]] = {}
    engine_version: str | None = None

    for index, corner in enumerate(corner_points):
        points, version, log_path, diagnostic = _run_sweep(
            device=device,
            corner=corner,
            target=target,
            models_lib=models_lib,
            w_values=w_grid,
            work_dir=work_dir,
            deck_name=f"sweep_corner{index}.cir",
            log_name=f"sweep_corner{index}.log",
            timeout_s=timeout_s,
        )
        if version is not None:
            engine_version = version
        valid = [
            p for p in points if p["gm_s"] is not None and p["id_a"] not in (None, 0.0)
        ]
        for p in valid:
            p["gm_id"] = abs(p["gm_s"]) / abs(p["id_a"])
        if len(valid) < 2:
            corner_diagnostics[index] = (log_path, diagnostic)
        else:
            corner_curves[index] = sorted(valid, key=lambda p: p["w_um"])

    environment = {
        "engine": "ngspice",
        "engine_version": engine_version,
        "models_lib": models_lib,
    }

    if not corner_curves:
        first_index = next(iter(corner_diagnostics))
        log_path, diagnostic = corner_diagnostics[first_index]
        reason = (
            "ngspice did not return usable operating-point data for at "
            "least 2 of the swept widths at any declared corner (first "
            f"seen at {corner_points[first_index]['corner_id']}"
            + (f": {diagnostic}" if diagnostic else "")
            + f", see sweep log: {log_path})"
        )
        return _error_payload(
            device=device,
            corner=corner_points[sizing_index],
            corners=corners_echo,
            target=target,
            environment=environment,
            provenance=provenance,
            reason=reason,
        )

    w_min_um, w_max_um = device["w_min_um"], device["w_max_um"]

    def _bounds_error(w_um: float) -> tuple[float, float]:
        errors = [
            (_interp_gm_id_at_width(corner_curves[i], w_um) - target["gm_id"])
            / target["gm_id"]
            for i in corner_curves
        ]
        return max(errors), min(errors)

    u_lo, l_lo = _bounds_error(w_min_um)
    u_hi, l_hi = _bounds_error(w_max_um)
    h_lo, h_hi = u_lo + l_lo, u_hi + l_hi

    if h_lo >= 0:
        # Every declared corner is already at or above target gm/Id even at
        # the narrowest allowed width -- widening only makes it worse, so
        # the least-bad achievable width is the lower bound.
        w_star, feasible = w_min_um, False
    elif h_hi <= 0:
        # The mirror image: every corner is still below target gm/Id even
        # at the widest allowed width.
        w_star, feasible = w_max_um, False
    else:
        lo_ln, hi_ln = math.log(w_min_um), math.log(w_max_um)
        for _ in range(_WORST_CASE_BISECTION_ITERS):
            mid_ln = (lo_ln + hi_ln) / 2
            u_mid, l_mid = _bounds_error(math.exp(mid_ln))
            if u_mid + l_mid < 0:
                lo_ln = mid_ln
            else:
                hi_ln = mid_ln
        w_star, feasible = math.exp((lo_ln + hi_ln) / 2), True

    corner_results: list[dict[str, Any] | None] = [None] * len(corner_points)
    for index, corner in enumerate(corner_points):
        if index in corner_curves:
            corner_results[index] = _verify_corner(
                device=device,
                corner=corner,
                target=target,
                models_lib=models_lib,
                w_star=w_star,
                work_dir=work_dir,
                deck_name=f"confirm_corner{index}.cir",
                log_name=f"confirm_corner{index}.log",
                timeout_s=timeout_s,
                gm_id_rel_tol=gm_id_rel_tol,
                is_sizing=(index == sizing_index),
            )
        else:
            log_path, diagnostic = corner_diagnostics[index]
            corner_results[index] = {
                "corner_id": corner["corner_id"],
                "process": corner["process"],
                "temperature_c": corner["temperature_c"],
                "is_sizing": index == sizing_index,
                "status": "error",
                "operating_point": None,
                "margins": None,
                "diagnostic": (
                    "ngspice did not return usable operating-point data for "
                    "this corner's own width sweep"
                    + (f": {diagnostic}" if diagnostic else "")
                ),
            }
    corners_echo["results"] = corner_results

    usable = [
        i for i, result in enumerate(corner_results) if result["margins"] is not None
    ]
    if not usable:
        return _error_payload(
            device=device,
            corner=corner_points[sizing_index],
            corners=corners_echo,
            target=target,
            environment=environment,
            provenance=provenance,
            reason=(
                "ngspice confirmation at the worst-case-margin width did "
                "not return usable operating-point data at any declared "
                "corner -- see corners.results for the per-corner detail"
            ),
        )

    worst_index = max(
        usable, key=lambda i: abs(corner_results[i]["margins"]["gm_id_rel_error"])
    )
    worst = corner_results[worst_index]
    corners_echo["worst_case"] = worst["corner_id"]

    status = (
        "error"
        if any(result["status"] == "error" for result in corner_results)
        else worst["status"]
    )

    corners_used = [corner_points[i]["corner_id"] for i in sorted(corner_curves)]
    rationale = (
        "Width chosen to maximize the worst per-corner gm/Id margin across "
        f"{len(corners_used)} declared corner(s) ({', '.join(corners_used)}) via "
        "a bisection on ln(W), balancing each corner's own gm/Id(W) curve "
        "(swept once per corner, confirmed by a fresh ngspice operating-point "
        f"run at the converged width): W={w_star:.6g}um, worst margin at "
        f"'{worst['corner_id']}' (gm_id_rel_error="
        f"{worst['margins']['gm_id_rel_error']:+.4g})."
    )
    if not feasible:
        rationale += (
            " Every declared corner sits on the same side of the target "
            "throughout [w_min_um, w_max_um] -- reporting the boundary "
            "width closest to equalizing the margin instead of "
            "extrapolating."
        )

    method = {
        "name": "worst-corner margin via minimax bisection across the declared PVT set",
        "bias": "diode-connected (gate tied to drain, Vds=Vgs)",
        "rationale": rationale,
        "sweep_points": len(w_grid),
        "valid_sweep_points": min(len(corner_curves[i]) for i in corner_curves),
        "bracket_w_um": None,
        "interpolated_w_um": w_star,
        "feasible": feasible,
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "device": device,
        "corner": _corner_public(corner_points[worst_index]),
        "corners": corners_echo,
        "target": target,
        "tolerance": {"gm_id_rel": gm_id_rel_tol},
        "operating_point": worst["operating_point"],
        "margins": worst["margins"],
        "method": method,
        "environment": environment,
        "provenance": provenance,
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


def _bracket_or_boundary(
    points: list[dict[str, Any]], target_gm_id: float
) -> tuple[float, bool, str]:
    """Shared "solve one free width from a monotonic gm/Id(W) curve" step:
    bracket-and-interpolate when the target falls inside the swept range,
    else report the boundary point closest to it (never extrapolate) -- the
    same policy :func:`run_size`'s single-device path applies inline, pulled
    out here so :func:`_run_topology_size` (three width searches: pair,
    mirror, tail) and any future caller share one implementation. Returns
    ``(w_star, feasible, note)``.
    """
    bracket, note = _find_bracket(points, target_gm_id)
    if bracket is None:
        boundary = min(points, key=lambda p: abs(p["gm_id"] - target_gm_id))
        return boundary["w_um"], False, note
    lo, hi = bracket
    w_star = _log_interp(lo["w_um"], lo["gm_id"], hi["w_um"], hi["gm_id"], target_gm_id)
    return w_star, True, ""


def _write_topology_deck(
    *,
    deck_path: str,
    topology: dict[str, Any],
    corner: dict[str, Any],
    id_tail_a: float,
    models_lib: str,
    sweep_role: str,
    sweep_values: list[float],
    fixed_w_pair_um: float,
    fixed_w_mirror_um: float,
) -> None:
    """Generate the coupled diff-pair+mirror+tail deck: the *actual* 5T-OTA
    connectivity (see this module's docstring's "Coupled multi-device
    sizing" section and ``examples/design-pipeline/ota_5t.spice``, this
    repo's own hand-sized sky130 canary using the identical topology) rather
    than a diode-connected surrogate for the pair or the mirror --

    - an ideal DC current source sinks the full tail current budget out of
      the shared source node (``tail``), so both pair branches' currents are
      forced to sum to it exactly (KCL), but -- unlike the single-device
      diode-connected method -- are *not* assumed to split exactly in half:
      the pair devices' drains sit at genuinely different node voltages
      (``n1``, the mirror's diode-connected reference node, vs ``out``, the
      mirror's output node), so channel-length modulation makes the true
      split only approximately even. ngspice's own DC operating-point solver
      resolves the real split; that is the point of a *coupled* sizing.
    - the mirror's diode-connected reference device (``XM3``, ``w_mirror``)
      and its output device (``XM4``, ``w_mirror * mirror.ratio``) sit in
      the real circuit, not in isolation -- ``XM3``'s own Vgs (and hence the
      pair's actual Vds) depends on ``w_mirror``, which is exactly the
      pair<->mirror coupling a set of three independent single-device
      sizings cannot capture.

    Both pair gates are tied to the same ``vcm_v`` bias (the balanced,
    zero-differential-input operating point gm/Id sizing targets) rather
    than driven differentially -- consistent with every other role in this
    topology being characterized at its DC bias point, not a transient/AC
    response.

    ``sweep_role`` selects which width is swept across ``sweep_values``
    inside one ngspice invocation (via ``alterparam``/``reset``, mirroring
    :func:`_write_sweep_deck`'s own single-parameter sweep) and which
    device's own internal op-point vector is probed at each point -- the
    *other* role's width stays fixed at its own ``fixed_w_*_um`` value for
    the whole invocation. :func:`_run_topology_size` alternates which role is
    swept across a small fixed-point iteration (see that function's
    docstring) rather than searching both widths in one pass, since a 2-D
    joint search would need far more ngspice invocations for the same
    resolution.
    """
    pair = topology["pair"]
    mirror = topology["mirror"]
    vdd = corner["vdd_v"]
    vcm = topology["vcm_v"]

    if sweep_role not in ("pair", "mirror"):
        raise ValueError(f"sweep_role must be 'pair' or 'mirror' (got {sweep_role!r})")

    lines = [
        "* klt size -- generated coupled diff-pair+mirror+tail sweep deck, do not edit"
    ]
    if sweep_role == "pair":
        lines.append(f".param w_pair={sweep_values[0]!r}")
        lines.append(f".param w_mirror={fixed_w_mirror_um!r}")
    else:
        lines.append(f".param w_pair={fixed_w_pair_um!r}")
        lines.append(f".param w_mirror={sweep_values[0]!r}")
    lines.append(f".param w_mirror_out={{w_mirror*{mirror['ratio']!r}}}")

    process_sections = corner.get("process_sections")
    if process_sections:
        for section in process_sections:
            lines.append(f".lib {models_lib} {section}")
    else:
        lines.append(f".lib {models_lib} {corner['process']}")

    lines.append(f"Vdd vdd 0 DC {vdd!r}")
    lines.append(f"Vcm cm 0 DC {vcm!r}")
    # Ideal tail current sink: pulls the full tail-current budget out of the
    # shared pair-source node down to ground -- the same "an ideal source
    # fixes the current budget exactly, width is the only free variable"
    # convention _write_sweep_deck's own Idc uses (see this module's
    # docstring's "Method" section), just at the shared node two devices'
    # currents sum into rather than one device's own drain.
    lines.append(f"Itail tail 0 DC {id_tail_a!r}")

    pair_params = (
        f"l={pair['l_um']!r} w={{w_pair}} nf={pair['nf']!r} mult={pair['mult']!r}"
    )
    mirror_ref_params = (
        f"l={mirror['l_um']!r} w={{w_mirror}} nf={mirror['nf']!r} "
        f"mult={mirror['mult']!r}"
    )
    mirror_out_params = (
        f"l={mirror['l_um']!r} w={{w_mirror_out}} nf={mirror['nf']!r} "
        f"mult={mirror['mult']!r}"
    )
    lines.append(f"XM1 n1  cm tail 0   {pair['model']} {pair_params}")
    lines.append(f"XM2 out cm tail 0   {pair['model']} {pair_params}")
    lines.append(f"XM3 n1  n1 vdd vdd  {mirror['model']} {mirror_ref_params}")
    lines.append(f"XM4 out n1 vdd vdd  {mirror['model']} {mirror_out_params}")
    lines.append(f".temp {corner['temperature_c']!r}")

    probe_element = "xm1" if sweep_role == "pair" else "xm3"
    probe_role = pair if sweep_role == "pair" else mirror

    lines.append(".control")
    for index, value in enumerate(sweep_values):
        if index > 0:
            param = "w_pair" if sweep_role == "pair" else "w_mirror"
            lines.append(f"alterparam {param}={value!r}")
            lines.append("reset")
        lines.append("op")
        lines.append(f"echo KLT_SIZE_POINT {index} {value!r}")
        for param in _OP_PARAMS:
            lines.append(
                f"print @m.{probe_element}.{probe_role['op_point_element']}[{param}]"
            )
    lines.append("quit")
    lines.append(".endc")
    lines.append(".end")

    with open(deck_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def _run_topology_sweep(
    *,
    topology: dict[str, Any],
    corner: dict[str, Any],
    id_tail_a: float,
    models_lib: str,
    sweep_role: str,
    sweep_values: list[float],
    fixed_w_pair_um: float,
    fixed_w_mirror_um: float,
    work_dir: str,
    deck_name: str,
    log_name: str,
    timeout_s: float,
) -> tuple[list[dict[str, Any]], str | None, str, str | None]:
    """The coupled-deck counterpart of :func:`_run_sweep`: write, invoke, and
    parse one ``ngspice`` run of :func:`_write_topology_deck`, returning
    ``(points, engine_version, log_path, diagnostic)`` in the same shape
    (each point carries ``w_um`` plus the raw ``_OP_PARAM_KEYS`` fields).
    """
    deck_path = os.path.join(work_dir, deck_name)
    log_path = os.path.join(work_dir, log_name)
    _write_topology_deck(
        deck_path=deck_path,
        topology=topology,
        corner=corner,
        id_tail_a=id_tail_a,
        models_lib=models_lib,
        sweep_role=sweep_role,
        sweep_values=sweep_values,
        fixed_w_pair_um=fixed_w_pair_um,
        fixed_w_mirror_um=fixed_w_mirror_um,
    )
    points, engine_version, diagnostic = _invoke_ngspice(
        deck_path=deck_path,
        log_path=log_path,
        timeout_s=timeout_s,
        w_values=sweep_values,
    )
    return points, engine_version, log_path, diagnostic


def _solve_tail_device(
    *,
    tail: dict[str, Any],
    corner: dict[str, Any],
    id_tail_a: float,
    target_gm_id: float,
    models_lib: str,
    gm_id_rel_tol: float,
    w_grid: list[float],
    work_dir: str,
    timeout_s: float,
) -> dict[str, Any]:
    """Size the tail role independently, via the exact same diode-connected
    single-device method :func:`run_size`'s own ``device`` path uses (reusing
    :func:`_run_sweep` directly), at the full tail current budget
    ``id_tail_a``.

    This is a deliberate simplification, not an oversight: a tail branch is
    conventionally its own replica-bias generator (a diode-connected
    reference mirrored 1:1 onto the actual tail device -- see
    ``examples/design-pipeline/05-sizing.json``'s ``M5b``/``M5``, which this
    topology's ``tail`` role models), characterized and sized on its own
    merits (output impedance / CMRR floor) independent of the differential
    pair it biases, exactly as real analog design flows and this repo's own
    hand-sized canary treat it. The pair+mirror branch (see
    :func:`_run_topology_size`) is where the genuinely novel *coupling* this
    issue targets lives -- the pair's actual Vds and the mirror's actual Vgs
    are mutually dependent in a way three independent single-device sizings
    cannot capture; the tail bias generator is not.

    Returns a dict with ``status`` (``"pass"``/``"fail"``/``"error"``) plus,
    on anything but ``"error"``, ``operating_point``/``margins``/
    ``feasible``/``sweep_points``/``valid_sweep_points``; on ``"error"``,
    a ``diagnostic`` string instead. ``engine_version`` is always present
    (``None`` if ngspice never reported one).
    """
    sweep_points, engine_version, sweep_log, sweep_diag = _run_sweep(
        device=tail,
        corner=corner,
        target={"id_a": id_tail_a},
        models_lib=models_lib,
        w_values=w_grid,
        work_dir=work_dir,
        deck_name="tail_sweep.cir",
        log_name="tail_sweep.log",
        timeout_s=timeout_s,
    )
    valid = [
        p
        for p in sweep_points
        if p["gm_s"] is not None and p["id_a"] not in (None, 0.0)
    ]
    for p in valid:
        p["gm_id"] = abs(p["gm_s"]) / abs(p["id_a"])
    if len(valid) < 2:
        return {
            "status": "error",
            "engine_version": engine_version,
            "diagnostic": (
                "ngspice did not return usable operating-point data for at "
                "least 2 of the swept tail widths"
                + (f": {sweep_diag}" if sweep_diag else "")
            ),
        }

    w_star, feasible, note = _bracket_or_boundary(valid, target_gm_id)

    confirm_points, confirm_version, confirm_log, confirm_diag = _run_sweep(
        device=tail,
        corner=corner,
        target={"id_a": id_tail_a},
        models_lib=models_lib,
        w_values=[w_star],
        work_dir=work_dir,
        deck_name="tail_confirm.cir",
        log_name="tail_confirm.log",
        timeout_s=timeout_s,
    )
    confirmed = confirm_points[0] if confirm_points else None
    if confirmed is None or confirmed["gm_s"] is None or not confirmed["id_a"]:
        return {
            "status": "error",
            "engine_version": confirm_version or engine_version,
            "diagnostic": (
                "ngspice confirmation run for the tail device did not "
                "return usable operating-point data"
                + (f": {confirm_diag}" if confirm_diag else "")
            ),
        }

    operating_point, gm_id, vov, vov_is_approx = _op_point_from_confirmed(
        confirmed, tail
    )
    gm_id_rel_error = (gm_id - target_gm_id) / target_gm_id
    id_rel_error = (abs(confirmed["id_a"]) - id_tail_a) / id_tail_a
    status = "pass" if (feasible and abs(gm_id_rel_error) <= gm_id_rel_tol) else "fail"

    return {
        "status": status,
        "engine_version": confirm_version or engine_version,
        "operating_point": operating_point,
        "vov_v": vov,
        "vov_is_approx": vov_is_approx,
        "margins": {"gm_id_rel_error": gm_id_rel_error, "id_rel_error": id_rel_error},
        "feasible": feasible,
        "bracket_note": note,
        "sweep_points": len(sweep_points),
        "valid_sweep_points": len(valid),
    }


def _role_rationale_clause(
    label: str, op: dict[str, Any], vov: float | None, vov_is_approx: bool
) -> str:
    """One role's *own* gm/Id + inversion-level rationale sentence.

    Issue #768's third acceptance criterion is per-device, not per-cell:
    "every device's result states its gm/Id and inversion-level rationale".
    A coupled solve therefore emits one of these per role rather than a
    single aggregate sentence, so a reader of ``method.rationale`` can tell
    *which* device was sized where in the gm/Id plane and on what evidence
    its inversion level was classified (a true ``Vov = Vgs - Vth``, the
    ``Vov ~= Vdsat`` fallback when the model does not expose ``Vth``, or
    neither).
    """
    if vov is None:
        derivation = " (Vth/Vdsat not exposed by this device model)"
    elif vov_is_approx:
        derivation = (
            f" from Vov~=Vdsat={vov:.4g}V (Vth not exposed by this device model)"
        )
    else:
        derivation = f" from Vov={vov:.4g}V"
    return (
        f"{label}: confirmed gm/Id={op['gm_id']:.6g} S/A at Id="
        f"{op['id_a']:.6g}A, W={op['w_um']:.6g}um / L={op['l_um']:.6g}um; "
        f"inversion level '{op['inversion_level']}'{derivation}."
    )


def _topology_error_payload(
    *,
    topology: dict[str, Any],
    corner: dict[str, Any],
    target: dict[str, Any],
    environment: dict[str, Any],
    provenance: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    """The ``status: "error"`` response shape for a coupled topology
    request -- mirrors :func:`_error_payload`'s "no stated method is
    rejected, even on error" precedent, adapted to the ``topology``/
    ``devices`` response shape (see ``docs/cli/size.md``'s "Coupled
    multi-device sizing" section)."""
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "error",
        "topology": {
            "kind": topology["kind"],
            "vcm_v": topology["vcm_v"],
            "pair": _topology_role_public(topology["pair"]),
            "mirror": _topology_role_public(topology["mirror"]),
            "tail": _topology_role_public(topology["tail"]),
        },
        "corner": _corner_public(corner),
        "target": target,
        "tolerance": None,
        "devices": None,
        "method": {
            "name": (
                "coupled diff-pair+mirror+tail sizing via a real-circuit gm/Id search"
            ),
            "rationale": f"Evaluator error: {reason}",
            "iterations": None,
            "feasible": None,
        },
        "environment": environment,
        "provenance": provenance,
    }


def _run_topology_size(
    request: dict[str, Any],
    *,
    request_dir: str,
    artifacts_dir: str | None,
    timeout_s: float | None,
) -> dict[str, Any]:
    """Solve ``request.topology``'s coupled differential-pair + current-
    mirror + tail devices as one sizing problem (issue #768, Phase 1 of the
    analog-sizing epic #705) -- called from :func:`run_size` when the
    request declares ``topology`` instead of a single ``device``.

    Method
    ------
    The tail role is sized independently (see :func:`_solve_tail_device`'s
    docstring for why that is a legitimate simplification, not a shortcut
    around the coupling this issue targets). The pair and mirror roles are
    solved jointly against the *real* coupled circuit (see
    :func:`_write_topology_deck`'s docstring for the exact netlist) via a
    fixed-point iteration: sweep the pair's width (mirror held at its
    current value) to bracket ``target.pair_gm_id``, then sweep the mirror's
    width (pair held at its just-solved value) to bracket
    ``target.mirror_gm_id`` -- repeating up to
    :data:`_TOPOLOGY_FIXED_POINT_ITERS` times, stopping early once neither
    width moves by more than :data:`_TOPOLOGY_CONVERGENCE_REL` between
    rounds. This converges quickly because the coupling is one-directional
    to first order (the mirror's diode-connected Vgs sets the pair's actual
    Vds, a second-order channel-length-modulation effect on the pair's own
    gm/Id; the pair's width does not materially move the mirror's own
    diode-connected operating point at all, since the mirror's Id is set by
    KCL, not by the pair's width directly).

    A single request declares one corner only (``request.corner``, the
    legacy single-corner shape) -- ``request.corners`` (a corner *set*) is
    not yet supported for topology sizing; see "Known limitations" in
    ``docs/cli/size.md``.

    Returns a dict matching the documented ``topology``-response JSON shape.
    Raises :class:`SizeError` for anything that prevents the search from
    starting at all; once it starts, the response always carries a
    ``status`` and a populated ``method``, mirroring the single-device
    path's own "no stated method is rejected" bar.
    """
    if "corners" in request:
        raise SizeError(
            "request.topology sizing does not yet support request.corners "
            "(a corner set) -- declare a single request.corner instead; "
            "see docs/cli/size.md's 'Known limitations'"
        )

    topology = _parse_topology(request["topology"])
    corner = _parse_corner(request.get("corner") or {})
    corner["corner_id"] = _corner_label(corner["process"], corner["temperature_c"])
    target = _parse_topology_target(request["target"])

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
    gm_id_rel_tol = _require_positive_number(
        tolerance.get("gm_id_rel", DEFAULT_GM_ID_TOLERANCE),
        "request.tolerance.gm_id_rel",
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
        work_dir = tempfile.mkdtemp(prefix="klt-size-topology-")

    provenance = build_provenance(
        deck_name=os.path.basename(models_lib), deck_path=models_lib, pdk=provenance_pdk
    )
    environment: dict[str, Any] = {
        "engine": "ngspice",
        "engine_version": None,
        "models_lib": models_lib,
    }
    topology_echo = {
        "kind": topology["kind"],
        "vcm_v": topology["vcm_v"],
        "pair": _topology_role_public(topology["pair"]),
        "mirror": _topology_role_public(topology["mirror"]),
        "tail": _topology_role_public(topology["tail"]),
    }

    def _finish(payload: dict[str, Any]) -> dict[str, Any]:
        if keep_artifacts:
            payload["environment"]["artifacts_dir"] = work_dir
        else:
            _cleanup_dir(work_dir)
        return payload

    # --- Step 1: tail (independent, diode-connected -- see
    # `_solve_tail_device`'s docstring) ---
    tail_w_grid = _log_space(
        topology["tail"]["w_min_um"], topology["tail"]["w_max_um"], sweep_points
    )
    tail_result = _solve_tail_device(
        tail=topology["tail"],
        corner=corner,
        id_tail_a=target["id_tail_a"],
        target_gm_id=target["tail_gm_id"],
        models_lib=models_lib,
        gm_id_rel_tol=gm_id_rel_tol,
        w_grid=tail_w_grid,
        work_dir=work_dir,
        timeout_s=timeout_s,
    )
    if tail_result.get("engine_version"):
        environment["engine_version"] = tail_result["engine_version"]
    if tail_result["status"] == "error":
        return _finish(
            _topology_error_payload(
                topology=topology,
                corner=corner,
                target=target,
                environment=environment,
                provenance=provenance,
                reason=f"tail device: {tail_result['diagnostic']}",
            )
        )

    # --- Steps 2-3: pair + mirror, jointly, against the real coupled
    # circuit -- fixed-point iteration (see this function's docstring). ---
    pair_w_grid = _log_space(
        topology["pair"]["w_min_um"], topology["pair"]["w_max_um"], sweep_points
    )
    mirror_w_grid = _log_space(
        topology["mirror"]["w_min_um"], topology["mirror"]["w_max_um"], sweep_points
    )
    w_pair = pair_w_grid[len(pair_w_grid) // 2]
    w_mirror = mirror_w_grid[len(mirror_w_grid) // 2]
    pair_feasible = mirror_feasible = False
    pair_note = mirror_note = ""
    rounds_run = 0

    for iteration in range(_TOPOLOGY_FIXED_POINT_ITERS):
        rounds_run += 1
        pair_points, engine_version, _pair_log, pair_diag = _run_topology_sweep(
            topology=topology,
            corner=corner,
            id_tail_a=target["id_tail_a"],
            models_lib=models_lib,
            sweep_role="pair",
            sweep_values=pair_w_grid,
            fixed_w_pair_um=w_pair,
            fixed_w_mirror_um=w_mirror,
            work_dir=work_dir,
            deck_name=f"pair_sweep_{iteration}.cir",
            log_name=f"pair_sweep_{iteration}.log",
            timeout_s=timeout_s,
        )
        if engine_version:
            environment["engine_version"] = engine_version
        valid_pair = [
            p
            for p in pair_points
            if p["gm_s"] is not None and p["id_a"] not in (None, 0.0)
        ]
        for p in valid_pair:
            p["gm_id"] = abs(p["gm_s"]) / abs(p["id_a"])
        if len(valid_pair) < 2:
            return _finish(
                _topology_error_payload(
                    topology=topology,
                    corner=corner,
                    target=target,
                    environment=environment,
                    provenance=provenance,
                    reason=(
                        "pair sweep: ngspice did not return usable "
                        "operating-point data for at least 2 of the swept "
                        "widths" + (f": {pair_diag}" if pair_diag else "")
                    ),
                )
            )
        new_w_pair, pair_feasible, pair_note = _bracket_or_boundary(
            valid_pair, target["pair_gm_id"]
        )

        mirror_points, engine_version, _mirror_log, mirror_diag = _run_topology_sweep(
            topology=topology,
            corner=corner,
            id_tail_a=target["id_tail_a"],
            models_lib=models_lib,
            sweep_role="mirror",
            sweep_values=mirror_w_grid,
            fixed_w_pair_um=new_w_pair,
            fixed_w_mirror_um=w_mirror,
            work_dir=work_dir,
            deck_name=f"mirror_sweep_{iteration}.cir",
            log_name=f"mirror_sweep_{iteration}.log",
            timeout_s=timeout_s,
        )
        if engine_version:
            environment["engine_version"] = engine_version
        valid_mirror = [
            p
            for p in mirror_points
            if p["gm_s"] is not None and p["id_a"] not in (None, 0.0)
        ]
        for p in valid_mirror:
            p["gm_id"] = abs(p["gm_s"]) / abs(p["id_a"])
        if len(valid_mirror) < 2:
            return _finish(
                _topology_error_payload(
                    topology=topology,
                    corner=corner,
                    target=target,
                    environment=environment,
                    provenance=provenance,
                    reason=(
                        "mirror sweep: ngspice did not return usable "
                        "operating-point data for at least 2 of the swept "
                        "widths" + (f": {mirror_diag}" if mirror_diag else "")
                    ),
                )
            )
        new_w_mirror, mirror_feasible, mirror_note = _bracket_or_boundary(
            valid_mirror, target["mirror_gm_id"]
        )

        converged = (
            abs(new_w_pair - w_pair) <= _TOPOLOGY_CONVERGENCE_REL * w_pair
            and abs(new_w_mirror - w_mirror) <= _TOPOLOGY_CONVERGENCE_REL * w_mirror
        )
        w_pair, w_mirror = new_w_pair, new_w_mirror
        if converged:
            break

    # --- Step 4: joint confirmation at the converged (w_pair, w_mirror) --
    # a fresh ngspice run of the real coupled circuit at both final widths
    # simultaneously, never trusting the per-role sweep grids alone. ---
    confirm_pair_points, engine_version, _cp_log, pair_confirm_diag = (
        _run_topology_sweep(
            topology=topology,
            corner=corner,
            id_tail_a=target["id_tail_a"],
            models_lib=models_lib,
            sweep_role="pair",
            sweep_values=[w_pair],
            fixed_w_pair_um=w_pair,
            fixed_w_mirror_um=w_mirror,
            work_dir=work_dir,
            deck_name="confirm_pair.cir",
            log_name="confirm_pair.log",
            timeout_s=timeout_s,
        )
    )
    if engine_version:
        environment["engine_version"] = engine_version
    confirm_mirror_points, engine_version, _cm_log, mirror_confirm_diag = (
        _run_topology_sweep(
            topology=topology,
            corner=corner,
            id_tail_a=target["id_tail_a"],
            models_lib=models_lib,
            sweep_role="mirror",
            sweep_values=[w_mirror],
            fixed_w_pair_um=w_pair,
            fixed_w_mirror_um=w_mirror,
            work_dir=work_dir,
            deck_name="confirm_mirror.cir",
            log_name="confirm_mirror.log",
            timeout_s=timeout_s,
        )
    )
    if engine_version:
        environment["engine_version"] = engine_version

    confirmed_pair = confirm_pair_points[0] if confirm_pair_points else None
    confirmed_mirror = confirm_mirror_points[0] if confirm_mirror_points else None
    if (
        confirmed_pair is None
        or confirmed_pair["gm_s"] is None
        or not confirmed_pair["id_a"]
        or confirmed_mirror is None
        or confirmed_mirror["gm_s"] is None
        or not confirmed_mirror["id_a"]
    ):
        return _finish(
            _topology_error_payload(
                topology=topology,
                corner=corner,
                target=target,
                environment=environment,
                provenance=provenance,
                reason=(
                    "joint confirmation run did not return usable "
                    "operating-point data for the pair and/or mirror device"
                    + (f" (pair: {pair_confirm_diag})" if pair_confirm_diag else "")
                    + (
                        f" (mirror: {mirror_confirm_diag})"
                        if mirror_confirm_diag
                        else ""
                    )
                ),
            )
        )

    pair_op, pair_gm_id, pair_vov, pair_vov_approx = _op_point_from_confirmed(
        confirmed_pair, topology["pair"]
    )
    id_tail_half = target["id_tail_a"] / 2
    pair_margins = {
        "gm_id_rel_error": (pair_gm_id - target["pair_gm_id"]) / target["pair_gm_id"],
        "id_rel_error": (abs(confirmed_pair["id_a"]) - id_tail_half) / id_tail_half,
    }
    pair_status = (
        "pass"
        if (pair_feasible and abs(pair_margins["gm_id_rel_error"]) <= gm_id_rel_tol)
        else "fail"
    )

    mirror_op, mirror_gm_id, mirror_vov, mirror_vov_approx = _op_point_from_confirmed(
        confirmed_mirror, topology["mirror"]
    )
    mirror_op["ratio"] = topology["mirror"]["ratio"]
    mirror_op["w_output_um"] = mirror_op["w_um"] * topology["mirror"]["ratio"]
    mirror_margins = {
        "gm_id_rel_error": (
            (mirror_gm_id - target["mirror_gm_id"]) / target["mirror_gm_id"]
        ),
        "id_rel_error": (abs(confirmed_mirror["id_a"]) - id_tail_half) / id_tail_half,
    }
    mirror_status = (
        "pass"
        if (mirror_feasible and abs(mirror_margins["gm_id_rel_error"]) <= gm_id_rel_tol)
        else "fail"
    )

    tail_role_result = {
        "role": (
            "tail current source (diode-connected bias replica, mirrored "
            "onto the actual tail device)"
        ),
        "status": tail_result["status"],
        "operating_point": tail_result["operating_point"],
        "margins": tail_result["margins"],
    }
    pair_role_result = {
        "role": "input differential pair (M1/M2, matched, balanced at vcm_v)",
        "status": pair_status,
        "operating_point": pair_op,
        "margins": pair_margins,
    }
    mirror_role_result = {
        "role": (
            "current-mirror load (diode-connected reference device; the "
            "mirrored output device is ratio * this width)"
        ),
        "status": mirror_status,
        "operating_point": mirror_op,
        "margins": mirror_margins,
    }
    devices = {
        "pair": pair_role_result,
        "mirror": mirror_role_result,
        "tail": tail_role_result,
    }

    statuses = [tail_result["status"], pair_status, mirror_status]
    if "error" in statuses:
        status = "error"
    elif "fail" in statuses:
        status = "fail"
    else:
        status = "pass"

    rationale_parts = [
        "Coupled diff-pair+mirror+tail sizing: the tail role is sized "
        "independently as a diode-connected bias replica at the full tail "
        f"current budget ({target['id_tail_a']:.6g}A); the input pair and "
        "current-mirror load are then solved jointly against the real "
        "coupled circuit (never a diode-connected surrogate for either) "
        f"via {rounds_run} fixed-point round(s) alternating a pair-width "
        "sweep and a mirror-width sweep, each bracketing its own gm/Id "
        "target, until neither width moved by more than "
        f"{_TOPOLOGY_CONVERGENCE_REL:.2%} between rounds -- then confirmed "
        "with a fresh joint ngspice run at both final widths."
    ]
    if not pair_feasible:
        rationale_parts.append(
            f"Pair target gm/Id={target['pair_gm_id']:.6g} is not reachable "
            f"within [{topology['pair']['w_min_um']}, "
            f"{topology['pair']['w_max_um']}]um -- {pair_note}; reporting "
            "the closest boundary point actually achieved."
        )
    if not mirror_feasible:
        rationale_parts.append(
            f"Mirror target gm/Id={target['mirror_gm_id']:.6g} is not "
            f"reachable within [{topology['mirror']['w_min_um']}, "
            f"{topology['mirror']['w_max_um']}]um -- {mirror_note}; "
            "reporting the closest boundary point actually achieved."
        )
    # Per-device rationale (AC #3): one clause per role, each stating that
    # device's own confirmed gm/Id and how its inversion level was derived.
    rationale_parts.append(
        _role_rationale_clause("Input pair (M1/M2)", pair_op, pair_vov, pair_vov_approx)
    )
    rationale_parts.append(
        _role_rationale_clause(
            "Mirror load reference (M3; output device M4 is ratio x this "
            f"width, ratio={topology['mirror']['ratio']:.6g})",
            mirror_op,
            mirror_vov,
            mirror_vov_approx,
        )
    )
    rationale_parts.append(
        _role_rationale_clause(
            "Tail current source (M5/M5b)",
            tail_result["operating_point"],
            tail_result["vov_v"],
            tail_result["vov_is_approx"],
        )
    )
    # The pair's true per-branch current is a KCL-forced outcome of the real
    # coupled circuit, not an assumption -- surface how far it drifted from
    # the naive "tail current split exactly in half" expectation, since that
    # drift is itself the signal that a set of independent single-device
    # sizings would have missed.
    rationale_parts.append(
        "Actual per-branch current split from the coupled solve: pair="
        f"{abs(confirmed_pair['id_a']):.6g}A, mirror-output-side implied="
        f"{abs(confirmed_mirror['id_a']):.6g}A (nominal half-tail="
        f"{id_tail_half:.6g}A) -- a real circuit's channel-length "
        "modulation keeps this only approximately even, which is exactly "
        "the coupling three independent single-device sizings cannot see."
    )

    method = {
        "name": "coupled diff-pair+mirror+tail sizing via a real-circuit gm/Id search",
        "rationale": " ".join(rationale_parts),
        "iterations": rounds_run,
        "feasible": pair_feasible and mirror_feasible and tail_result["feasible"],
    }

    payload = {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "topology": topology_echo,
        "corner": _corner_public(corner),
        "target": target,
        "tolerance": {"gm_id_rel": gm_id_rel_tol},
        "devices": devices,
        "method": method,
        "environment": environment,
        "provenance": provenance,
    }
    return _finish(payload)


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

    points, engine_version, diagnostic = _invoke_ngspice(
        deck_path=deck_path, log_path=log_path, timeout_s=timeout_s, w_values=w_values
    )
    return points, engine_version, log_path, diagnostic


def _invoke_ngspice(
    *, deck_path: str, log_path: str, timeout_s: float, w_values: list[float]
) -> tuple[list[dict[str, Any]], str | None, str | None]:
    """Invoke ``ngspice -b <deck_path> -o <log_path>``, parse the resulting
    log for ``len(w_values)`` ``KLT_SIZE_POINT``-marked operating points, and
    scan it for a fatal-error diagnostic.

    Shared by :func:`_run_sweep` (the single-device diode-connected deck) and
    :func:`_run_topology_sweep` (the coupled diff-pair+mirror+tail deck) --
    both decks emit the same marker/op-point-vector log shape (see this
    module's docstring), only the deck *generator* differs. Never raises -- a
    launch failure or timeout yields ``gm_s: None`` for every point and a
    synthetic diagnostic instead.
    """
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
    return points, engine_version, diagnostic


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

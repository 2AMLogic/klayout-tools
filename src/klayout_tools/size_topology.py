"""Coupled multi-device topology sizing for ``klt size`` (issue #768,
Phase 1a of epic #705).

Split out of :mod:`klayout_tools.size` (issue #3983) so the topology
solver -- request parsing, deck generation, the joint gm/Id solve, and the
per-instance reporting -- lives in its own module. Pure move: no behavior
or JSON change. :mod:`klayout_tools.size` still owns the request/response
envelope, the single-device solver, and the helpers this module imports;
it imports this module lazily at the few call sites that need it.

See :mod:`klayout_tools.size`'s docstring ("Coupled multi-device topology
sizing") for the method overview and ``docs/cli/size.md`` for the request/
response shape.
"""

from __future__ import annotations

import math
import os
import re
import tempfile
from typing import Any

from ._provenance import build_provenance
from .pdk import PdkNotFoundError, find_pdk
from .sim import SimError, _resolve_models_lib
from .size import (
    _JOINT_WIDTH_STEP_EPS,
    _OP_PARAM_KEYS,
    _OP_PARAMS,
    _OP_VALUE_RE,
    _TOPOLOGY_ROLE_KIND,
    DEFAULT_GM_ID_TOLERANCE,
    DEFAULT_MAX_JOINT_ITERATIONS,
    DEFAULT_SWEEP_POINTS,
    DEFAULT_TIMEOUT_S,
    MIN_JOINT_ITERATIONS,
    MIN_SWEEP_POINTS,
    SCHEMA_VERSION,
    SUPPORTED_TOPOLOGIES,
    TOPOLOGY_ROLES,
    SizeError,
    _cleanup_dir,
    _corner_label,
    _corner_public,
    _find_bracket,
    _interp_gm_id_at_width,
    _invoke_ngspice,
    _log_interp,
    _log_space,
    _op_point_from_confirmed,
    _parse_corner,
    _parse_device,
    _report_path,
    _require_positive_number,
    _run_sweep,
)

# --------------------------------------------------------------------------- #
# Coupled multi-device topology sizing (issue #768, Phase 1a of epic #705)
# --------------------------------------------------------------------------- #
#
# See this module's docstring's "Coupled multi-device topology sizing"
# section for the method overview. Request/response shape is documented in
# ``docs/cli/size.md``'s "Coupled multi-device topology sizing" section.

_INSTANCE_MARKER_RE = re.compile(r"^KLT_SIZE_INSTANCE\s+(\S+)\s*$")
_NODE_VALUE_RE = re.compile(r"^v\((\w+)\)\s*=\s*([-+0-9.eEgGnN]+)\s*$")

#: Every device instance the assembled ``diff_pair_mirror_tail`` deck
#: contains, in report order: ``(key, spice_instance, role, is_control)``.
#: Six instances, three sizing roles -- the input pair and the mirror load
#: are each a matched pair sharing one solved width, and the tail is a
#: matched pair too (the diode-connected bias replica ``tail_ref`` plus the
#: tail device proper). ``is_control`` marks the one instance per role whose
#: *in-circuit* gm/Id drives that role's width update in the joint solve
#: (:func:`_run_joint_solve`) and gates the aggregate status; the other
#: instances of the same role are still fully reported (their own gm/Id,
#: inversion level, and rationale) but do not add a redundant degree of
#: freedom -- they share a width with their partner by construction.
TOPOLOGY_INSTANCES = (
    ("tail_ref", "xtailref", "tail", False),
    ("tail", "xtail", "tail", True),
    ("input_a", "xinputa", "input_pair", True),
    ("input_b", "xinputb", "input_pair", False),
    ("mirror_a", "xmirrora", "mirror", True),
    ("mirror_b", "xmirrorb", "mirror", False),
)

#: Human-readable placement of each instance in the topology, folded into
#: that instance's own rationale string so a response explains *where* the
#: device sits, not just what it measured (issue #768's third acceptance
#: criterion).
_INSTANCE_DESCRIPTION = {
    "tail_ref": (
        "diode-connected tail-bias replica (gate/drain tied to the reference "
        "current), Vds=Vgs"
    ),
    "tail": (
        "tail current source, gate driven by the bias replica, drain at the "
        "input pair's common source node"
    ),
    "input_a": (
        "differential-pair leg driving the mirror's diode leg (drain at the "
        "mirror node)"
    ),
    "input_b": (
        "differential-pair leg driving the output node (drain at the "
        "single-ended output)"
    ),
    "mirror_a": "diode-connected leg of the PMOS current-mirror load, Vds=Vgs",
    "mirror_b": (
        "output leg of the PMOS current-mirror load, drain at the single-ended output"
    ),
}

#: The instance whose measured gm/Id drives each role's width update.
_ROLE_CONTROL_INSTANCE = {
    role: key for key, _instance, role, is_control in TOPOLOGY_INSTANCES if is_control
}

#: Instance key (`design_centering.py`'s own geometry key, e.g. `"input_a"`)
#: -> the role that instance's width is solved under (issue #1326) -- the
#: bridge the design-centering objective mode needs to turn a
#: `request.design_centering.parameter_map` instance name into the
#: `request.devices` key it actually grows.
_INSTANCE_TO_ROLE = {
    key: role for key, _instance, role, _is_control in TOPOLOGY_INSTANCES
}


def _parse_topology_devices(devices: Any, topology: str) -> dict[str, dict[str, Any]]:
    """Parse ``request.devices`` for a coupled topology: one
    :func:`_parse_device`-shaped entry per role in :data:`TOPOLOGY_ROLES`,
    each required to declare the ``device.kind`` :data:`_TOPOLOGY_ROLE_KIND`
    fixes for the shipped topology orientation.
    """
    if not isinstance(devices, dict):
        raise SizeError("request.devices must be a JSON object")

    parsed: dict[str, dict[str, Any]] = {}
    for role in TOPOLOGY_ROLES:
        role_device = devices.get(role)
        if role_device is None:
            raise SizeError(
                f"request.devices.{role} is required for topology '{topology}'"
            )
        parsed_device = _parse_device(role_device)
        expected_kind = _TOPOLOGY_ROLE_KIND[role]
        if parsed_device["kind"] != expected_kind:
            raise SizeError(
                f"request.devices.{role}.kind must be '{expected_kind}' for "
                f"topology '{topology}' (got {parsed_device['kind']!r}) -- this "
                "MVP only supports the NMOS-input-pair/PMOS-mirror-load/"
                "NMOS-tail orientation (see docs/cli/size.md's 'Known "
                "limitations')"
            )
        parsed[role] = parsed_device
    return parsed


def _parse_topology_budget(budget: Any) -> dict[str, Any]:
    """Parse ``request.budget``: the shared tail-current budget and the
    tail mirror ratio, the two coupled degrees of freedom a
    ``diff_pair_mirror_tail`` request declares instead of a per-device
    current (see this module's docstring's "Coupled multi-device topology
    sizing" section).

    ``tail_mirror_ratio`` is the tail device's multiplier relative to the
    diode-connected bias replica: the deck injects ``tail_current_a /
    tail_mirror_ratio`` into the replica and lets the mirror multiply it
    back up, so a ratio of ``4`` costs a quarter of the reference current
    for the same tail current. Defaults to ``1`` (the 1:1 replica this
    repo's own 5T OTA canary uses).
    """
    if not isinstance(budget, dict):
        raise SizeError("request.budget must be a JSON object")
    tail_current_a = _require_positive_number(
        budget.get("tail_current_a"), "request.budget.tail_current_a"
    )
    tail_mirror_ratio = budget.get("tail_mirror_ratio", 1)
    tail_mirror_ratio = _require_positive_number(
        tail_mirror_ratio, "request.budget.tail_mirror_ratio"
    )
    return {
        "tail_current_a": tail_current_a,
        "tail_mirror_ratio": tail_mirror_ratio,
        "reference_current_a": tail_current_a / tail_mirror_ratio,
        "branch_current_a": tail_current_a / 2.0,
    }


def _parse_topology_targets(target: Any, topology: str) -> dict[str, dict[str, Any]]:
    """Parse ``request.target`` for a coupled topology: one
    ``{"gm_id": number}`` entry per role -- unlike single-device mode's
    ``target.id_a``, current is never declared per role here; it is derived
    from ``request.budget`` (see :func:`_parse_topology_budget`), since the
    whole point of coupled sizing is that a role's current is not an
    independent choice.
    """
    if not isinstance(target, dict):
        raise SizeError("request.target must be a JSON object")
    parsed: dict[str, dict[str, Any]] = {}
    for role in TOPOLOGY_ROLES:
        role_target = target.get(role)
        if not isinstance(role_target, dict):
            raise SizeError(
                f"request.target.{role} is required for topology '{topology}'"
            )
        gm_id = _require_positive_number(
            role_target.get("gm_id"), f"request.target.{role}.gm_id"
        )
        parsed[role] = {"gm_id": gm_id}
    return parsed


def _parse_topology_bias(bias: Any) -> dict[str, Any]:
    """Parse the optional ``request.bias`` block controlling the assembled
    topology's input common-mode voltage (defaults to ``Vdd/2`` -- a typical
    mid-supply operating point for a differential pair's gates)."""
    if bias is None:
        return {"vcm_v": None}
    if not isinstance(bias, dict):
        raise SizeError("request.bias must be a JSON object")
    vcm_v = bias.get("vcm_v")
    if vcm_v is not None:
        if isinstance(vcm_v, bool) or not isinstance(vcm_v, (int, float)):
            raise SizeError("request.bias.vcm_v must be a number")
        vcm_v = float(vcm_v)
    return {"vcm_v": vcm_v}


def _parse_max_joint_iterations(options: dict[str, Any]) -> int:
    value = options.get("max_joint_iterations", DEFAULT_MAX_JOINT_ITERATIONS)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < MIN_JOINT_ITERATIONS
    ):
        raise SizeError(
            "request.options.max_joint_iterations must be an integer >= "
            f"{MIN_JOINT_ITERATIONS}"
        )
    return value


def _invert_curve(
    points: list[dict[str, Any]], target_gm_id: float
) -> tuple[float, bool, str]:
    """Solve one role's already-swept ``gm_id(W)`` curve for the width at
    which it hits ``target_gm_id``, returning ``(w_um, feasible, note)``.

    ``gm/Id`` is monotonically increasing in width at fixed current (this
    module's own documented, empirically verified assumption -- see the
    "Method" docstring section), so the inversion is a bracket search
    (:func:`_find_bracket`) plus a log-linear interpolation
    (:func:`_log_interp`) -- exactly what single-device mode does. A target
    outside the swept range is *not* extrapolated: the closest boundary
    point actually achieved is returned with ``feasible=False``.
    """
    ordered = sorted(points, key=lambda p: p["w_um"])
    bracket, note = _find_bracket(ordered, target_gm_id)
    if bracket is None:
        boundary = min(ordered, key=lambda p: abs(p["gm_id"] - target_gm_id))
        return boundary["w_um"], False, note
    lo, hi = bracket
    return (
        _log_interp(lo["w_um"], lo["gm_id"], hi["w_um"], hi["gm_id"], target_gm_id),
        True,
        "",
    )


def _run_topology_size(
    request: dict[str, Any],
    request_dir: str,
    *,
    repo_root: str | None,
    artifacts_dir: str | None,
    timeout_s: float | None,
) -> dict[str, Any]:
    """Solve a coupled ``diff_pair_mirror_tail`` topology's three widths
    jointly, against the *assembled* circuit in ngspice -- see this module's
    docstring's "Coupled multi-device topology sizing" section for the
    method and ``docs/cli/size.md`` for the full request/response schema.
    """
    topology = request.get("topology")
    if topology not in SUPPORTED_TOPOLOGIES:
        raise SizeError(
            f"unsupported topology '{topology}' (supported: "
            f"{', '.join(SUPPORTED_TOPOLOGIES)})"
        )
    if "corners" in request:
        raise SizeError(
            "topology mode ('request.topology') does not yet support a "
            "declared corner set ('request.corners') -- size at a single "
            "corner and re-verify with klt sim across corners; see "
            "docs/cli/size.md's 'Known limitations'"
        )

    devices = _parse_topology_devices(request.get("devices"), topology)
    corner = _parse_corner(request.get("corner") or {})
    corner["corner_id"] = _corner_label(corner["process"], corner["temperature_c"])
    budget = _parse_topology_budget(request.get("budget"))
    targets = _parse_topology_targets(request.get("target"), topology)
    bias = _parse_topology_bias(request.get("bias"))

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
    max_joint_iterations = _parse_max_joint_iterations(options)

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

    resolved_vcm_v = bias["vcm_v"] if bias["vcm_v"] is not None else corner["vdd_v"] / 2
    bias_echo = {"vcm_v": resolved_vcm_v, "vout_v": None, "vtail_v": None}

    provenance = build_provenance(
        deck_name=os.path.basename(models_lib),
        deck_path=models_lib,
        pdk=provenance_pdk,
    )
    environment: dict[str, Any] = {
        "engine": "ngspice",
        "engine_version": None,
        # Issue #1274 -- see `run_size`'s own `environment` block.
        "models_lib": _report_path(models_lib, repo_root=repo_root),
    }

    def _finish(payload: dict[str, Any]) -> dict[str, Any]:
        if keep_artifacts:
            payload["environment"]["artifacts_dir"] = work_dir
        else:
            _cleanup_dir(work_dir)
        return payload

    # ---- Phase A: characterize each role's own gm/Id(W) curve ------------ #
    #
    # One diode-connected width sweep per role, at that role's nominal
    # current -- the same single ngspice invocation per sweep single-device
    # mode uses (see the "Two-invocation-per-request design" docstring
    # section). These curves are what the joint solve inverts to turn a
    # measured in-circuit gm/Id error into a width correction, without
    # paying an extra ngspice invocation per correction step.
    nominal_currents = _topology_nominal_currents(budget)
    curves: dict[str, list[dict[str, Any]]] = {}
    for role in TOPOLOGY_ROLES:
        device = devices[role]
        w_grid = _log_space(device["w_min_um"], device["w_max_um"], sweep_points)
        points, version, log_path, diagnostic = _run_sweep(
            device=device,
            corner=corner,
            target={"id_a": nominal_currents[role], "gm_id": targets[role]["gm_id"]},
            models_lib=models_lib,
            w_values=w_grid,
            work_dir=work_dir,
            deck_name=f"{role}_sweep.cir",
            log_name=f"{role}_sweep.log",
            timeout_s=timeout_s,
        )
        if version is not None:
            environment["engine_version"] = version
        valid = [
            p for p in points if p["gm_s"] is not None and p["id_a"] not in (None, 0.0)
        ]
        for point in valid:
            point["gm_id"] = abs(point["gm_s"]) / abs(point["id_a"])
        if len(valid) < 2:
            return _finish(
                _topology_error_payload(
                    topology=topology,
                    corner=corner,
                    budget=budget,
                    bias=bias_echo,
                    targets=targets,
                    devices=devices,
                    gm_id_rel_tol=gm_id_rel_tol,
                    environment=environment,
                    provenance=provenance,
                    reason=(
                        f"the '{role}' role's characterization sweep did not "
                        "return usable operating-point data for at least 2 of "
                        "the swept widths"
                        + (f": {diagnostic}" if diagnostic else "")
                        + (f" (see sweep log: {log_path})" if keep_artifacts else "")
                    ),
                )
            )
        curves[role] = sorted(valid, key=lambda p: p["w_um"])

    # ---- Phase B: joint solve on the assembled topology ------------------ #
    widths_um: dict[str, float] = {}
    feasible: dict[str, bool] = {}
    feasibility_notes: dict[str, str] = {}
    for role in TOPOLOGY_ROLES:
        w_um, role_feasible, note = _invert_curve(curves[role], targets[role]["gm_id"])
        widths_um[role] = w_um
        feasible[role] = role_feasible
        feasibility_notes[role] = note

    solve = _run_joint_solve(
        devices=devices,
        curves=curves,
        widths_um=widths_um,
        targets=targets,
        corner=corner,
        budget=budget,
        vcm_v=resolved_vcm_v,
        models_lib=models_lib,
        work_dir=work_dir,
        timeout_s=timeout_s,
        gm_id_rel_tol=gm_id_rel_tol,
        max_iterations=max_joint_iterations,
    )
    if solve["engine_version"] is not None:
        environment["engine_version"] = solve["engine_version"]

    if solve["final"] is None:
        return _finish(
            _topology_error_payload(
                topology=topology,
                corner=corner,
                budget=budget,
                bias=bias_echo,
                targets=targets,
                devices=devices,
                gm_id_rel_tol=gm_id_rel_tol,
                environment=environment,
                provenance=provenance,
                reason=solve["diagnostic"] or "the joint topology solve failed",
                joint_solve=solve["report"],
            )
        )

    final = solve["final"]
    widths_um = final["widths_um"]
    bias_echo = {
        "vcm_v": resolved_vcm_v,
        "vout_v": final["nodes"].get("out"),
        "vtail_v": final["nodes"].get("tail"),
    }

    device_results = _build_topology_device_results(
        devices=devices,
        widths_um=widths_um,
        targets=targets,
        budget=budget,
        points=final["points"],
        feasible=feasible,
        feasibility_notes=feasibility_notes,
        gm_id_rel_tol=gm_id_rel_tol,
        corner=corner,
        sweep_points=sweep_points,
        iterations_run=solve["report"]["iterations_run"],
    )

    measured = _measured_bias(final["points"], budget)
    control_status = [
        entry["status"]
        for entry in device_results.values()
        if entry["is_control"] and entry["status"] != "error"
    ]
    all_feasible = all(feasible.values())
    status = (
        "pass"
        if (
            all_feasible and control_status and all(s == "pass" for s in control_status)
        )
        else "fail"
    )

    return _finish(
        {
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "topology": topology,
            "corner": _corner_public(corner),
            "budget": {**budget, "measured": measured},
            "bias": bias_echo,
            "roles": {
                role: {
                    "w_um": widths_um[role],
                    "l_um": devices[role]["l_um"],
                    "target_gm_id": targets[role]["gm_id"],
                    "control_instance": _ROLE_CONTROL_INSTANCE[role],
                    "feasible": feasible[role],
                }
                for role in TOPOLOGY_ROLES
            },
            "devices": device_results,
            "tolerance": {"gm_id_rel": gm_id_rel_tol},
            "joint_solve": solve["report"],
            "method": _topology_method(
                topology=topology,
                solve_report=solve["report"],
                widths_um=widths_um,
                feasible=feasible,
                feasibility_notes=feasibility_notes,
                sweep_points=sweep_points,
            ),
            "environment": environment,
            "provenance": provenance,
        }
    )


def _topology_nominal_currents(budget: dict[str, Any]) -> dict[str, float]:
    """Each role's nominal current from the shared budget -- the starting
    point for the characterization sweep, not a constraint on the solve
    (the joint solve reads back whatever current the assembled circuit
    actually settles at, see :func:`_run_joint_solve`)."""
    return {
        "tail": budget["tail_current_a"],
        "input_pair": budget["branch_current_a"],
        "mirror": budget["branch_current_a"],
    }


def _instance_nominal_current(key: str, budget: dict[str, Any]) -> float:
    if key == "tail_ref":
        return budget["reference_current_a"]
    if key == "tail":
        return budget["tail_current_a"]
    return budget["branch_current_a"]


def _run_joint_solve(
    *,
    devices: dict[str, dict[str, Any]],
    curves: dict[str, list[dict[str, Any]]],
    widths_um: dict[str, float],
    targets: dict[str, dict[str, Any]],
    corner: dict[str, Any],
    budget: dict[str, Any],
    vcm_v: float,
    models_lib: str,
    work_dir: str,
    timeout_s: float,
    gm_id_rel_tol: float,
    max_iterations: int,
) -> dict[str, Any]:
    """The coupled solve proper: iterate the three widths against the
    **assembled** ``diff_pair_mirror_tail`` circuit, one ngspice DC
    operating-point invocation per candidate point.

    Each iteration writes the whole topology at the current candidate
    ``(W_tail, W_input, W_mirror)``, runs ngspice, and reads back every
    instance's *in-circuit* ``gm``/``Id``/``Vgs``/``Vth``/``Vdsat`` at its
    real coupled ``Vds`` -- not a per-device diode-connected surrogate. A
    role whose control instance misses its gm/Id target is corrected by
    inverting that role's own already-swept ``gm_id(W)`` curve *shifted by
    the measured in-circuit offset*:

        offset_r   = gm_id_measured_r(W_k) - curve_r(W_k)
        W_{k+1, r} = curve_r^-1(target_r - offset_r)

    The offset absorbs everything the diode-connected sweep cannot model --
    the real ``Vds``, the real current the circuit settles at (which is not
    exactly the declared budget), and the body effect on the input pair's
    raised source node -- while the curve supplies the local slope, so this
    converges like a Newton step without needing a per-step derivative
    evaluation. All three roles are corrected simultaneously from the *same*
    coupled evaluation, which is what makes this one joint solve rather than
    three independent single-device solves: a width change in any role moves
    every other role's operating point, and the next iteration measures that
    directly.

    Returns ``{"final", "report", "engine_version", "diagnostic"}``. Never
    raises -- an evaluator failure yields ``final: None`` and a populated
    ``diagnostic``, the same convention every other ngspice-invoking helper
    in this module follows.
    """
    engine_version: str | None = None
    iterations: list[dict[str, Any]] = []
    final: dict[str, Any] | None = None
    diagnostic: str | None = None
    converged = False
    candidate = dict(widths_um)

    for index in range(max_iterations):
        deck_path = os.path.join(work_dir, f"joint_{index}.cir")
        log_path = os.path.join(work_dir, f"joint_{index}.log")
        _write_topology_deck(
            deck_path=deck_path,
            devices=devices,
            widths_um=candidate,
            corner=corner,
            budget=budget,
            vcm_v=vcm_v,
            models_lib=models_lib,
        )
        log_text, version, run_diagnostic = _invoke_ngspice(
            deck_path, log_path, timeout_s
        )
        if version is not None:
            engine_version = version
        points, nodes = _parse_topology_log(log_text)

        missing = [
            key
            for key, _instance, _role, _control in TOPOLOGY_INSTANCES
            if points.get(key) is None
            or points[key].get("gm_s") is None
            or not points[key].get("id_a")
        ]
        if missing:
            diagnostic = (
                "the joint topology operating-point run did not return usable "
                f"data for instance(s): {', '.join(missing)}"
                + (f": {run_diagnostic}" if run_diagnostic else "")
                + f" (see log: {log_path})"
            )
            iterations.append(
                {
                    "index": index,
                    "widths_um": dict(candidate),
                    "gm_id": None,
                    "gm_id_rel_error": None,
                    "worst_abs_rel_error": None,
                    "status": "error",
                }
            )
            break

        measured_gm_id = {}
        rel_error = {}
        for role in TOPOLOGY_ROLES:
            point = points[_ROLE_CONTROL_INSTANCE[role]]
            gm_id = abs(point["gm_s"]) / abs(point["id_a"])
            measured_gm_id[role] = gm_id
            rel_error[role] = (gm_id - targets[role]["gm_id"]) / targets[role]["gm_id"]
        worst = max(abs(value) for value in rel_error.values())

        iterations.append(
            {
                "index": index,
                "widths_um": dict(candidate),
                "gm_id": dict(measured_gm_id),
                "gm_id_rel_error": dict(rel_error),
                "worst_abs_rel_error": worst,
                "status": "ok",
            }
        )
        final = {"widths_um": dict(candidate), "points": points, "nodes": nodes}

        if worst <= gm_id_rel_tol:
            converged = True
            break
        if index == max_iterations - 1:
            break

        next_candidate: dict[str, float] = {}
        for role in TOPOLOGY_ROLES:
            device = devices[role]
            offset = measured_gm_id[role] - _interp_gm_id_at_width(
                curves[role], candidate[role]
            )
            shifted_target = targets[role]["gm_id"] - offset
            w_next, _feasible, _note = _invert_curve(curves[role], shifted_target)
            next_candidate[role] = min(
                max(w_next, device["w_min_um"]), device["w_max_um"]
            )
        if all(
            abs(math.log(next_candidate[role]) - math.log(candidate[role]))
            < _JOINT_WIDTH_STEP_EPS
            for role in TOPOLOGY_ROLES
        ):
            # The correction step has stopped moving (every role is pinned at
            # a width bound, or the curve simply cannot reach its shifted
            # target) -- iterating further only repeats the same ngspice run.
            break
        candidate = next_candidate

    report = {
        "method": (
            "coupled fixed-point on the assembled topology: one ngspice DC "
            "operating-point run per candidate (W_tail, W_input, W_mirror), "
            "each role corrected by inverting its own swept gm/Id(W) curve "
            "shifted by the measured in-circuit offset"
        ),
        "max_iterations": max_iterations,
        "iterations_run": len(iterations),
        "converged": converged,
        "gm_id_rel_tol": gm_id_rel_tol,
        "iterations": iterations,
    }
    return {
        "final": final,
        "report": report,
        "engine_version": engine_version,
        "diagnostic": diagnostic,
    }


def _measured_bias(points: dict[str, Any], budget: dict[str, Any]) -> dict[str, Any]:
    """The coupled quantities the assembled circuit actually settled at --
    the real tail current, how it split across the two input-pair legs, and
    the mirror's realized current ratio. These are *outputs* of the joint
    solve, not request inputs: a 50/50 split and a 1:1 mirror are what the
    topology is supposed to deliver, and reporting the measured values is
    how a caller checks that it did (issue #768's "returns a sized operating
    point for all devices jointly")."""
    tail_id = abs(points["tail"]["id_a"])
    input_a_id = abs(points["input_a"]["id_a"])
    input_b_id = abs(points["input_b"]["id_a"])
    mirror_a_id = abs(points["mirror_a"]["id_a"])
    mirror_b_id = abs(points["mirror_b"]["id_a"])
    branch_total = input_a_id + input_b_id
    return {
        "tail_current_a": tail_id,
        "reference_current_a": abs(points["tail_ref"]["id_a"]),
        "branch_current_a": {"input_a": input_a_id, "input_b": input_b_id},
        "tail_split": (
            [input_a_id / branch_total, input_b_id / branch_total]
            if branch_total
            else None
        ),
        "tail_mirror_ratio": (
            tail_id / abs(points["tail_ref"]["id_a"])
            if points["tail_ref"]["id_a"]
            else None
        ),
        "mirror_ratio": (mirror_b_id / mirror_a_id if mirror_a_id else None),
        "tail_current_rel_error": (
            (tail_id - budget["tail_current_a"]) / budget["tail_current_a"]
        ),
    }


def _build_topology_device_results(
    *,
    devices: dict[str, dict[str, Any]],
    widths_um: dict[str, float],
    targets: dict[str, dict[str, Any]],
    budget: dict[str, Any],
    points: dict[str, Any],
    feasible: dict[str, bool],
    feasibility_notes: dict[str, str],
    gm_id_rel_tol: float,
    corner: dict[str, Any],
    sweep_points: int,
    iterations_run: int,
) -> dict[str, Any]:
    """One fully-explained result per *instance* in the assembled topology
    (six of them, three solved widths) -- each carrying its own in-circuit
    operating point, gm/Id, inversion level, and the rationale sentence
    issue #768's third acceptance criterion requires ("every device's result
    states its gm/Id and inversion-level rationale")."""
    results: dict[str, Any] = {}
    for key, instance, role, is_control in TOPOLOGY_INSTANCES:
        device = devices[role]
        point = dict(points[key])
        point["w_um"] = widths_um[role]
        operating_point, gm_id, vov, vov_is_approx = _op_point_from_confirmed(
            point, device
        )
        target_gm_id = targets[role]["gm_id"]
        nominal_id = _instance_nominal_current(key, budget)
        gm_id_rel_error = (gm_id - target_gm_id) / target_gm_id
        id_rel_error = (abs(point["id_a"]) - nominal_id) / nominal_id
        status = "pass" if abs(gm_id_rel_error) <= gm_id_rel_tol else "fail"
        results[key] = {
            "role": role,
            "instance": instance,
            "is_control": is_control,
            "placement": _INSTANCE_DESCRIPTION[key],
            "device": device,
            "status": status,
            "target": {"gm_id": target_gm_id, "id_a": nominal_id},
            # Issue #770: hoisted rationale fields -- see the single-device
            # payload's own copy of this comment.
            "gm_id_target": target_gm_id,
            "gm_id_achieved": gm_id,
            "inversion_level": operating_point["inversion_level"],
            "operating_point": operating_point,
            "margins": {
                "gm_id_rel_error": gm_id_rel_error,
                "id_rel_error": id_rel_error,
            },
            "rationale": _instance_rationale(
                key=key,
                role=role,
                is_control=is_control,
                device=device,
                w_um=widths_um[role],
                target_gm_id=target_gm_id,
                gm_id=gm_id,
                operating_point=operating_point,
                vov=vov,
                vov_is_approx=vov_is_approx,
                nominal_id=nominal_id,
                feasible=feasible[role],
                feasibility_note=feasibility_notes[role],
                corner=corner,
                sweep_points=sweep_points,
                iterations_run=iterations_run,
            ),
        }
    return results


def _instance_rationale(
    *,
    key: str,
    role: str,
    is_control: bool,
    device: dict[str, Any],
    w_um: float,
    target_gm_id: float,
    gm_id: float,
    operating_point: dict[str, Any],
    vov: float | None,
    vov_is_approx: bool,
    nominal_id: float,
    feasible: bool,
    feasibility_note: str,
    corner: dict[str, Any],
    sweep_points: int,
    iterations_run: int,
) -> str:
    """The per-device "why this size, why this inversion level" sentence.

    Deliberately states the *measured in-circuit* gm/Id (not the
    diode-connected surrogate the characterization sweep used) and the
    inversion level's own numeric basis, so a caller can audit the sizing
    decision without re-running the tool.
    """
    parts = [
        f"[{key}] {role} role, {_INSTANCE_DESCRIPTION[key]}: {device['kind']} "
        f"'{device['model']}' sized to W={w_um:.4g}um at L={device['l_um']}um "
        f"(nf={device['nf']}, mult={device['mult']})."
    ]
    if is_control:
        parts.append(
            f"This instance is the '{role}' role's control device: its "
            "in-circuit gm/Id is what the joint solve drove to the target by "
            f"correcting W ({iterations_run} coupled ngspice operating-point "
            f"iteration(s), seeded from a {sweep_points}-point diode-connected "
            f"gm/Id(W) sweep at the '{corner['corner_id']}' corner)."
        )
    else:
        parts.append(
            f"This instance shares the '{role}' role's solved width with its "
            "matched partner rather than adding a separate degree of freedom, "
            "so its operating point is reported as measured, not targeted "
            "independently."
        )
    parts.append(
        f"Measured in-circuit gm/Id={gm_id:.4g} S/A against the target "
        f"{target_gm_id:.4g} S/A "
        f"({(gm_id - target_gm_id) / target_gm_id:+.2%}), at "
        f"Id={abs(operating_point['id_a']):.4g}A "
        f"(nominal {nominal_id:.4g}A from the shared tail-current budget)."
    )
    if not feasible:
        parts.append(
            f"The '{role}' role's target gm/Id={target_gm_id:.4g} is not "
            f"reachable within [{device['w_min_um']}, {device['w_max_um']}]um "
            f"at this current -- {feasibility_note}; the closest achievable "
            "width was used instead of extrapolating."
        )
    inversion_level = operating_point["inversion_level"]
    if vov is not None and not vov_is_approx:
        parts.append(
            f"Inversion level '{inversion_level}' from Vov=Vgs-Vth={vov:.4g}V "
            "(Vov<=0: weak; 0<Vov<0.1V: moderate; Vov>=0.1V: strong -- a "
            "standard rule-of-thumb threshold, not a precise "
            "inversion-coefficient computation)."
        )
    elif vov is not None and vov_is_approx:
        parts.append(
            f"Inversion level '{inversion_level}' from Vov~=Vdsat={vov:.4g}V "
            "(Vth not exposed by this device model; Vdsat approximates Vov "
            "for a square-law device, same thresholds as above)."
        )
    else:
        parts.append(
            "Inversion level could not be classified: the device model did "
            "not expose Vth or Vdsat at this operating point."
        )
    return " ".join(parts)


def _topology_method(
    *,
    topology: str,
    solve_report: dict[str, Any],
    widths_um: dict[str, float],
    feasible: dict[str, bool],
    feasibility_notes: dict[str, str],
    sweep_points: int,
) -> dict[str, Any]:
    sized = ", ".join(f"{role}={widths_um[role]:.4g}um" for role in TOPOLOGY_ROLES)
    rationale = (
        f"Coupled '{topology}' sizing: the three widths ({sized}) were solved "
        "jointly against the assembled circuit, not device by device. Each "
        f"role's gm/Id(W) curve was first characterized by a {sweep_points}-"
        "point diode-connected width sweep at its nominal current (one "
        "ngspice invocation per role) to seed the search and supply the local "
        "slope; the solve itself then ran "
        f"{solve_report['iterations_run']} ngspice DC operating-point "
        "evaluation(s) of the *whole* topology, each one reading back every "
        "device's real in-circuit gm/Id at its actual coupled Vds and "
        "correcting all three widths simultaneously from that single coupled "
        "measurement."
    )
    if solve_report["converged"]:
        rationale += (
            " Every role's control device landed within the requested gm/Id "
            f"tolerance ({solve_report['gm_id_rel_tol']:.3g} relative)."
        )
    else:
        rationale += (
            " The joint iteration stopped before every role's control device "
            "was inside the requested gm/Id tolerance -- see "
            "joint_solve.iterations for the per-iteration trajectory."
        )
    for role in TOPOLOGY_ROLES:
        if not feasible[role]:
            rationale += (
                f" The '{role}' role's target is out of reach within its "
                f"declared width bounds: {feasibility_notes[role]}."
            )
    return {
        "name": "coupled multi-device joint solve (diff-pair + mirror + tail)",
        "bias": (
            "assembled topology at DC: diode-connected tail-bias replica, "
            "input pair at the declared common-mode voltage, output held at "
            "the common-mode point by a DC-only feedback network"
        ),
        "rationale": rationale,
        "sweep_points": sweep_points,
        "joint_iterations": solve_report["iterations_run"],
        "converged": solve_report["converged"],
        "feasible": all(feasible.values()),
    }


def _topology_error_payload(
    *,
    topology: str,
    corner: dict[str, Any],
    budget: dict[str, Any],
    bias: dict[str, Any],
    targets: dict[str, dict[str, Any]],
    devices: dict[str, dict[str, Any]],
    gm_id_rel_tol: float,
    environment: dict[str, Any],
    provenance: dict[str, Any],
    reason: str,
    joint_solve: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """``status: "error"`` for topology mode -- the evaluator itself could
    not produce a trustworthy result, so there are no device results to
    report. ``method`` is still populated (this module's "no stated method
    is rejected" precedent applies to every status), as are the request
    echoes, so a caller can tell *what* was attempted."""
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "error",
        "topology": topology,
        "corner": _corner_public(corner),
        "budget": {**budget, "measured": None},
        "bias": bias,
        "roles": {
            role: {
                "w_um": None,
                "l_um": devices[role]["l_um"],
                "target_gm_id": targets[role]["gm_id"],
                "control_instance": _ROLE_CONTROL_INSTANCE[role],
                "feasible": None,
            }
            for role in TOPOLOGY_ROLES
        },
        "devices": None,
        "tolerance": {"gm_id_rel": gm_id_rel_tol},
        "joint_solve": joint_solve,
        "method": {
            "name": "coupled multi-device joint solve (diff-pair + mirror + tail)",
            "bias": (
                "assembled topology at DC: diode-connected tail-bias replica, "
                "input pair at the declared common-mode voltage, output held "
                "at the common-mode point by a DC-only feedback network"
            ),
            "rationale": f"Evaluator error: {reason}",
            "sweep_points": None,
            "joint_iterations": (
                joint_solve["iterations_run"] if joint_solve is not None else 0
            ),
            "converged": False,
            "feasible": None,
        },
        "environment": environment,
        "provenance": provenance,
    }


def _write_topology_deck(
    *,
    deck_path: str,
    devices: dict[str, dict[str, Any]],
    widths_um: dict[str, float],
    corner: dict[str, Any],
    budget: dict[str, Any],
    vcm_v: float,
    models_lib: str,
) -> None:
    """Generate one candidate point's ``diff_pair_mirror_tail`` deck: the
    *actual* coupled circuit (never a per-device diode-connected surrogate)
    at the candidate widths, instrumented to read back all six instances'
    operating points plus the output/tail node voltages.

    Topology, mirroring this repo's own hand-sized 5T OTA canary
    (``examples/design-pipeline/ota_5t.spice``) generalized to the request's
    declared models and widths:

    - ``Iref`` injects ``tail_current_a / tail_mirror_ratio`` into the
      diode-connected replica ``Xtailref``, whose gate drives ``Xtail``; the
      ratio is realized as ``Xtail``'s ``mult`` multiplier, so the tail
      device sources ``tail_current_a``.
    - The input pair's sources tie to ``Xtail``'s drain (``tail``), gates to
      the common-mode bias.
    - The PMOS mirror load's diode leg (``Xmirrora``) sits on the ``n1``
      branch and its output leg (``Xmirrorb``) on ``out``.
    - ``Lfb``/``Cfb`` form the canary's own DC-only feedback network: at DC
      the inductor is a short, so the output is held at the common-mode
      operating point instead of floating to a fragile, corner-dependent
      balance point (the exact failure the canary's own
      ``05-sizing.json`` records from its first characterization pass).
      The capacitor opens the loop at AC, keeping the deck reusable for a
      later AC pass.
    """
    tail = devices["tail"]
    input_pair = devices["input_pair"]
    mirror = devices["mirror"]
    vdd = corner["vdd_v"]

    def _x_params(device: dict[str, Any], w_um: float, mult_scale: float = 1.0) -> str:
        return (
            f"l={device['l_um']!r} w={w_um!r} nf={device['nf']!r} "
            f"mult={device['mult'] * mult_scale!r}"
        )

    lines = [
        "* klt size -- generated coupled topology deck "
        "(diff_pair_mirror_tail), do not edit"
    ]
    process_sections = corner.get("process_sections")
    if process_sections:
        for section in process_sections:
            lines.append(f".lib {models_lib} {section}")
    else:
        lines.append(f".lib {models_lib} {corner['process']}")
    lines.append(f"Vdd vdd 0 DC {vdd!r}")
    lines.append(f"Vcm cm 0 DC {vcm_v!r}")
    lines.append("Vin inp cm DC 0")
    lines.append(f"Iref vdd nbias DC {budget['reference_current_a']!r}")
    # DC-only feedback: short at DC (holds out at the common-mode point),
    # open at AC. See this function's docstring.
    lines.append("Lfb out inn 1e9")
    lines.append("Cfb inn cm 1e3")

    lines.append(
        f"Xtailref nbias nbias 0 0 {tail['model']} {_x_params(tail, widths_um['tail'])}"
    )
    lines.append(
        f"Xtail tail nbias 0 0 {tail['model']} "
        f"{_x_params(tail, widths_um['tail'], budget['tail_mirror_ratio'])}"
    )

    input_params = _x_params(input_pair, widths_um["input_pair"])
    lines.append(f"Xinputa n1 inp tail 0 {input_pair['model']} {input_params}")
    lines.append(f"Xinputb out inn tail 0 {input_pair['model']} {input_params}")

    mirror_params = _x_params(mirror, widths_um["mirror"])
    lines.append(f"Xmirrora n1 n1 vdd vdd {mirror['model']} {mirror_params}")
    lines.append(f"Xmirrorb out n1 vdd vdd {mirror['model']} {mirror_params}")

    # A top-level dot-card, like `_write_sweep_deck`'s own `.temp` handling.
    lines.append(f".temp {corner['temperature_c']!r}")

    lines.append(".control")
    lines.append("op")
    for key, instance, role, _is_control in TOPOLOGY_INSTANCES:
        op_element = devices[role]["op_point_element"]
        lines.append(f"echo KLT_SIZE_INSTANCE {key}")
        for param in _OP_PARAMS:
            lines.append(f"print @m.{instance}.{op_element}[{param}]")
    lines.append("echo KLT_SIZE_INSTANCE __nodes__")
    for node in ("out", "tail", "n1", "nbias"):
        lines.append(f"print v({node})")
    lines.append("quit")
    lines.append(".endc")
    lines.append(".end")

    with open(deck_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def _parse_topology_log(
    log_text: str,
) -> tuple[dict[str, Any], dict[str, float]]:
    """Parse one joint-topology deck's log into ``(points, nodes)``:
    per-instance op-point dicts keyed by :data:`TOPOLOGY_INSTANCES` key,
    plus the node voltages printed after the ``__nodes__`` marker. Mirrors
    :func:`_parse_sweep_log`'s marker-then-values grouping (there keyed by
    sweep-point index instead of instance key).
    """
    points: dict[str, Any] = {}
    nodes: dict[str, float] = {}
    current_key: str | None = None
    current_values: dict[str, float] = {}

    def _flush() -> None:
        if current_key is None or current_key == "__nodes__":
            return
        entry: dict[str, Any] = {}
        for param in _OP_PARAMS:
            entry[_OP_PARAM_KEYS[param]] = current_values.get(param)
        points[current_key] = entry

    for raw_line in log_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        marker = _INSTANCE_MARKER_RE.match(line)
        if marker:
            _flush()
            current_key = marker.group(1)
            current_values = {}
            continue
        if current_key is None:
            continue
        node_match = _NODE_VALUE_RE.match(line)
        if node_match:
            try:
                nodes[node_match.group(1)] = float(node_match.group(2))
            except ValueError:
                pass
            continue
        value_match = _OP_VALUE_RE.match(line)
        if value_match:
            key, raw_value = value_match.groups()
            try:
                current_values[key] = float(raw_value)
            except ValueError:
                continue
    _flush()

    for key, _instance, _role, _control in TOPOLOGY_INSTANCES:
        points.setdefault(key, None)
    return points, nodes

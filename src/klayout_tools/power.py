"""Extract a routed layout's power grid as a resistive network, solve it for
its static (DC) IR drop, and emit a per-net EM (electromigration)
current-density verdict (``klt power``, issues #844/#845/#846, Phases
1a/1b/1c of the power/IR-drop + EM signoff epic #712).

Pure library: :func:`run_power` returns plain Python data (a JSON-
serialisable ``dict``) and never prints -- serialisation and human-readable
formatting live in ``cli/power_cmd.py``, matching every other ``klt`` verb
(see ``layers.py``'s docstring on the same convention).

Scope of this module (see ``docs/cli/power.md`` for the full picture):
``klt power`` is defined end to end as JSON in (a routed layout + a
power-net definition + a per-instance current model) / JSON out (an IR-drop
map + worst-case droop + a per-net EM verdict). All three phases have
landed:

- **#844, Phase 1a** -- the interface and the resistive-network extraction:
  turning a routed layout's named power/ground nets into a graph of nodes +
  segment resistances (``networks[]`` below).
- **#845, Phase 1b** -- the static IR-drop solve: given ``pads`` (where the
  supply is delivered) and a ``current_model`` (what each instance draws),
  solve that network for its DC node voltages, and report an IR-drop map +
  the worst-case droop (``ir_drop_map``/``worst_case_droop_mv``). The linear
  algebra lives in :mod:`klayout_tools.ir_solver`, deliberately geometry-free
  so it can be validated against closed-form resistive networks with no
  layout involved; this module is the geometry-to-network binding.
- **#846, Phase 1c** -- the per-net EM (electromigration) current-density
  verdict: each ``stackup``/``vias`` role may additionally declare its own
  EM current-density limit (``current_limit_a_per_um``/``current_limit_a``,
  expressed the same way a real PDK's own tech LEF already expresses it --
  see :func:`_compute_em_verdict`'s docstring) plus a free-text
  ``current_limit_source`` citation. ``em_verdict`` compares every solved
  edge's branch current (1b's ``ir_drop_map``) against its own cited limit
  and rolls the result up per net -- ``None`` when there was no IR-drop
  solve at all (nothing to compare).
- **#1320, epic #712's Phase 2** -- the activity-weighted current mode: a
  ``current_model.instances[]`` entry may set an ``activity`` object
  (``toggle_rate_hz`` * ``capacitance_f`` * ``vdd_v``, a synthesis/P&R
  switching-activity estimate) instead of a static ``current_a`` -- see
  :func:`_validate_activity`. Additive to Phase 1b's static-only model: a
  spec that only ever set ``current_a`` behaves exactly as before.

Connectivity: geometry is traced with ``klayout.db.LayoutToNetlist`` used
purely for wire/via connectivity (no device recognition registered) --
exactly the same API ``extract.py``'s ``_extract_netlist`` uses for its
metal/via connectivity graph, scoped down to only the caller-declared power-
grid layers. A net's *name* comes from text labels on a caller-declared
``label_layer`` per metal role (the routed layout's own pin/net-name text,
e.g. sky130's ``met1.pin``/``68/5`` datatype convention) -- there is no
naming without at least one labelled layer in the spec's ``stackup``.

A named power net commonly resolves to **several disconnected islands**, not
one connected mesh: ``klt par`` (issue #700) deliberately does not run PDN
(power-grid) generation (see ``place_and_route.py``'s own "Deliberately out
of scope for this v1" note), so a routed design's power/ground geometry is
whatever the standard-cell rows themselves contribute -- typically many
un-strapped per-row rail segments. This is not a bug to paper over: each
island is reported separately (an ``island_id`` per electrically distinct
cluster), because that is exactly the real electrical topology a later
IR-drop solve (#845) needs to reason about (an un-strapped island has no
path to a pad and cannot be solved for droop without saying so).

Resistor-network model (MVP, stated plainly):

- Each layer's net geometry, read via ``LayoutToNetlist.polygons_of_net``
  and merged into maximal polygons, becomes **one metal edge per
  rectangular merged polygon** between two endpoint nodes at the polygon's
  ends along its longer axis -- ``resistance_ohm =
  sheet_resistance_ohm_per_sq * length / width``. Every merged polygon in
  this module's own real-fixture validation
  (``tests/corpus/place_and_route/gcd.gds.gz``, a genuine ``klt par``
  output) is an axis-aligned box, matching this model exactly.
- A **non-rectangular** merged polygon -- the normal shape of a real PDN
  ring or an L/T/comb-shaped bus -- is *decomposed*, not approximated
  (issue #2171). It is cut into the grid of rectangles formed by extending
  every one of its own vertex coordinates across it, and each rectangle
  becomes its own resistor: a centre node, a shared port node on every
  boundary it has with a neighbouring rectangle, and a terminal port node
  at each free end of its longer axis, with
  ``sheet_resistance_ohm_per_sq * (half the cell's extent along the flow
  direction) / (its conducting width)`` per centre-to-port edge. The
  previous behaviour -- one resistor sized by the polygon's *bounding box*
  -- was silently **non-conservative** on any such shape: an L whose arms
  are 20 um wide inside a 100x100 um bounding box was modelled as a single
  square of metal, understating resistance (and droop) roughly nine-fold
  while overstating the per-edge EM limit.
- A merged polygon that cannot be decomposed exactly -- not axis-aligned
  (45-degree geometry), or so many-vertexed that its grid would exceed
  :data:`MAX_DECOMPOSITION_CELLS` -- makes its island **unsolved**: the
  island is reported with an ``unsolved_reason`` and no nodes/edges at all,
  plus a ``warnings`` entry. Refusing is honest; approximating would move
  the IR/EM verdict in the unsafe direction.
- Each via layer's net geometry becomes **one via edge** per merged via
  polygon, connecting the *nearest* existing node (by straight-line
  distance) on each of the two metal layers the via spec declares it
  bridges -- not a true T-junction split of the rail it taps. This is a
  documented v1 simplification (see ``docs/cli/power.md``'s "Scope and
  limitations"): precise enough to preserve every island's real connectivity
  and rail resistance, at the cost of a small positional error in exactly
  where along a rail a tap lands.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ._layout import load_layout, select_top_cells
from ._layout import region as _region
from ._layout import texts as _texts
from ._paths import _load_spec_json, _parse_layer_datatype, _validate_via_entries
from .coverage import build_check_coverage, coverage_rollup, rollup_status, work_id
from .ir_solver import solve_ir_drop, worst_deviation

if TYPE_CHECKING:
    import klayout.db as kdb

#: `1` -- the resistive-network extraction (issue #844, Phase 1a of epic
#: #712), the static IR-drop solve (#845, Phase 1b), and the per-net EM
#: verdict (#846, Phase 1c). Each phase added its fields **additively** --
#: every field an earlier phase documented is unchanged -- so no bump was
#: needed for any of them (see docs/cli/power.md and
#: docs/json-contract.md's additive-envelope design).
SCHEMA_VERSION = 1

#: The most mesh cells one non-rectangular merged polygon may be decomposed
#: into (issue #2171). A polygon's rectangular decomposition is the grid
#: formed by extending every vertex coordinate across it, so a pathological
#: segment (thousands of vertices -- fill-like or stair-stepped geometry)
#: could otherwise explode into a network no consumer can read and no solve
#: can finish. Over the cap the island is reported with an
#: ``unsolved_reason`` instead, never with a bounding-box approximation:
#: refusing is honest, approximating understates resistance and overstates
#: EM headroom. Deliberately module-level so an operator (or a test) can
#: raise it without a spec change.
MAX_DECOMPOSITION_CELLS = 4096


class PowerError(Exception):
    """Raised when ``klt power`` cannot run: a bad layout/spec file, a
    malformed stackup/via/power-net declaration, an unresolvable top cell,
    or a request whose declared power nets match no geometry at all.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback -- see ``docs/json-contract.md``.
    """


#: The ``power_nets`` match modes (issue #2171). ``"label"`` -- the default,
#: and the only one that works on a real pad-connected PDN -- matches a net
#: that *carries* the requested label among the comma-joined label set
#: KLayout names it after; ``"exact"`` is the pre-#2171 behaviour, matching
#: only a net whose whole ``expanded_name()`` is the requested string. See
#: :func:`_matched_nets` and ``docs/cli/power.md``'s "Matching a power net".
_MATCH_MODES = ("label", "exact")


def _parse_power_net_entry(entry: Any, index: int, spec_path: str) -> tuple[str, str]:
    """One ``power_nets`` entry -> ``(name, match_mode)``.

    A bare string is the common form (``"VDD_CORE"``, matched by label); the
    object form ``{"name": ..., "match": "exact"|"label"}`` exists so a spec
    can opt back into whole-name matching when two nets in the same layout
    deliberately share a label.
    """
    if isinstance(entry, dict):
        name = str(entry.get("name", "")).strip()
        match = str(entry.get("match", _MATCH_MODES[0]))
    elif isinstance(entry, str):
        name, match = entry.strip(), _MATCH_MODES[0]
    else:
        name, match = "", _MATCH_MODES[0]
    if not name:
        raise PowerError(
            f"spec '{spec_path}': power_nets[{index}] must be a non-empty string or "
            "an object with a non-empty 'name' (and an optional 'match' mode)"
        )
    if match not in _MATCH_MODES:
        raise PowerError(
            f"spec '{spec_path}': power_nets[{index}] 'match' must be one of "
            f"{', '.join(repr(mode) for mode in _MATCH_MODES)} (got {match!r})"
        )
    return name, match


def _validate_power_nets(spec: dict[str, Any], spec_path: str) -> list[dict[str, str]]:
    """The spec's ``power_nets`` as ``[{"name", "match"}]``, deduplicated by
    name in first-seen order (the order ``networks`` is reported in)."""
    raw = spec.get("power_nets")
    if not isinstance(raw, list) or not raw:
        raise PowerError(f"spec '{spec_path}' must have a non-empty 'power_nets' array")
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        name, match = _parse_power_net_entry(entry, index, spec_path)
        if name in seen:
            continue
        seen.add(name)
        entries.append({"name": name, "match": match})
    return entries


def _validate_stackup(spec: dict[str, Any], spec_path: str) -> list[dict[str, Any]]:
    raw = spec.get("stackup")
    if not isinstance(raw, list) or not raw:
        raise PowerError(f"spec '{spec_path}' must have a non-empty 'stackup' array")

    entries: list[dict[str, Any]] = []
    names: list[str] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise PowerError(f"spec '{spec_path}': stackup[{i}] must be a JSON object")
        for key in ("name", "layer", "sheet_resistance_ohm_per_sq"):
            if key not in entry:
                raise PowerError(f"spec '{spec_path}': stackup[{i}] missing {key!r}")
        name = str(entry["name"])
        if name in names:
            raise PowerError(f"spec '{spec_path}': duplicate stackup name {name!r}")
        names.append(name)

        layer = _parse_layer_datatype(
            str(entry["layer"]), spec_path, f"stackup[{i}].layer", PowerError
        )
        label_layer = None
        if entry.get("label_layer") is not None:
            label_layer = _parse_layer_datatype(
                str(entry["label_layer"]),
                spec_path,
                f"stackup[{i}].label_layer",
                PowerError,
            )

        try:
            sheet_r = float(entry["sheet_resistance_ohm_per_sq"])
        except (TypeError, ValueError) as exc:
            raise PowerError(
                f"spec '{spec_path}': stackup[{i}].sheet_resistance_ohm_per_sq "
                f"must be a number (got {entry['sheet_resistance_ohm_per_sq']!r})"
            ) from exc
        if sheet_r < 0:
            raise PowerError(
                f"spec '{spec_path}': stackup[{i}].sheet_resistance_ohm_per_sq "
                f"must be >= 0 (got {sheet_r!r})"
            )

        # `current_limit_a_per_um` (issue #846, Phase 1c): this role's own
        # EM (electromigration) current-density limit, expressed the same
        # way the PDK's own tech LEF expresses it -- a per-width-micron
        # current, not a per-area one (see `docs/cli/power.md`'s "EM
        # current-density verdict" section for why: a `DCCURRENTDENSITY`
        # entry in a real sky130 `.tlef` is already `mA/um` at that layer's
        # fixed drawn thickness, so no separate thickness term is needed).
        # Optional -- a stackup entry that omits it simply reports no EM
        # verdict for its edges (`current_limit_a: null`), matching how an
        # omitted `label_layer` simply cannot name a net.
        current_limit_a_per_um = entry.get("current_limit_a_per_um")
        if current_limit_a_per_um is not None:
            try:
                current_limit_a_per_um = float(current_limit_a_per_um)
            except (TypeError, ValueError) as exc:
                raise PowerError(
                    f"spec '{spec_path}': stackup[{i}].current_limit_a_per_um "
                    f"must be a number (got {current_limit_a_per_um!r})"
                ) from exc
            if current_limit_a_per_um < 0:
                raise PowerError(
                    f"spec '{spec_path}': stackup[{i}].current_limit_a_per_um "
                    f"must be >= 0 (got {current_limit_a_per_um!r})"
                )

        # `current_limit_source` (optional): a free-text citation of exactly
        # where `current_limit_a_per_um` came from (e.g. the PDK doc/rule
        # name), echoed back in `em_verdict` -- see `docs/cli/power.md`'s
        # worked example, which cites a real sky130 `.tlef`
        # `DCCURRENTDENSITY` line this way.
        current_limit_source = entry.get("current_limit_source")
        if current_limit_source is not None:
            current_limit_source = str(current_limit_source)

        entries.append(
            {
                "name": name,
                "layer": layer,
                "label_layer": label_layer,
                "sheet_resistance_ohm_per_sq": sheet_r,
                "current_limit_a_per_um": current_limit_a_per_um,
                "current_limit_source": current_limit_source,
            }
        )

    if not any(entry["label_layer"] is not None for entry in entries):
        raise PowerError(
            f"spec '{spec_path}': at least one 'stackup' entry must set "
            "'label_layer' -- otherwise no net in the layout can ever be "
            "matched by name"
        )
    return entries


def _validate_vias(
    spec: dict[str, Any], spec_path: str, stackup_names: list[str]
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for i, entry, name, layer, between in _validate_via_entries(
        spec,
        spec_path,
        stackup_names,
        PowerError,
        required_keys=("layer", "between", "resistance_ohm"),
    ):
        try:
            resistance_ohm = float(entry["resistance_ohm"])
        except (TypeError, ValueError) as exc:
            raise PowerError(
                f"spec '{spec_path}': vias[{i}].resistance_ohm must be a "
                f"number (got {entry['resistance_ohm']!r})"
            ) from exc
        if resistance_ohm < 0:
            raise PowerError(
                f"spec '{spec_path}': vias[{i}].resistance_ohm must be >= 0 "
                f"(got {resistance_ohm!r})"
            )

        # `current_limit_a` (issue #846, Phase 1c): this via role's own EM
        # limit -- unlike a metal role's `current_limit_a_per_um`, a real
        # PDK's `DCCURRENTDENSITY` for a via/cut layer is already a flat
        # per-shape amperage (e.g. sky130's `.tlef` "mA per via"), matching
        # this spec's existing "one merged via polygon = one resistor"
        # model (`resistance_ohm` above): the limit applies verbatim to
        # every merged via edge this role contributes, with no width term.
        current_limit_a = entry.get("current_limit_a")
        if current_limit_a is not None:
            try:
                current_limit_a = float(current_limit_a)
            except (TypeError, ValueError) as exc:
                raise PowerError(
                    f"spec '{spec_path}': vias[{i}].current_limit_a must be a "
                    f"number (got {current_limit_a!r})"
                ) from exc
            if current_limit_a < 0:
                raise PowerError(
                    f"spec '{spec_path}': vias[{i}].current_limit_a must be "
                    f">= 0 (got {current_limit_a!r})"
                )

        current_limit_source = entry.get("current_limit_source")
        if current_limit_source is not None:
            current_limit_source = str(current_limit_source)

        entries.append(
            {
                "name": name,
                "layer": layer,
                "between": between,
                "resistance_ohm": resistance_ohm,
                "current_limit_a": current_limit_a,
                "current_limit_source": current_limit_source,
            }
        )
    return entries


def _require_number(
    entry: dict[str, Any], key: str, spec_path: str, field: str
) -> float:
    if key not in entry:
        raise PowerError(f"spec '{spec_path}': {field} missing {key!r}")
    try:
        return float(entry[key])
    except (TypeError, ValueError) as exc:
        raise PowerError(
            f"spec '{spec_path}': {field}.{key} must be a number (got {entry[key]!r})"
        ) from exc


def _validate_pads(
    spec: dict[str, Any], spec_path: str, power_nets: list[str]
) -> list[dict[str, Any]]:
    """Validate the (optional) ``pads`` array: where each power net's supply
    is actually delivered. Without at least one pad on a net, that net has no
    DC path to a source and no operating point to solve for -- see
    ``docs/cli/power.md``."""
    raw = spec.get("pads", [])
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise PowerError(f"spec '{spec_path}': 'pads' must be an array")

    pads: list[dict[str, Any]] = []
    names: list[str] = []
    for i, entry in enumerate(raw):
        field = f"pads[{i}]"
        if not isinstance(entry, dict):
            raise PowerError(f"spec '{spec_path}': {field} must be a JSON object")
        if "net" not in entry:
            raise PowerError(f"spec '{spec_path}': {field} missing 'net'")
        net = str(entry["net"])
        if net not in power_nets:
            raise PowerError(
                f"spec '{spec_path}': {field}.net {net!r} is not one of "
                f"'power_nets' ({', '.join(power_nets)})"
            )
        name = str(entry.get("name", f"pad{i}"))
        if name in names:
            raise PowerError(f"spec '{spec_path}': duplicate pad name {name!r}")
        names.append(name)
        pads.append(
            {
                "name": name,
                "net": net,
                "x_um": _require_number(entry, "x_um", spec_path, field),
                "y_um": _require_number(entry, "y_um", spec_path, field),
                "voltage_v": _require_number(entry, "voltage_v", spec_path, field),
            }
        )
    return pads


def _validate_activity(
    entry: dict[str, Any],
    spec_path: str,
    field: str,
    default_vdd_v: float | None,
) -> float:
    """Compute one instance's ``activity``-mode current: a synthesis/P&R
    switching-activity estimate -- toggle rate * capacitance * Vdd -- rather
    than a caller-supplied static ``current_a`` (issue #1320, Phase 2 of epic
    #712's power/IR-drop + EM signoff, additive to Phase 1b's static-only
    model; see ``docs/cli/power.md``'s "Activity-weighted current mode").

    This is the standard average-current estimate a synthesis/P&R tool's own
    switching-activity report already implies: a node that transitions at
    ``toggle_rate_hz`` moves ``capacitance_f`` of charge to/from ``vdd_v``
    that many times a second, so its average current is
    ``toggle_rate_hz * capacitance_f * vdd_v`` (equivalently, dynamic power
    ``C * V^2 * f`` divided by ``V``). Every quantity is a magnitude (`>= 0`),
    matching the existing static ``current_a``'s own sign convention -- the
    instance's ``supply_net``/``ground_net`` decide which way the current
    actually flows, not this computation.
    """
    activity = entry["activity"]
    if not isinstance(activity, dict):
        raise PowerError(f"spec '{spec_path}': {field}.activity must be a JSON object")
    activity_field = f"{field}.activity"

    toggle_rate_hz = _require_number(
        activity, "toggle_rate_hz", spec_path, activity_field
    )
    if toggle_rate_hz < 0:
        raise PowerError(
            f"spec '{spec_path}': {activity_field}.toggle_rate_hz must be >= 0 "
            f"(got {toggle_rate_hz!r})"
        )

    capacitance_f = _require_number(
        activity, "capacitance_f", spec_path, activity_field
    )
    if capacitance_f < 0:
        raise PowerError(
            f"spec '{spec_path}': {activity_field}.capacitance_f must be >= 0 "
            f"(got {capacitance_f!r})"
        )

    if "vdd_v" in activity:
        vdd_v = _require_number(activity, "vdd_v", spec_path, activity_field)
    elif default_vdd_v is not None:
        vdd_v = default_vdd_v
    else:
        raise PowerError(
            f"spec '{spec_path}': {activity_field} has no 'vdd_v' and "
            "'current_model.vdd_v' sets no default"
        )
    if vdd_v < 0:
        raise PowerError(
            f"spec '{spec_path}': {activity_field}.vdd_v must be >= 0 (got {vdd_v!r})"
        )

    return toggle_rate_hz * capacitance_f * vdd_v


def _validate_current_model(
    spec: dict[str, Any], spec_path: str, power_nets: list[str]
) -> list[dict[str, Any]] | None:
    """Validate the (optional) ``current_model``: what each instance draws,
    and from/to which nets. Returns ``None`` when the spec declares no
    current model at all (extraction-only, the Phase 1a behaviour), or a list
    of normalised instance records.

    Each instance sets exactly one of two additive current sources (issue
    #1320, Phase 2 of epic #712 -- see ``docs/cli/power.md``): a static
    ``current_a`` magnitude (Phase 1b, unchanged), or an ``activity`` object
    -- a switching-activity estimate (toggle rate * capacitance * Vdd, see
    ``_validate_activity``) an agent can derive straight from a synthesis/P&R
    tool's own activity report instead of hand-picking a static number. Both
    resolve to the same normalised ``current_a`` field every downstream solve
    step already consumes, so the static mode's own behaviour is unchanged.
    """
    raw = spec.get("current_model")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise PowerError(f"spec '{spec_path}': 'current_model' must be a JSON object")

    def _default_net(key: str) -> str | None:
        value = raw.get(key)
        if value is None:
            return None
        net = str(value)
        if net not in power_nets:
            raise PowerError(
                f"spec '{spec_path}': current_model.{key} {net!r} is not one of "
                f"'power_nets' ({', '.join(power_nets)})"
            )
        return net

    default_supply = _default_net("supply_net")
    default_ground = _default_net("ground_net")

    # `vdd_v` (issue #1320): a model-level default supply voltage for every
    # instance's ``activity`` block that omits its own -- the same
    # per-model-default-with-per-instance-override convention
    # `supply_net`/`ground_net` already use above.
    default_vdd_v = raw.get("vdd_v")
    if default_vdd_v is not None:
        try:
            default_vdd_v = float(default_vdd_v)
        except (TypeError, ValueError) as exc:
            raise PowerError(
                f"spec '{spec_path}': current_model.vdd_v must be a number "
                f"(got {default_vdd_v!r})"
            ) from exc
        if default_vdd_v < 0:
            raise PowerError(
                f"spec '{spec_path}': current_model.vdd_v must be >= 0 "
                f"(got {default_vdd_v!r})"
            )

    instances_raw = raw.get("instances")
    if not isinstance(instances_raw, list) or not instances_raw:
        raise PowerError(
            f"spec '{spec_path}': 'current_model' must have a non-empty "
            "'instances' array"
        )

    instances: list[dict[str, Any]] = []
    names: list[str] = []
    for i, entry in enumerate(instances_raw):
        field = f"current_model.instances[{i}]"
        if not isinstance(entry, dict):
            raise PowerError(f"spec '{spec_path}': {field} must be a JSON object")

        name = str(entry.get("name", f"inst{i}"))
        if name in names:
            raise PowerError(f"spec '{spec_path}': duplicate instance name {name!r}")
        names.append(name)

        supply_net = (
            str(entry["supply_net"]) if "supply_net" in entry else default_supply
        )
        if supply_net is None:
            raise PowerError(
                f"spec '{spec_path}': {field} has no 'supply_net' and "
                "'current_model.supply_net' sets no default"
            )
        ground_net = (
            str(entry["ground_net"]) if "ground_net" in entry else default_ground
        )
        for key, net in (("supply_net", supply_net), ("ground_net", ground_net)):
            if net is not None and net not in power_nets:
                raise PowerError(
                    f"spec '{spec_path}': {field}.{key} {net!r} is not one of "
                    f"'power_nets' ({', '.join(power_nets)})"
                )
        if ground_net == supply_net:
            raise PowerError(
                f"spec '{spec_path}': {field} draws from and returns to the "
                f"same net {supply_net!r}"
            )

        has_current_a = "current_a" in entry
        has_activity = "activity" in entry
        if has_current_a and has_activity:
            raise PowerError(
                f"spec '{spec_path}': {field} sets both 'current_a' and "
                "'activity' -- an instance's current comes from exactly one "
                "current source, not both"
            )
        if has_activity:
            current_a = _validate_activity(entry, spec_path, field, default_vdd_v)
            current_source = "activity"
        elif has_current_a:
            current_a = _require_number(entry, "current_a", spec_path, field)
            current_source = "static"
        else:
            raise PowerError(
                f"spec '{spec_path}': {field} missing 'current_a' (a static "
                "instance current) or 'activity' (an activity-weighted "
                "current model -- see docs/cli/power.md)"
            )
        if current_a < 0:
            raise PowerError(
                f"spec '{spec_path}': {field}.current_a must be >= 0 (got "
                f"{current_a!r}) -- an instance's draw is a magnitude; the "
                "supply/ground sign convention is set by 'supply_net'/"
                "'ground_net'"
            )

        instances.append(
            {
                "name": name,
                "supply_net": supply_net,
                "ground_net": ground_net,
                "x_um": _require_number(entry, "x_um", spec_path, field),
                "y_um": _require_number(entry, "y_um", spec_path, field),
                "current_a": current_a,
                "current_source": current_source,
            }
        )
    return instances


def _nearest_endpoint(
    rails: list[dict[str, Any]], x_um: float, y_um: float
) -> str | None:
    """The id of the node (among every rail endpoint recorded for one net on
    one metal layer) nearest to ``(x_um, y_um)`` -- the via-tap
    approximation this module's docstring documents. ``None`` when the net
    has no rail geometry at all on that layer."""
    best_id: str | None = None
    best_dist: float | None = None
    for rail in rails:
        for node_id, nx, ny in rail["endpoints"]:
            dist = (nx - x_um) ** 2 + (ny - y_um) ** 2
            if best_dist is None or dist < best_dist:
                best_dist = dist
                best_id = node_id
    return best_id


def _polygon_cut_coordinates(polygon: kdb.Polygon) -> tuple[list[int], list[int]]:
    """The sorted, deduplicated x/y coordinates of every vertex of a merged
    polygon (hull **and** holes) -- the cut lines of the rectangular grid
    :func:`_decompose_manhattan_polygon` carves it into."""
    xs: set[int] = set()
    ys: set[int] = set()
    for point in polygon.each_point_hull():
        xs.add(point.x)
        ys.add(point.y)
    for hole in range(polygon.holes()):
        for point in polygon.each_point_hole(hole):
            xs.add(point.x)
            ys.add(point.y)
    return sorted(xs), sorted(ys)


def _decomposition_grid(
    polygon: kdb.Polygon, max_cells: int
) -> tuple[list[int] | None, list[int] | None, str | None]:
    """``(xs, ys, None)`` for a polygon worth decomposing, or ``(None, None,
    reason)`` when it is degenerate or would need more mesh cells than
    ``max_cells``."""
    xs, ys = _polygon_cut_coordinates(polygon)
    if len(xs) < 2 or len(ys) < 2:
        return None, None, "it has no positive-area extent"
    cells = (len(xs) - 1) * (len(ys) - 1)
    if cells > max_cells:
        return (
            None,
            None,
            (
                f"decomposing it needs up to {cells} mesh cells, over this build's "
                f"{max_cells}-mesh-cell cap (MAX_DECOMPOSITION_CELLS)"
            ),
        )
    return xs, ys, None


def _decompose_manhattan_polygon(
    polygon: kdb.Polygon, max_cells: int
) -> tuple[list[tuple[int, int, kdb.Box]] | None, str | None]:
    """Cut one merged, axis-aligned polygon into the grid of rectangles
    formed by extending every vertex's x and y coordinate across it (issue
    #2171).

    Returns ``(cells, None)`` -- each cell a ``(column, row, box)`` triple
    keyed to that grid, so two cells sharing a boundary always share the
    *whole* side -- or ``(None, reason)`` when the polygon cannot be
    decomposed exactly (not Manhattan, degenerate, or over the cell cap).
    A caller must never fall back to the bounding box on a ``reason``: that
    is the non-conservative approximation this function exists to replace.
    """
    import klayout.db as kdb

    xs, ys, reason = _decomposition_grid(polygon, max_cells)
    if xs is None or ys is None:
        return None, reason
    columns = {x: index for index, x in enumerate(xs)}
    poly_region = kdb.Region(polygon)
    cells: list[tuple[int, int, kdb.Box]] = []
    for row in range(len(ys) - 1):
        band = kdb.Region(kdb.Box(xs[0], ys[row], xs[-1], ys[row + 1])) & poly_region
        for part in band.merged().each():
            if not part.is_box():
                return None, (
                    "it is not axis-aligned (Manhattan), so it has no exact "
                    "decomposition into rectangles"
                )
            box = part.bbox()
            left, right = columns.get(box.left), columns.get(box.right)
            if left is None or right is None:
                return None, "its rectangles do not land on its own vertex grid"
            for column in range(left, right):
                cells.append(
                    (
                        column,
                        row,
                        kdb.Box(xs[column], ys[row], xs[column + 1], ys[row + 1]),
                    )
                )
    if not cells:
        return None, "it has no positive-area extent"
    return cells, None


def _cell_sides(
    column: int,
    row: int,
    x0: float,
    y0: float,
    x1: float,
    y1: float,
    horizontal_major: bool,
) -> tuple[
    tuple[tuple[int, int], tuple[str, int, int], float, float, float, float, bool], ...
]:
    """The four sides of one mesh cell as ``(neighbour_key, port_key, port_x,
    port_y, half_length_um, cross_um, is_major_axis_end)`` tuples.

    ``port_key`` is deliberately shared by the two cells on either side of a
    boundary, so they meet at **one** node; ``half_length_um`` is the
    centre-to-side travel along the flow direction and ``cross_um`` the
    conducting width across it.
    """
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    half_w, half_h = (x1 - x0) / 2, (y1 - y0) / 2
    width_um, height_um = x1 - x0, y1 - y0
    return (
        (
            (column - 1, row),
            ("v", column, row),
            x0,
            cy,
            half_w,
            height_um,
            horizontal_major,
        ),
        (
            (column + 1, row),
            ("v", column + 1, row),
            x1,
            cy,
            half_w,
            height_um,
            horizontal_major,
        ),
        (
            (column, row - 1),
            ("h", column, row),
            cx,
            y0,
            half_h,
            width_um,
            not horizontal_major,
        ),
        (
            (column, row + 1),
            ("h", column, row + 1),
            cx,
            y1,
            half_h,
            width_um,
            not horizontal_major,
        ),
    )


def _model_box_polygon(
    box: kdb.Box,
    entry: dict[str, Any],
    dbu: float,
    add_node: Any,
    add_edge: Any,
) -> list[tuple[str, float, float]]:
    """A rectangular merged polygon: one edge between two endpoint nodes at
    the box's ends along its longer axis -- the model this module has always
    used, and the one every ``klt par`` standard-cell rail matches exactly."""
    x0, y0 = box.left * dbu, box.bottom * dbu
    x1, y1 = box.right * dbu, box.top * dbu
    width_um, height_um = x1 - x0, y1 - y0
    if width_um <= 0 or height_um <= 0:
        return []

    if width_um >= height_um:
        y_mid = (y0 + y1) / 2
        a_x, a_y, b_x, b_y = x0, y_mid, x1, y_mid
        length_um, cross_um = width_um, height_um
    else:
        x_mid = (x0 + x1) / 2
        a_x, a_y, b_x, b_y = x_mid, y0, x_mid, y1
        length_um, cross_um = height_um, width_um

    n_a = add_node(entry["name"], a_x, a_y)
    n_b = add_node(entry["name"], b_x, b_y)
    # A metal role's EM limit is per-width (like a real PDK's own
    # `DCCURRENTDENSITY`, mA/um at that layer's fixed thickness), so it
    # scales by this specific merged rail's own cross-width -- a narrow rail
    # has a lower absolute current limit than a wide one on the same layer,
    # exactly as electromigration physics says it should.
    current_limit_a = entry["current_limit_a_per_um"]
    if current_limit_a is not None:
        current_limit_a = current_limit_a * cross_um
    add_edge(
        "metal",
        entry["name"],
        n_a,
        n_b,
        entry["sheet_resistance_ohm_per_sq"] * length_um / cross_um,
        current_limit_a,
        entry["current_limit_source"],
    )
    return [(n_a, a_x, a_y), (n_b, b_x, b_y)]


def _model_mesh_polygon(
    cells: list[tuple[int, int, kdb.Box]],
    entry: dict[str, Any],
    dbu: float,
    add_node: Any,
    add_edge: Any,
) -> list[tuple[str, float, float]]:
    """A decomposed non-rectangular merged polygon: a finite-difference
    resistor mesh over its rectangles (issue #2171).

    Each cell gets a centre node; each boundary it shares with a neighbour
    gets a port node **shared** by both cells, and each free end of the
    cell's own longer axis gets a terminal port node so the arm's true
    extremity is still represented (that is where a pad or a load actually
    attaches). Every centre-to-port edge is ``sheet_resistance * (half the
    cell's extent along the flow direction) / (its conducting width across
    it)``, so an L's two arms contribute their own lengths at their own
    widths instead of one resistor sized by a bounding box far wider than
    either arm is drawn.
    """
    occupied = {(column, row) for column, row, _ in cells}
    ports: dict[tuple[str, int, int], str] = {}
    endpoints: list[tuple[str, float, float]] = []
    sheet_r = entry["sheet_resistance_ohm_per_sq"]
    limit_per_um = entry["current_limit_a_per_um"]

    for column, row, box in cells:
        x0, y0 = box.left * dbu, box.bottom * dbu
        x1, y1 = box.right * dbu, box.top * dbu
        if x1 - x0 <= 0 or y1 - y0 <= 0:
            continue
        centre = add_node(entry["name"], (x0 + x1) / 2, (y0 + y1) / 2)
        endpoints.append((centre, (x0 + x1) / 2, (y0 + y1) / 2))
        sides = _cell_sides(column, row, x0, y0, x1, y1, (x1 - x0) >= (y1 - y0))
        for neighbour, port_key, px, py, half_um, cross_um, is_end in sides:
            if neighbour in occupied:
                port = ports.get(port_key)
                if port is None:
                    port = add_node(entry["name"], px, py)
                    ports[port_key] = port
                    endpoints.append((port, px, py))
            elif is_end:
                port = add_node(entry["name"], px, py)
                endpoints.append((port, px, py))
            else:
                continue
            add_edge(
                "metal",
                entry["name"],
                centre,
                port,
                sheet_r * half_um / cross_um,
                None if limit_per_um is None else limit_per_um * cross_um,
                entry["current_limit_source"],
            )
    return endpoints


def _build_island_network(
    l2n: kdb.LayoutToNetlist,
    net: kdb.Net,
    stackup: list[dict[str, Any]],
    vias: list[dict[str, Any]],
    layer_index: dict[str, int],
    via_layer_index: dict[str, int],
    dbu: float,
    net_name: str,
    island_id: str,
    warnings: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    """Build one electrically-connected island's node/edge lists -- see this
    module's docstring for the resistor-network model.

    Returns ``(nodes, edges, unsolved_reason)``. ``nodes``/``edges`` are both
    empty when ``net`` has no geometry on any declared ``stackup`` layer (the
    caller decides whether that is worth a warning). ``unsolved_reason`` is a
    string -- with empty ``nodes``/``edges`` -- when some segment's geometry
    cannot be modelled exactly (issue #2171): the island is declared
    unsolved rather than approximated in the non-conservative direction."""
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    rails_by_layer: dict[str, list[dict[str, Any]]] = {
        entry["name"]: [] for entry in stackup
    }
    counters = {"node": 0, "edge": 0}

    def add_node(layer_name: str, x_um: float, y_um: float) -> str:
        node_id = f"n{counters['node']}"
        counters["node"] += 1
        nodes.append(
            {
                "id": node_id,
                "layer": layer_name,
                "x_um": round(x_um, 6),
                "y_um": round(y_um, 6),
            }
        )
        return node_id

    def add_edge(
        kind: str,
        layer_name: str,
        frm: str,
        to: str,
        resistance_ohm: float,
        current_limit_a: float | None = None,
        current_limit_source: str | None = None,
    ) -> None:
        edge_id = f"e{counters['edge']}"
        counters["edge"] += 1
        edges.append(
            {
                "id": edge_id,
                "kind": kind,
                "layer": layer_name,
                "from": frm,
                "to": to,
                "resistance_ohm": round(resistance_ohm, 9),
                # Issue #846, Phase 1c: this edge's own EM current-density
                # limit, `null` when its `stackup`/`vias` role declared none
                # -- see `_validate_stackup`/`_validate_vias` above and
                # `docs/cli/power.md`'s "EM current-density verdict" section.
                "current_limit_a": (
                    round(current_limit_a, 12) if current_limit_a is not None else None
                ),
                "current_limit_source": current_limit_source,
            }
        )

    decomposed = {"segments": 0, "cells": 0}
    for entry in stackup:
        region = l2n.polygons_of_net(net, layer_index[entry["name"]]).merged()
        for polygon in region.each():
            if polygon.is_box():
                endpoints = _model_box_polygon(
                    polygon.bbox(), entry, dbu, add_node, add_edge
                )
            else:
                cells, reason = _decompose_manhattan_polygon(
                    polygon, MAX_DECOMPOSITION_CELLS
                )
                if cells is None:
                    return (
                        [],
                        [],
                        f"a non-rectangular {entry['name']} segment could not be "
                        f"decomposed into rectangles -- {reason}",
                    )
                decomposed["segments"] += 1
                decomposed["cells"] += len(cells)
                endpoints = _model_mesh_polygon(cells, entry, dbu, add_node, add_edge)
            if endpoints:
                rails_by_layer[entry["name"]].append({"endpoints": endpoints})

    if decomposed["segments"]:
        warnings.append(
            f"power net {net_name!r} island {island_id!r}: "
            f"{decomposed['segments']} non-rectangular segment(s) decomposed "
            f"into {decomposed['cells']} rectangular sub-segment(s) -- "
            "resistance is modelled per sub-segment, not from a bounding box"
        )

    for via in vias:
        region = l2n.polygons_of_net(net, via_layer_index[via["name"]]).merged()
        for polygon in region.each():
            box = polygon.bbox()
            cx = (box.left + box.right) / 2 * dbu
            cy = (box.bottom + box.top) / 2 * dbu
            layer_a, layer_b = via["between"]
            node_a = _nearest_endpoint(rails_by_layer[layer_a], cx, cy)
            node_b = _nearest_endpoint(rails_by_layer[layer_b], cx, cy)
            if node_a is None or node_b is None:
                missing = layer_a if node_a is None else layer_b
                warnings.append(
                    f"power net {net_name!r} island {island_id!r}: a "
                    f"{via['name']!r} via at ({cx:.3f}, {cy:.3f}) um has no "
                    f"matching {missing!r} rail on this net -- skipped"
                )
                continue
            add_edge(
                "via",
                via["name"],
                node_a,
                node_b,
                via["resistance_ohm"],
                via["current_limit_a"],
                via["current_limit_source"],
            )

    return nodes, edges, None


def _nearest_node(
    net_nodes: list[tuple[int, dict[str, Any]]], x_um: float, y_um: float
) -> tuple[int, dict[str, Any]] | None:
    """``(island_index, node)`` of the node nearest ``(x_um, y_um)`` among
    one net's whole extracted geometry, or ``None`` when the net has no nodes
    at all. Ties resolve to the first node in extraction order, so a given
    layout+spec always attaches a pad/instance to the same node.

    This is the same "snap to the nearest existing node" approximation
    ``_nearest_endpoint`` already uses for via taps (see this module's
    docstring and ``docs/cli/power.md``'s "Scope and limitations"): a pad or
    an instance is wired to the closest *extracted* node, not spliced into a
    rail at its exact position."""
    best: tuple[int, dict[str, Any]] | None = None
    best_dist: float | None = None
    for island_index, node in net_nodes:
        dist = (node["x_um"] - x_um) ** 2 + (node["y_um"] - y_um) ** 2
        if best_dist is None or dist < best_dist:
            best_dist = dist
            best = (island_index, node)
    return best


def _solve_ir_drop(
    networks: list[dict[str, Any]],
    pads: list[dict[str, Any]],
    instances: list[dict[str, Any]] | None,
    warnings: list[str],
) -> dict[str, Any]:
    """Solve every extracted island for its DC operating point and build the
    ``ir_drop_map`` response block. See ``docs/cli/power.md`` for the shape
    and :mod:`klayout_tools.ir_solver` for the numerics."""
    by_net = {entry["net"]: entry for entry in networks}
    nodes_by_net: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for net_name, entry in by_net.items():
        nodes_by_net[net_name] = [
            (island_index, node)
            for island_index, island in enumerate(entry["islands"])
            for node in island["nodes"]
        ]

    # (net, island_index) -> {node_id: value}
    pad_voltages: dict[tuple[str, int], dict[str, float]] = {}
    injections: dict[tuple[str, int], dict[str, float]] = {}
    pad_counts: dict[tuple[str, int], int] = {}
    instance_counts: dict[tuple[str, int], int] = {}
    pad_reports: list[dict[str, Any]] = []

    def _attach(
        net_name: str, x_um: float, y_um: float
    ) -> tuple[int, dict[str, Any]] | None:
        return _nearest_node(nodes_by_net.get(net_name, []), x_um, y_um)

    for pad in pads:
        target = _attach(pad["net"], pad["x_um"], pad["y_um"])
        report = dict(pad)
        if target is None:
            report["island_id"] = None
            report["node_id"] = None
            pad_reports.append(report)
            warnings.append(
                f"pad {pad['name']!r} on net {pad['net']!r} has no extracted "
                "geometry on that net to attach to -- skipped"
            )
            continue
        island_index, node = target
        key = (pad["net"], island_index)
        island_id = by_net[pad["net"]]["islands"][island_index]["island_id"]
        report["island_id"] = island_id
        report["node_id"] = node["id"]
        pad_reports.append(report)
        existing = pad_voltages.setdefault(key, {})
        existing[node["id"]] = pad["voltage_v"]
        pad_counts[key] = pad_counts.get(key, 0) + 1

    def _inject(net_name: str, instance: dict[str, Any], current_a: float) -> None:
        target = _attach(net_name, instance["x_um"], instance["y_um"])
        if target is None:
            warnings.append(
                f"current_model instance {instance['name']!r} has no extracted "
                f"{net_name!r} geometry to attach to -- its {abs(current_a) * 1e3:g} "
                "mA is not modelled on that net"
            )
            return
        island_index, node = target
        key = (net_name, island_index)
        bucket = injections.setdefault(key, {})
        bucket[node["id"]] = bucket.get(node["id"], 0.0) + current_a
        instance_counts[key] = instance_counts.get(key, 0) + 1

    for instance in instances or []:
        # An instance *draws* current off its supply net (a negative
        # injection) and *returns* the same current to its ground net (a
        # positive injection) -- the sign convention `ir_solver` documents.
        _inject(instance["supply_net"], instance, -instance["current_a"])
        if instance["ground_net"] is not None:
            _inject(instance["ground_net"], instance, instance["current_a"])

    net_reports: list[dict[str, Any]] = []
    overall_worst: dict[str, Any] | None = None
    overall_worst_droop = 0.0
    unsolved_current_a = 0.0
    solved_node_total = 0
    unsolved_node_total = 0
    quiet_no_pad_islands: list[str] = []

    for entry in networks:
        net_name = entry["net"]
        net_worst: dict[str, Any] | None = None
        net_worst_droop = 0.0
        island_reports: list[dict[str, Any]] = []
        net_current = 0.0
        solved_islands = 0

        for island_index, island in enumerate(entry["islands"]):
            key = (net_name, island_index)
            island_pads = pad_voltages.get(key, {})
            island_injections = injections.get(key, {})
            net_current += sum(abs(v) for v in island_injections.values())

            solution = solve_ir_drop(
                [node["id"] for node in island["nodes"]],
                island["edges"],
                pads=island_pads,
                injections=island_injections,
            )

            solved_components = [c for c in solution["components"] if c["solved"]]
            unsolved_components = [c for c in solution["components"] if not c["solved"]]
            solved_nodes = sum(c["node_count"] for c in solved_components)
            unsolved_nodes = sum(c["node_count"] for c in unsolved_components)
            solved_node_total += solved_nodes
            unsolved_node_total += unsolved_nodes

            island_worst_id, island_worst_droop = worst_deviation(
                solution["deviations"], [node["id"] for node in island["nodes"]]
            )

            reason = unsolved_components[0]["reason"] if unsolved_components else None
            if unsolved_components:
                stranded = sum(
                    abs(c["injected_current_a"]) for c in unsolved_components
                )
                unsolved_current_a += stranded
                if stranded or reason != "no_pad":
                    # Worth naming individually: either the current model
                    # puts real current somewhere with no return path to a
                    # pad, or the solver itself did not converge.
                    warnings.append(
                        f"island {island['island_id']!r}: {unsolved_nodes} of "
                        f"{len(island['nodes'])} node(s) not solved ({reason})"
                        + (
                            f" -- {stranded * 1e3:g} mA of the current model "
                            "lands there with no pad to source it"
                            if stranded
                            else ""
                        )
                    )
                else:
                    quiet_no_pad_islands.append(island["island_id"])

            island_report = {
                "island_id": island["island_id"],
                "solved": bool(solved_components),
                "unsolved_reason": reason,
                "pad_count": pad_counts.get(key, 0),
                "instance_count": instance_counts.get(key, 0),
                "current_a": round(
                    float(sum(abs(v) for v in island_injections.values())), 12
                ),
                "reference_voltage_v": (
                    solved_components[0]["reference_voltage_v"]
                    if solved_components
                    else None
                ),
                "pad_current_a": (
                    round(float(sum(c["pad_current_a"] for c in solved_components)), 12)
                    if solved_components
                    else None
                ),
                "solved_node_count": solved_nodes,
                "unsolved_node_count": unsolved_nodes,
                "worst_case_droop_mv": (
                    round(island_worst_droop * 1e3, 9)
                    if island_worst_id is not None
                    else None
                ),
                "worst_case_node_id": island_worst_id,
                "iterations": max(
                    (c["iterations"] for c in solution["components"]), default=0
                ),
                "residual": max(
                    (c["residual"] for c in solution["components"]), default=0.0
                ),
                "nodes": [
                    {
                        "id": node["id"],
                        "voltage_v": _round_or_none(
                            solution["voltages"][node["id"]], 9
                        ),
                        "droop_mv": _round_or_none(
                            _abs_or_none(solution["deviations"][node["id"]]), 9, 1e3
                        ),
                        # What the current model + pad snapping actually
                        # attached here (signed: negative = drawn off this
                        # net). Reported so a reader can reconstruct the exact
                        # network that was solved -- see
                        # tests/test_power_ir_cross_check.py, which rebuilds
                        # it for ngspice from this field.
                        "injected_current_a": round(
                            float(island_injections.get(node["id"], 0.0)), 12
                        ),
                        "pad_voltage_v": island_pads.get(node["id"]),
                    }
                    for node in island["nodes"]
                ],
                "edges": [
                    {
                        "id": edge["id"],
                        "current_a": _round_or_none(
                            solution["edge_currents"][edge["id"]], 12
                        ),
                    }
                    for edge in island["edges"]
                ],
            }
            island_reports.append(island_report)
            if solved_components:
                solved_islands += 1

            if island_worst_id is not None and (
                net_worst is None or island_worst_droop > net_worst_droop
            ):
                node = next(n for n in island["nodes"] if n["id"] == island_worst_id)
                net_worst_droop = island_worst_droop
                net_worst = {
                    "net": net_name,
                    "island_id": island["island_id"],
                    "node_id": island_worst_id,
                    "layer": node["layer"],
                    "x_um": node["x_um"],
                    "y_um": node["y_um"],
                    "voltage_v": _round_or_none(
                        solution["voltages"][island_worst_id], 9
                    ),
                    "droop_mv": round(island_worst_droop * 1e3, 9),
                }

        net_reports.append(
            {
                "net": net_name,
                "pad_count": sum(1 for pad in pads if pad["net"] == net_name),
                "instance_count": sum(
                    instance_counts.get((net_name, i), 0)
                    for i in range(len(entry["islands"]))
                ),
                "current_a": round(float(net_current), 12),
                "solved_island_count": solved_islands,
                "unsolved_island_count": len(entry["islands"]) - solved_islands,
                "worst_case_droop_mv": (
                    round(net_worst_droop * 1e3, 9) if net_worst else None
                ),
                "worst_case_node": net_worst,
                "islands": island_reports,
            }
        )

        if net_worst and (
            overall_worst is None or net_worst_droop > overall_worst_droop
        ):
            overall_worst_droop = net_worst_droop
            overall_worst = net_worst

    if quiet_no_pad_islands:
        warnings.append(
            f"{len(quiet_no_pad_islands)} island(s) have no pad and no modelled "
            "current, so they have no DC operating point and were not solved "
            f"(first: {quiet_no_pad_islands[0]!r}) -- see "
            "ir_drop_map.nets[].islands[].unsolved_reason for the full list"
        )

    return {
        "pads": pad_reports,
        "instance_count": len(instances or []),
        "total_current_a": round(
            float(sum(instance["current_a"] for instance in instances or [])), 12
        ),
        "unsolved_current_a": round(float(unsolved_current_a), 12),
        "solved_node_count": solved_node_total,
        "unsolved_node_count": unsolved_node_total,
        "worst_case": overall_worst,
        "nets": net_reports,
    }


def _abs_or_none(value: float | None) -> float | None:
    return None if value is None else abs(value)


def _round_or_none(
    value: float | None, digits: int, scale: float = 1.0
) -> float | None:
    return None if value is None else round(value * scale, digits)


def _em_coverage(
    networks: list[dict[str, Any]], ir_drop_map: dict[str, Any] | None
) -> dict[str, Any]:
    """Track every extracted edge against its own solved current and limit."""
    currents = {
        (net["net"], island["island_id"], edge["id"]): edge["current_a"]
        for net in (ir_drop_map or {}).get("nets", [])
        for island in net["islands"]
        for edge in island["edges"]
    }
    checked = []
    skipped = []
    inapplicable = []
    for net in networks:
        for island in net["islands"]:
            for edge in island["edges"]:
                key = (net["net"], island["island_id"], edge["id"])
                identity = work_id("em", *key)
                if ir_drop_map is None:
                    inapplicable.append(
                        {"id": identity, "reason": "no_current_solve_requested"}
                    )
                elif edge["current_limit_a"] is None:
                    skipped.append({"id": identity, "reason": "missing_current_limit"})
                elif currents.get(key) is None:
                    skipped.append(
                        {"id": identity, "reason": "unavailable_branch_current"}
                    )
                else:
                    checked.append(identity)
    return {
        "scope": "electromigration",
        **build_check_coverage(
            checked=checked, skipped=skipped, inapplicable=inapplicable
        ),
    }


def _compute_em_verdict(
    networks: list[dict[str, Any]],
    ir_drop_map: dict[str, Any] | None,
    coverage: dict[str, Any],
) -> dict[str, Any] | None:
    """The per-net EM (electromigration) current-density verdict (issue
    #846, Phase 1c): compare every solved edge's branch current
    (``ir_drop_map``'s per-segment currents, Phase 1b) against that edge's
    own ``current_limit_a`` (this module's Phase 1a extraction, scaled from
    the spec's per-layer ``current_limit_a_per_um``/``current_limit_a`` --
    see ``_build_island_network`` above), citing the exact limit and its
    ``current_limit_source`` for every edge it checks.

    ``None`` when there is nothing to check: either no IR-drop solve ran at
    all (no ``pads``/``current_model`` in the spec -- there are no branch
    currents to compare), matching how ``ir_drop_map`` itself is ``None``
    in that case. An edge with no declared ``current_limit_a`` (the spec's
    stackup/vias role set none) or no solved ``current_a`` (an unsolved
    island, or a zero-resistance short whose own current is not
    recoverable -- see ``ir_solver.py``) is counted in
    ``unchecked_edge_count``, never guessed at, the same "report, never
    guess" discipline ``_solve_ir_drop`` above already applies to droop.

    A net's own ``status`` is ``"fail"`` if any of its checked edges
    exceeds its limit, ``"pass"`` if at least one edge was checked and none
    failed, or ``"not_checked"`` if the net had no edge with both a
    declared limit and a solved current (e.g. every edge on that net's
    stackup/via roles declared no ``current_limit_a_per_um``/
    ``current_limit_a`` at all). Per-net status is deliberately unaffected
    by this function's *overall* rollup below -- a net's own coverage is
    already fully expressed by its own ``checked_edge_count``/
    ``unchecked_edge_count`` pair, and #2109's common table is a
    whole-design decision, not a per-net one.

    The *overall* ``status`` (issue #1997, migrated onto the common
    ``coverage_rollup()``/``rollup_status()`` decision table by issue #2116)
    is derived from ``coverage`` -- the same versioned ``checked``/
    ``skipped``/``inapplicable`` edge identities :func:`_em_coverage` already
    tracks -- plus this function's own ``fail_count``, exactly the same rule
    every other coverage-adopting `klt` verb applies (``docs/coverage-
    contract.md``'s migration contract): a checked edge over its limit is
    ``"fail"`` regardless of coverage; with no failure, a nonempty edge skip
    list is ``"pass_partial"`` (never an unconditional ``"pass"``); known
    zero checked edges is ``"not_checked"``; and only a design where every
    edge that exists was actually compared against a limit is plain
    ``"pass"``.
    """
    if ir_drop_map is None:
        return None

    # (net, island_id, edge_id) -> the base extraction edge, which carries
    # `kind`/`layer`/`current_limit_a`/`current_limit_source` -- everything
    # `ir_drop_map`'s own (deliberately slimmer) per-edge `current_a` does
    # not repeat.
    base_edges: dict[tuple[str, str, str], dict[str, Any]] = {}
    for net_entry in networks:
        for island in net_entry["islands"]:
            for edge in island["edges"]:
                base_edges[(net_entry["net"], island["island_id"], edge["id"])] = edge

    def _edge_verdict(
        net_name: str, island_id: str, edge: dict[str, Any]
    ) -> dict[str, Any] | None:
        """A single edge's checked verdict, or ``None`` when it cannot be
        checked (no declared limit, or no solved current)."""
        base = base_edges.get((net_name, island_id, edge["id"]))
        if base is None:
            return None
        limit = base["current_limit_a"]
        current = edge["current_a"]
        if limit is None or current is None:
            return None
        current_abs = abs(current)
        margin_a = limit - current_abs
        return {
            "net": net_name,
            "island_id": island_id,
            "edge_id": edge["id"],
            "kind": base["kind"],
            "layer": base["layer"],
            "current_a": round(current_abs, 12),
            "current_limit_a": round(limit, 12),
            "current_limit_source": base["current_limit_source"],
            "margin_a": round(margin_a, 12),
            "status": "fail" if current_abs > limit else "pass",
        }

    net_reports: list[dict[str, Any]] = []
    overall_worst: dict[str, Any] | None = None
    overall_checked = 0
    overall_unchecked = 0
    overall_fail = 0

    for net_entry in ir_drop_map["nets"]:
        net_name = net_entry["net"]
        checked = 0
        unchecked = 0
        fail_count = 0
        failing_edges: list[dict[str, Any]] = []
        net_worst: dict[str, Any] | None = None

        for island in net_entry["islands"]:
            for edge in island["edges"]:
                verdict = _edge_verdict(net_name, island["island_id"], edge)
                if verdict is None:
                    unchecked += 1
                    continue
                checked += 1
                if verdict["status"] == "fail":
                    fail_count += 1
                    failing_edges.append(verdict)
                # The "worst case" is whichever checked edge sits closest
                # to (or furthest past) its own limit -- the smallest
                # margin, which is negative for a failing edge -- the same
                # "largest deviation wins" idea `_solve_ir_drop` already
                # uses for `worst_case_droop_mv`, just signed the other way
                # (margin shrinking is worse, not growing).
                if net_worst is None or verdict["margin_a"] < net_worst["margin_a"]:
                    net_worst = verdict

        overall_checked += checked
        overall_unchecked += unchecked
        overall_fail += fail_count

        if checked == 0:
            status = "not_checked"
        elif fail_count:
            status = "fail"
        else:
            status = "pass"

        net_reports.append(
            {
                "net": net_name,
                "status": status,
                "checked_edge_count": checked,
                "unchecked_edge_count": unchecked,
                "fail_count": fail_count,
                "worst_case": net_worst,
                "failing_edges": failing_edges,
            }
        )

        if net_worst is not None and (
            overall_worst is None or net_worst["margin_a"] < overall_worst["margin_a"]
        ):
            overall_worst = net_worst

    # Overall rollup `status` (issue #1997, migrated onto the common
    # decision table by issue #2116): `coverage` already tracks every
    # extracted edge's `checked`/`skipped`/`inapplicable` identity
    # (`_em_coverage`, one-for-one with `overall_checked`/`overall_unchecked`
    # above), so `coverage_rollup()` reads the same eight-row table every
    # other coverage-adopting `klt` verb applies rather than re-deriving an
    # equivalent rule locally -- see `docs/coverage-contract.md`'s migration
    # contract. `failed=` is this function's own executed-check outcome
    # (`overall_fail > 0`, decided *before* coverage is consulted, so a real
    # EM violation is never masked by a coverage gap): `"fail"`/exit 3.
    # With no failure: known zero checked edges is `"not_checked"`/exit 4;
    # a nonempty edge skip list alongside successful checks is
    # `"pass_partial"`/exit 0 -- never an unconditional `"pass"` (the
    # operator's #1988 rule); and only a design where every edge that
    # exists was actually compared against a limit reaches plain `"pass"`.
    status = rollup_status(
        coverage_rollup({"coverage": coverage}, failed=bool(overall_fail)),
        success="pass",
    )

    return {
        "status": status,
        "checked_edge_count": overall_checked,
        "unchecked_edge_count": overall_unchecked,
        "fail_count": overall_fail,
        "worst_case": overall_worst,
        "nets": net_reports,
    }


def _em_overall_status(
    em_verdict: dict[str, Any] | None, coverage: dict[str, Any]
) -> str:
    """The report's top-level ``status`` (issue #2116): ``em_verdict``'s own
    common-rollup-derived status when a solve ran at all, or the same
    rollup applied directly to ``coverage`` when it did not.

    No solve at all (no ``pads``/``current_model``) leaves ``em_verdict``
    ``None`` -- every extracted edge's EM check is `inapplicable` (see
    ``_em_coverage``), which the common table classifies as known zero
    checked work (``coverage_rollup()``'s ``RESULT_ZERO`` row), reporting
    the fixed ``"not_checked"`` token -- matching the standalone
    ``"not_checked"`` this branch always reported before the shared rollup
    existed.
    """
    if em_verdict is not None:
        return em_verdict["status"]
    return rollup_status(coverage_rollup({"coverage": coverage}), success="pass")


def _net_labels(expanded_name: str) -> list[str]:
    """The individual labels behind one KLayout net name.

    ``LayoutToNetlist`` names a net after **every** text label attached to
    it, comma-joined -- a chip-level ``VDD_CORE`` bus landing on a pad
    cell's own ``DVDD`` plate is named ``"DVDD,VDD_CORE"`` (issue #2171).
    """
    return [label.strip() for label in expanded_name.split(",") if label.strip()]


def _matched_nets(circuit: Any, entry: dict[str, str]) -> list[Any]:
    """Every extracted net one ``power_nets`` entry selects, ordered by
    cluster id so a given layout+spec always numbers islands the same way.

    ``match: "exact"`` compares the whole comma-joined ``expanded_name()``;
    the default ``match: "label"`` *additionally* matches any net carrying
    the requested name as one of its labels, which is what makes a spec
    written against design net names work on a pad-connected PDN.
    """
    name = entry["name"]
    by_label = entry["match"] == "label"
    matched = []
    for candidate in circuit.each_net():
        if candidate.cluster_id == 0:
            continue
        expanded = candidate.expanded_name()
        if expanded == name or (by_label and name in _net_labels(expanded)):
            matched.append(candidate)
    return sorted(matched, key=lambda candidate: candidate.cluster_id)


def _describe_observed_nets(circuit: Any, limit: int = 20) -> str:
    """The net names this layout actually carries, for a zero-match
    diagnostic (issue #2171: the old message pointed at the layer/datatype
    numbers even when the layers were right and only the *name* was off)."""
    observed = sorted(
        {
            candidate.expanded_name()
            for candidate in circuit.each_net()
            if candidate.cluster_id != 0 and candidate.name
        }
    )
    if not observed:
        return (
            "no net in this layout carries any label at all on the declared "
            "'stackup' 'label_layer' text layers"
        )
    shown = ", ".join(repr(name) for name in observed[:limit])
    if len(observed) > limit:
        shown += f", ... (+{len(observed) - limit} more)"
    return f"nets actually present: {shown}"


def _build_net_islands(
    l2n: kdb.LayoutToNetlist,
    matched: list[Any],
    stackup: list[dict[str, Any]],
    vias: list[dict[str, Any]],
    layer_index: dict[str, int],
    via_layer_index: dict[str, int],
    dbu: float,
    net_name: str,
    warnings: list[str],
) -> list[dict[str, Any]]:
    """One ``networks[].islands`` list: every matched net cluster turned
    into its own node/edge network, or reported with an
    ``unsolved_reason`` when its geometry cannot be modelled exactly (issue
    #2171 -- never silently approximated in the non-conservative
    direction)."""
    islands: list[dict[str, Any]] = []
    for island_index, net in enumerate(matched):
        island_id = f"{net_name}#{island_index}"
        nodes, edges, unsolved_reason = _build_island_network(
            l2n,
            net,
            stackup,
            vias,
            layer_index,
            via_layer_index,
            dbu,
            net_name,
            island_id,
            warnings,
        )
        if unsolved_reason is not None:
            warnings.append(
                f"power net {net_name!r} island {island_id!r} is unsolved: "
                f"{unsolved_reason} -- no resistor model was emitted for it, "
                "rather than approximating it by a bounding box (which would "
                "understate resistance and overstate EM headroom)"
            )
            islands.append(
                {
                    "island_id": island_id,
                    "node_count": 0,
                    "edge_count": 0,
                    "unsolved_reason": unsolved_reason,
                    "nodes": [],
                    "edges": [],
                }
            )
            continue
        if not nodes:
            warnings.append(
                f"power net {net_name!r} island {island_id!r} (net id "
                f"{net.cluster_id}) has a matching label but no geometry on "
                "the declared 'stackup' layers -- skipped"
            )
            continue
        islands.append(
            {
                "island_id": island_id,
                "node_count": len(nodes),
                "edge_count": len(edges),
                "unsolved_reason": None,
                "nodes": nodes,
                "edges": edges,
            }
        )
    return islands


def run_power(
    file: str,
    spec_path: str,
    *,
    top: str | None = None,
) -> dict[str, Any]:
    """Run ``klt power``'s resistive-network extraction (and, when the spec
    asks for one, the static IR-drop solve) end to end.

    ``file`` is a routed GDSII/OASIS layout (e.g. a ``klt par`` output);
    ``spec_path`` is a JSON file with:

    - ``power_nets`` (required, non-empty array): net names to extract, e.g.
      ``["VPWR", "VGND"]``. An entry is either a bare string -- matched
      against the labels the layout's own nets carry, so ``"VDD_CORE"``
      still matches a pad-connected net KLayout named ``"DVDD,VDD_CORE"``
      (issue #2171) -- or ``{"name": ..., "match": "label"|"exact"}``,
      where ``"exact"`` restricts matching to the whole comma-joined name.
    - ``stackup`` (required, non-empty array): each entry declares one
      metal role -- ``{"name", "layer": "<layer>/<datatype>",
      "label_layer": "<layer>/<datatype>" (optional),
      "sheet_resistance_ohm_per_sq"}``. At least one entry must set
      ``label_layer`` (the routed layout's own pin/net-name text on that
      layer), or no net can ever be matched by name.
    - ``vias`` (optional array, default ``[]``): each entry bridges two
      ``stackup`` names -- ``{"name" (optional, defaults to "via<index>"),
      "layer": "<layer>/<datatype>", "between": ["<metal>", "<metal>"],
      "resistance_ohm"}``.
    - ``pads`` (optional array, default ``[]``): where each net's supply is
      delivered -- ``{"name" (optional), "net", "x_um", "y_um",
      "voltage_v"}``.
    - ``current_model`` (optional object): what each instance draws --
      ``{"supply_net"/"ground_net" (optional per-model defaults), "vdd_v"
      (optional per-model default supply voltage for ``activity`` below),
      "instances": [{"name" (optional), "x_um", "y_um", "current_a" *or*
      "activity": {"toggle_rate_hz", "capacitance_f", "vdd_v" (optional,
      falls back to the model-level default)} -- exactly one of
      "current_a"/"activity" per instance (issue #1320's activity-weighted
      current mode, additive to the static "current_a"),
      "supply_net"/"ground_net" (optional per-instance overrides)}]}``.

    ``top`` selects the top cell to analyse when the stream has more than
    one (required in that case, matching ``select_top_cells``'s convention
    -- see ``docs/cli/layers.md``'s ``--top``).

    Returns a dict matching the documented ``klt power`` JSON schema (see
    ``docs/cli/power.md``), including ``schema_version``. ``ir_drop_map`` and
    ``worst_case_droop_mv`` are ``None`` when the spec declares neither
    ``pads`` nor a ``current_model`` (extraction only -- there is nothing to
    solve). Raises :class:`PowerError` for a malformed spec, an unresolvable
    layout/top cell, or a request whose declared power nets match no geometry
    at all (every requested net individually unmatched is reported in
    ``warnings`` rather than raised, as long as at least one net resolved
    to at least one island).
    """
    spec = _load_spec_json(spec_path, PowerError)
    power_net_entries = _validate_power_nets(spec, spec_path)
    power_nets = [entry["name"] for entry in power_net_entries]
    stackup = _validate_stackup(spec, spec_path)
    stackup_names = [entry["name"] for entry in stackup]
    vias = _validate_vias(spec, spec_path, stackup_names)
    pads = _validate_pads(spec, spec_path, power_nets)
    instances = _validate_current_model(spec, spec_path, power_nets)

    layout = load_layout(file, PowerError)
    top_cells = select_top_cells(layout, top, PowerError)
    if len(top_cells) != 1:
        raise PowerError(
            f"klt power needs exactly one top cell to analyse ({len(top_cells)} "
            f"found in '{file}'); pass --top to select one"
        )
    top_cell = top_cells[0]
    dbu = layout.dbu

    # Imported lazily, matching `load_layout`'s own lazy `klayout.db` import.
    import klayout.db as kdb

    l2n = kdb.LayoutToNetlist(top_cell.name, dbu)

    layer_index: dict[str, int] = {}
    metal_regions: dict[str, Any] = {}
    for entry in stackup:
        metal_region = _region(layout, top_cell, entry["layer"])
        metal_regions[entry["name"]] = metal_region
        layer_index[entry["name"]] = l2n.register(metal_region, entry["name"])
        l2n.connect(metal_region)
        if entry["label_layer"] is not None:
            label_texts = _texts(layout, top_cell, entry["label_layer"])
            l2n.register(label_texts, f"{entry['name']}_label")
            l2n.connect(metal_region, label_texts)

    via_layer_index: dict[str, int] = {}
    for via in vias:
        via_region = _region(layout, top_cell, via["layer"])
        via_layer_index[via["name"]] = l2n.register(via_region, via["name"])
        l2n.connect(via_region)
        layer_a, layer_b = via["between"]
        l2n.connect(metal_regions[layer_a], via_region)
        l2n.connect(via_region, metal_regions[layer_b])

    try:
        l2n.extract_netlist()
    except Exception as exc:  # KLayout raises a bare RuntimeError on internal failure
        raise PowerError(f"connectivity extraction failed: {exc}") from exc

    netlist = l2n.netlist()
    circuit = netlist.circuit_by_name(top_cell.name)
    if circuit is None:
        raise PowerError(
            f"no circuit named '{top_cell.name}' in the extracted connectivity graph"
        )

    warnings: list[str] = []
    networks: list[dict[str, Any]] = []
    total_islands = 0

    for net_entry in power_net_entries:
        net_name = net_entry["name"]
        matched = _matched_nets(circuit, net_entry)
        if not matched:
            warnings.append(
                f"power net {net_name!r} matches no labelled net in this layout -- "
                f"{_describe_observed_nets(circuit)}; check 'power_nets' against "
                "the layout's own pin/label text (a 'power_nets' entry matches a "
                "net that *carries* that label, so 'VDD_CORE' matches a net named "
                "'DVDD,VDD_CORE'), and that at least one 'stackup' entry's "
                "'label_layer' actually carries it"
            )
            networks.append(
                {
                    "net": net_name,
                    "island_count": 0,
                    "node_count": 0,
                    "edge_count": 0,
                    "islands": [],
                }
            )
            continue

        islands = _build_net_islands(
            l2n,
            matched,
            stackup,
            vias,
            layer_index,
            via_layer_index,
            dbu,
            net_name,
            warnings,
        )
        total_islands += len(islands)
        networks.append(
            {
                "net": net_name,
                "island_count": len(islands),
                "node_count": sum(island["node_count"] for island in islands),
                "edge_count": sum(island["edge_count"] for island in islands),
                "islands": islands,
            }
        )

    if total_islands == 0:
        raise PowerError(
            "none of the requested 'power_nets' matched any geometry in "
            f"'{file}' against this spec's 'stackup' -- "
            f"{_describe_observed_nets(circuit)}; see the per-net explanation "
            "this run would have reported in 'warnings', and check the "
            "layer/datatype numbers against the layout's own resolved PDK "
            "layer map"
        )

    ir_drop_map = None
    worst_case_droop_mv = None
    if pads or instances is not None:
        ir_drop_map = _solve_ir_drop(networks, pads, instances, warnings)
        worst_case = ir_drop_map["worst_case"]
        worst_case_droop_mv = worst_case["droop_mv"] if worst_case else None

    # Issues #2108/#2109/#2116: the versioned checked-work coverage block,
    # built ahead of `em_verdict` because its edge-level classification
    # (`checked`/`skipped`/`inapplicable`) is what the common
    # `coverage_rollup()` table below reads to decide *both* `em_verdict`'s
    # own overall status and, when there was no solve at all, the top-level
    # `status` fallback -- see `_em_coverage`'s docstring.
    coverage = _em_coverage(networks, ir_drop_map)

    # Issue #846, Phase 1c: the per-net EM current-density verdict, built
    # from this same `ir_drop_map`'s per-segment currents (`None` when there
    # was no solve at all -- see `_compute_em_verdict`'s own docstring).
    em_verdict = _compute_em_verdict(networks, ir_drop_map, coverage)
    if em_verdict and em_verdict["fail_count"]:
        warnings.append(
            f"{em_verdict['fail_count']} of {em_verdict['checked_edge_count']} "
            "EM-checked edge(s) exceed their declared current-density limit "
            "-- see em_verdict for the cited limit each failed against"
        )

    status = _em_overall_status(em_verdict, coverage)

    return {
        "schema_version": SCHEMA_VERSION,
        "file": file,
        "spec": spec_path,
        "power_nets": power_nets,
        "networks": networks,
        "node_count": sum(net_entry["node_count"] for net_entry in networks),
        "edge_count": sum(net_entry["edge_count"] for net_entry in networks),
        "island_count": total_islands,
        "ir_drop_map": ir_drop_map,
        "worst_case_droop_mv": worst_case_droop_mv,
        "em_verdict": em_verdict,
        "status": status,
        "coverage": coverage,
        "warnings": warnings,
    }

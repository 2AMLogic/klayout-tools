"""Define ``klt erc``: the layer-by-layer connectivity model (issue #859,
Phase 1a), the per-gate antenna-ratio verdict (issue #860, Phase 1b), the
core ERC finding checks (issue #861, Phase 1c), and antenna-violation fix
guidance (issue #908, Phase 3) -- all part of the antenna + ERC signoff epic
#713.

Pure library: :func:`run_erc` returns plain Python data (a JSON-serialisable
``dict``) and never prints -- serialisation and human-readable formatting
live in ``cli/erc_cmd.py``, matching every other ``klt`` verb (see
``layers.py``'s docstring on the same convention).

Scope (see ``docs/cli/erc.md`` for the full picture): ``klt erc``'s full,
intended interface (per epic #713) is JSON in (a routed layout, a netlist,
and the PDK's antenna/ERC rules) / JSON out (a per-gate antenna-ratio
verdict citing the PDK limit, plus an ERC finding list -- floating gate,
unconnected/multiply-driven net, missing tie, supply short). **Phase 1a**
(#859) delivered the interface and the layer-by-layer connectivity model --
the per-gate accumulation of connected metal area at each fabrication step
-- that both 1b's and 1c's checks below consume. **Phase 1b** (#860) adds
the antenna-ratio verdict: each level's ``antenna_ratio``
(``cumulative_area_um2 / gate_area_um2``) compared against a real PDK
antenna-ratio limit (``--pdk``), with the limit and its source citation
echoed on every checked level -- see :data:`_SKY130_ANTENNA_RATIO_MAX_EGAR`
below. **Phase 1c** (#861) additively delivers ``erc_findings``: the four
core electrical-correctness rules (floating gate, unconnected/multiply-
driven net, missing substrate/well tie, supply short), computed from the
same connectivity model plus two new optional spec sections (``nets``,
``ties``) -- see "ERC finding checks" below and "Phase scope" /
"ERC finding checks" in ``docs/cli/erc.md``. **Phase 3** (#908) additively
delivers ``levels[].remedy``: for every ``verdict: "violate"`` level, a
standard-fix recommendation (diode insertion, or layer jumping when a
neighbouring level has margin) naming the specific net and layer -- see
:func:`_antenna_remedy`.

Connectivity: geometry is traced with ``klayout.db.LayoutToNetlist`` used
purely for wire/via connectivity (no device recognition registered) --
exactly the same API ``extract.py``'s own metal/via connectivity graph and
``power.py``'s ``run_power`` already use, scoped down to only the
caller-declared gate + stackup (+ tie) layers. This is the "LVS's shared
net extraction" 1c's own issue description names as the connectivity model
1a builds and 1c reuses -- ``LayoutToNetlist`` is the same engine
``extract.py``'s device-aware netlist extraction and ``klt lvs``'s
comparison both sit on top of, used here without device recognition,
exactly as ``power.py`` already established for a sibling connectivity-only
verb.

"Per gate" means "per electrically distinct net whose geometry includes the
declared gate-role layer" -- not "per individually drawn poly finger". This
matches real process-antenna-area-ratio (PAAR) methodology directly:
antenna charge accumulates across an entire electrically connected net, so
two transistor gates tied together by the same poly/metal net are correctly
one accumulation, not two. A gate is therefore auto-discovered from
connectivity -- unlike ``klt power``'s caller-named ``power_nets``, this
spec never requires a ``label_layer`` to identify which net is "a gate";
every net that includes gate-role geometry becomes one entry.

ERC finding checks (issue #861, Phase 1c), each with no device-recognition
dependency -- purely geometric/connectivity, matching this module's Phase
1a posture:

- **Floating gate** (``erc.floating_gate``): a gate net (from the existing
  ``gates[]`` model) whose accumulation stops immediately after the gate
  role -- zero connected area on every ``stackup`` role above it. This is
  the electrical signature of an uncontacted/floating gate, computed
  directly from ``gates[]`` with no additional spec section required.
- **Unconnected / multiply-driven net** (``erc.unconnected_net`` /
  ``erc.multiply_driven_net``): driven by the new optional ``nets`` spec
  section (named nets to check, mirroring ``klt power``'s ``power_nets``
  but with an added ``kind``). A declared net matching zero or more than
  one disconnected electrical island is "unconnected"; two *different*
  declared net names that resolve to the same electrical island are
  "multiply-driven" (shorted together) -- or, when both are declared
  ``"kind": "supply"``, a **supply short** (``erc.supply_short``) instead.
- **Missing substrate/well tie** (``erc.missing_tie``): driven by the new
  optional ``ties`` spec section (each entry names a well/tub layer, a tap
  layer, the ``stackup`` role the tap connects up to, and the supply net
  it must reach). Every physically distinct well/tub shape must contain at
  least one tap that is electrically connected (via this module's own
  connectivity graph) to the declared net; a missing tap, or a tap wired
  to the wrong net entirely, is reported.

See ``docs/cli/erc.md`` for the full spec-file schema and JSON contract.
"""

from __future__ import annotations

import json
import os
from typing import Any

from ._layout import load_layout, select_top_cells
from ._layout import region as _region
from ._layout import texts as _texts

#: `1` -- unchanged since issue #859 (Phase 1a). Phase 1b (#860), Phase 1c
#: (#861), and Phase 3 (#908) all add fields additively -- no bump needed,
#: per docs/cli/erc.md's "Phase scope" and docs/json-contract.md's
#: additive-envelope design: `pdk`/`gates[].antenna_verdict`/
#: `levels[].antenna_ratio`/`levels[].antenna_ratio_max`/
#: `levels[].antenna_ratio_source`/`levels[].verdict` (Phase 1b),
#: `erc_findings`/`erc_finding_count` (Phase 1c), and `levels[].remedy`
#: (Phase 3).
SCHEMA_VERSION = 1


class ErcError(Exception):
    """Raised when ``klt erc`` cannot run: a bad layout/spec file, a
    malformed stackup/via declaration, an unresolvable top cell, an unknown
    ``--pdk`` name, or a spec whose gate role matches no geometry at all.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback -- see ``docs/json-contract.md``.
    """


#: Real sky130 antenna-ratio limits ("MAX_EGAR" -- the maximum effective-
#: area/gate-area ratio *without* an antenna diode), transcribed from the
#: official SkyWater sky130 antenna-rule table for the 5-metal "S8D" stack
#: option:
#: https://github.com/google/skywater-pdk/blob/main/docs/rules/antenna/table-Ia-antenna-rules-s8d.csv
#: -- each value is that table's own "Max EA/A w/o diode" column for the
#: named rule id (fetched 2026-08-12 via `gh search code` against
#: `google/skywater-pdk`, the canonical SkyWater sky130 PDK repository).
#:
#: Verified stack-invariant for every role below: the poly/licon1/li1/mcon/
#: met1/via1/met2 limits are identical across every sky130 metal-stack
#: option table checked (S8D, S8P/SP8P/S8P-10R, S8TM/S8TMC/S8TMA-5R,
#: S8P12/S8PIR/S8PF-10R, S8TNV-5R -- `docs/rules/antenna/table-I{a,e,c,g,b}-
#: antenna-rules-*.csv`), so this one table applies to every sky130 stack
#: variant, not just S8D specifically. met3-and-above limits *do* vary by
#: stack option (0.8-2.0 range across the checked tables) and are
#: intentionally not transcribed here -- a candidate follow-on, not a
#: silent omission (matching `decks/sky130.py`'s own convention of calling
#: out every deliberately-uncovered rule rather than pretending full
#: coverage).
#:
#: `licon1`/`mcon`/`via1` are kept here for citation completeness but are
#: never matched against a `stackup` role in practice: those are `klt erc`
#: *via* roles (see `_validate_vias`), not `stackup` roles, so they never
#: get a `levels[]` entry to attach a verdict to -- see this module's own
#: "Per gate" docstring section above. The source table also treats them
#: as area-only "Horizontal" checks (`<via-layer> area/gate area`, no
#: perimeter term) rather than the perimeter-based "Vertical" checks the
#: `stackup` roles use, a distinction this connectivity-area-only model
#: does not represent either way.
_SKY130_ANTENNA_RATIO_MAX_EGAR: dict[str, tuple[float, str]] = {
    "poly": (50.0, ".poly.1"),
    "licon1": (3.0, ".licon.1"),
    "li1": (75.0, ".li.1"),
    "mcon": (3.0, ".mcon.1"),
    "met1": (400.0, ".met1.1"),
    "via1": (6.0, ".via.1"),
    "met2": (400.0, ".met2.1"),
}

_SKY130_ANTENNA_SOURCE_URL = (
    "https://github.com/google/skywater-pdk/blob/main/docs/rules/antenna/"
    "table-Ia-antenna-rules-s8d.csv"
)

#: PDK name -> (role name -> (limit, source rule id)) lookup for `--pdk`.
#: sky130 only, per this repo's "open PDKs, sky130 first" scope (see
#: CLAUDE.md) -- gf180mcu antenna limits are a candidate follow-on, not
#: silently dropped.
_ANTENNA_LIMITS_BY_PDK: dict[str, dict[str, tuple[float, str]]] = {
    "sky130": _SKY130_ANTENNA_RATIO_MAX_EGAR,
}

_ANTENNA_SOURCE_URL_BY_PDK: dict[str, str] = {
    "sky130": _SKY130_ANTENNA_SOURCE_URL,
}


def _resolve_antenna_limits(pdk: str | None) -> dict[str, tuple[float, str]] | None:
    """Resolve ``--pdk`` to its antenna-ratio limit table.

    Returns ``None`` when ``pdk`` is ``None`` -- antenna ratios are still
    computed and reported on every level (see ``run_erc``), just with no
    PDK limit to compare against, so every level's ``verdict`` comes back
    ``"unchecked"``.

    Raises :class:`ErcError` for an unrecognised ``pdk`` name, matching
    ``klt drc``'s own ``--deck`` convention: an unknown name is a clean
    exit-1 error (not an argparse usage error), checked eagerly before any
    layout is even loaded.
    """
    if pdk is None:
        return None
    limits = _ANTENNA_LIMITS_BY_PDK.get(pdk)
    if limits is None:
        raise ErcError(
            f"unknown --pdk {pdk!r} (supported: "
            f"{', '.join(sorted(_ANTENNA_LIMITS_BY_PDK))})"
        )
    return limits


def _load_spec(spec_path: str) -> dict[str, Any]:
    if not os.path.exists(spec_path):
        raise ErcError(f"spec file not found: {spec_path}")
    if os.path.isdir(spec_path):
        raise ErcError(f"not a file: {spec_path}")
    try:
        with open(spec_path) as f:
            spec = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        raise ErcError(f"could not read spec '{spec_path}': {exc}") from exc
    if not isinstance(spec, dict):
        raise ErcError(f"spec '{spec_path}' must be a JSON object")
    return spec


def _parse_layer_datatype(raw: str, spec_path: str, field: str) -> tuple[int, int]:
    parts = raw.split("/")
    malformed = ErcError(
        f"spec '{spec_path}': {field} must be '<layer>/<datatype>' with "
        f"integer layer/datatype (got {raw!r})"
    )
    if len(parts) != 2:
        raise malformed
    try:
        return int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise malformed from exc


def _validate_stackup(spec: dict[str, Any], spec_path: str) -> list[dict[str, Any]]:
    """Validate the ``stackup`` array: fabrication order from the gate
    layer up through every metal role a gate's net may reach.

    ``stackup[0]`` must declare ``"role": "gate"`` -- the polysilicon/gate
    layer that starts the accumulation. Every other entry is an ordinary
    metal role and must not repeat ``role: "gate"``. At least one metal
    role beyond the gate itself is required, or "layer-by-layer
    accumulation" has nothing to accumulate.
    """
    raw = spec.get("stackup")
    if not isinstance(raw, list) or len(raw) < 2:
        raise ErcError(
            f"spec '{spec_path}' must have a 'stackup' array with at least "
            "two entries: the gate layer (stackup[0], role='gate') and at "
            "least one metal role above it"
        )

    entries: list[dict[str, Any]] = []
    names: list[str] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ErcError(f"spec '{spec_path}': stackup[{i}] must be a JSON object")
        for key in ("name", "layer"):
            if key not in entry:
                raise ErcError(f"spec '{spec_path}': stackup[{i}] missing {key!r}")
        name = str(entry["name"])
        if name in names:
            raise ErcError(f"spec '{spec_path}': duplicate stackup name {name!r}")
        names.append(name)

        layer = _parse_layer_datatype(
            str(entry["layer"]), spec_path, f"stackup[{i}].layer"
        )
        label_layer = None
        if entry.get("label_layer") is not None:
            label_layer = _parse_layer_datatype(
                str(entry["label_layer"]), spec_path, f"stackup[{i}].label_layer"
            )

        role = entry.get("role")
        if role is not None and role != "gate":
            raise ErcError(
                f"spec '{spec_path}': stackup[{i}].role must be omitted or "
                f"'gate' (got {role!r})"
            )
        if i == 0 and role != "gate":
            raise ErcError(
                f'spec \'{spec_path}\': stackup[0] must set "role": "gate" '
                "-- the polysilicon/gate layer, first in fabrication order"
            )
        if i > 0 and role == "gate":
            raise ErcError(
                f"spec '{spec_path}': only stackup[0] may set \"role\": "
                f'"gate" (a second one was found at stackup[{i}])'
            )

        entries.append(
            {
                "name": name,
                "layer": layer,
                "label_layer": label_layer,
                "role": role,
            }
        )

    return entries


def _validate_vias(
    spec: dict[str, Any], spec_path: str, stackup_names: list[str]
) -> list[dict[str, Any]]:
    raw = spec.get("vias", [])
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ErcError(f"spec '{spec_path}': 'vias' must be an array")

    entries: list[dict[str, Any]] = []
    names: list[str] = list(stackup_names)
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ErcError(f"spec '{spec_path}': vias[{i}] must be a JSON object")
        for key in ("layer", "between"):
            if key not in entry:
                raise ErcError(f"spec '{spec_path}': vias[{i}] missing {key!r}")

        layer = _parse_layer_datatype(
            str(entry["layer"]), spec_path, f"vias[{i}].layer"
        )

        between = entry["between"]
        if (
            not isinstance(between, list)
            or len(between) != 2
            or str(between[0]) == str(between[1])
            or any(str(n) not in stackup_names for n in between)
        ):
            raise ErcError(
                f"spec '{spec_path}': vias[{i}].between must name two distinct "
                f"'stackup' entries (got {between!r})"
            )

        name = str(entry.get("name", f"via{i}"))
        if name in names:
            raise ErcError(f"spec '{spec_path}': duplicate via/stackup name {name!r}")
        names.append(name)

        entries.append(
            {
                "name": name,
                "layer": layer,
                "between": (str(between[0]), str(between[1])),
            }
        )
    return entries


def _validate_nets(spec: dict[str, Any], spec_path: str) -> list[dict[str, Any]]:
    """Validate the optional ``nets`` array (issue #861): named nets to
    check for connectivity findings (``erc.unconnected_net``,
    ``erc.multiply_driven_net``, ``erc.supply_short``). Mirrors ``klt
    power``'s ``power_nets`` name-matching convention, plus a ``kind`` used
    to classify a two-net short as a plain multiply-driven net vs. a supply
    short. Omitted or empty -> no net-connectivity findings are computed
    (a caller who only wants the floating-gate check need not declare
    this)."""
    raw = spec.get("nets", [])
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ErcError(f"spec '{spec_path}': 'nets' must be an array")

    entries: list[dict[str, Any]] = []
    names: list[str] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ErcError(f"spec '{spec_path}': nets[{i}] must be a JSON object")
        if "name" not in entry:
            raise ErcError(f"spec '{spec_path}': nets[{i}] missing 'name'")
        name = str(entry["name"]).strip()
        if not name:
            raise ErcError(f"spec '{spec_path}': nets[{i}].name must be non-empty")
        if name in names:
            raise ErcError(f"spec '{spec_path}': duplicate net name {name!r}")
        names.append(name)

        kind = entry.get("kind", "signal")
        if kind not in ("signal", "supply"):
            raise ErcError(
                f"spec '{spec_path}': nets[{i}].kind must be 'signal' or "
                f"'supply' (got {kind!r})"
            )

        entries.append({"name": name, "kind": kind})
    return entries


def _validate_ties(
    spec: dict[str, Any], spec_path: str, stackup_names: list[str]
) -> list[dict[str, Any]]:
    """Validate the optional ``ties`` array (issue #861): substrate/well
    tie declarations for the ``erc.missing_tie`` check. Each entry names a
    well/tub layer and the tap (contact) layer expected inside it, the
    ``stackup`` role the tap must be wired up to, and the supply net that
    tap must ultimately reach. Omitted or empty -> no missing-tie findings
    are computed."""
    raw = spec.get("ties", [])
    if raw is None:
        raw = []
    if not isinstance(raw, list):
        raise ErcError(f"spec '{spec_path}': 'ties' must be an array")

    entries: list[dict[str, Any]] = []
    names: list[str] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ErcError(f"spec '{spec_path}': ties[{i}] must be a JSON object")
        for key in ("well_layer", "tap_layer", "connect_to", "net"):
            if key not in entry:
                raise ErcError(f"spec '{spec_path}': ties[{i}] missing {key!r}")

        name = str(entry.get("name", f"tie{i}"))
        if name in names:
            raise ErcError(f"spec '{spec_path}': duplicate tie name {name!r}")
        names.append(name)

        well_layer = _parse_layer_datatype(
            str(entry["well_layer"]), spec_path, f"ties[{i}].well_layer"
        )
        tap_layer = _parse_layer_datatype(
            str(entry["tap_layer"]), spec_path, f"ties[{i}].tap_layer"
        )

        connect_to = str(entry["connect_to"])
        if connect_to not in stackup_names:
            raise ErcError(
                f"spec '{spec_path}': ties[{i}].connect_to must name a "
                f"'stackup' entry (got {connect_to!r})"
            )

        net = str(entry["net"]).strip()
        if not net:
            raise ErcError(f"spec '{spec_path}': ties[{i}].net must be non-empty")

        entries.append(
            {
                "name": name,
                "well_layer": well_layer,
                "tap_layer": tap_layer,
                "connect_to": connect_to,
                "net": net,
            }
        )
    return entries


def _bbox_dict(box: Any) -> dict[str, int]:
    """A violation-style ``bbox`` dict (raw database units, matching
    ``klt drc``'s own ``violations[].bbox`` convention -- see
    ``drc.py``'s ``run_drc`` docstring)."""
    return {"left": box.left, "bottom": box.bottom, "right": box.right, "top": box.top}


def _finding(
    rule: str,
    description: str,
    *,
    net: str | None = None,
    other_net: str | None = None,
    gate_id: str | None = None,
    layer: str | None = None,
    bbox: dict[str, int] | None = None,
) -> dict[str, Any]:
    """One ``erc_findings[]`` entry (issue #861's finding shape, see this
    module's docstring "ERC finding checks"): the 7-key dict shared
    verbatim by every rule id (``erc.floating_gate``,
    ``erc.unconnected_net``, ``erc.multiply_driven_net``,
    ``erc.supply_short``, ``erc.missing_tie``) -- only which of
    ``net``/``other_net``/``gate_id``/``layer``/``bbox`` are populated vs.
    left ``None`` varies per call site. Mirrors ``ring_check.py``'s own
    keyword-only ``_violation()`` helper for the equivalent ``klt drc``-
    shaped violation dict."""
    return {
        "rule": rule,
        "description": description,
        "net": net,
        "other_net": other_net,
        "gate_id": gate_id,
        "layer": layer,
        "bbox": bbox,
    }


def _antenna_remedy(
    levels: list[dict[str, Any]], index: int, net: str | None
) -> dict[str, Any]:
    """``levels[].remedy`` for a single ``verdict: "violate"`` level (issue
    #908, epic #713 Phase 3 -- "fix guidance"): recommend the standard
    antenna-violation fix -- diode insertion or layer jumping -- naming the
    specific net and layer, so the violation is directly actionable rather
    than just flagged.

    Layer jumping is recommended only when both hold:

    - **the violation is layer-local**: the immediately preceding
      ``stackup`` level (``levels[index - 1]``, when one exists) does *not*
      itself violate. When a lower level already violates, that excess is
      already baked into every level above it (``cumulative_area_um2``
      only ever grows going up the stackup) -- redistributing this net's
      routing to an adjacent layer cannot undo a violation that originated
      lower down, so jumping is not a real fix there.
    - **an adjacent stackup level has margin**: either neighbour
      (``levels[index - 1]`` or ``levels[index + 1]``, whichever exists)
      reports ``verdict == "pass"`` -- i.e. it has headroom under its own
      limit, so shifting more of this net's routing onto that layer is a
      real option. The higher neighbour is preferred when both qualify
      (continuing the route forward, onto the next fabrication step, is
      the more common real remedy); the lower neighbour is used when only
      it has margin (e.g. the violating level is the last in the stackup).

    Every other case falls back to diode insertion -- the general-purpose
    remedy (an antenna diode bleeds off accumulated charge regardless of
    which layer is at fault), used whenever the layer-jumping
    preconditions above are not met.
    """
    level = levels[index]
    layer = level["layer"]
    ratio = level["antenna_ratio"]
    limit = level["antenna_ratio_max"]
    # `net` is `None` for an unlabelled gate (see `gates[].net`) -- name it
    # in prose rather than interpolating the bare Python `None` repr.
    net_desc = f"net {net!r}" if net is not None else "this unlabelled net"

    lower = levels[index - 1] if index > 0 else None
    higher = levels[index + 1] if index + 1 < len(levels) else None

    layer_local = lower is None or lower["verdict"] != "violate"
    higher_margin = higher is not None and higher["verdict"] == "pass"
    lower_margin = lower is not None and lower["verdict"] == "pass"

    if layer_local and (higher_margin or lower_margin):
        target = higher if higher_margin else lower
        return {
            "type": "layer_jumping",
            "net": net,
            "layer": layer,
            "target_layer": target["layer"],
            "justification": (
                f"{net_desc} exceeds the antenna-ratio limit at {layer!r} "
                f"(ratio {ratio:.3g} > {limit:.3g}); {target['layer']!r} has "
                f"margin (ratio {target['antenna_ratio']:.3g} <= "
                f"{target['antenna_ratio_max']:.3g}) -- route more of this "
                f"net's connection through {target['layer']!r} instead of "
                f"continuing to accumulate area on {layer!r}"
            ),
        }

    if layer_local:
        reason = (
            "no adjacent stackup level has margin to absorb the excess "
            "routing (a layer-local violation with no jump target)"
        )
    else:
        reason = (
            "the violation is already present at the preceding stackup "
            f"level ({lower['layer']!r}), so it carries forward regardless "
            f"of how routing on {layer!r} is redistributed"
        )
    return {
        "type": "diode_insertion",
        "net": net,
        "layer": layer,
        "target_layer": None,
        "justification": (
            f"{net_desc} exceeds the antenna-ratio limit at {layer!r} "
            f"(ratio {ratio:.3g} > {limit:.3g}); {reason} -- insert an "
            f"antenna diode on this net at {layer!r} to bleed off "
            "accumulated charge"
        ),
    }


def _floating_gate_findings(
    gate_entries_and_regions: list[tuple[dict[str, Any], Any]], gate_role: str
) -> list[dict[str, Any]]:
    """``erc.floating_gate`` findings (issue #861): every gate whose
    accumulation (``gates[].levels``) stops immediately after the gate role
    -- zero connected area on every ``stackup`` role above it -- is an
    uncontacted/floating gate. Computed directly from the already-built
    ``gates[]`` model; needs no additional spec section."""
    findings: list[dict[str, Any]] = []
    for gate_entry, gate_region in gate_entries_and_regions:
        levels = gate_entry["levels"]
        if len(levels) <= 1:
            continue
        if all(level["step_area_um2"] == 0.0 for level in levels[1:]):
            findings.append(
                _finding(
                    "erc.floating_gate",
                    (
                        "gate net has no connected geometry above the gate "
                        "layer (floating/uncontacted gate)"
                    ),
                    net=gate_entry["net"],
                    gate_id=gate_entry["gate_id"],
                    layer=gate_role,
                    bbox=_bbox_dict(gate_region.bbox()),
                )
            )
    return findings


def _match_net_clusters(circuit: Any, name: str) -> list[Any]:
    """Every net in ``circuit`` whose label set includes ``name``, one per
    disconnected electrical island, sorted by ``cluster_id`` for
    deterministic output.

    When two *different* labels land on the same electrically-connected
    net (a short), ``klayout.db.Net.expanded_name()`` joins every label
    into one comma-separated string (e.g. ``"SHORTED_VDD,VSS"``) rather
    than picking just one -- verified empirically for this issue. Matching
    against the *split* label set (rather than plain equality, the way
    ``power.py``'s ``run_power`` matches its un-shorted ``power_nets``) is
    what lets a single declared name still resolve to a shorted net at
    all -- an exact-equality match would silently see zero matches for
    every name folded into a short, masking the short as
    ``erc.unconnected_net`` instead of correctly finding it."""
    return sorted(
        (
            net
            for net in circuit.each_net()
            if net.cluster_id != 0 and name in net.expanded_name().split(",")
        ),
        key=lambda net: net.cluster_id,
    )


def _net_connectivity_findings(
    circuit: Any, nets_decl: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """``erc.unconnected_net`` / ``erc.multiply_driven_net`` /
    ``erc.supply_short`` findings (issue #861), driven by the optional
    ``nets`` spec section.

    A declared net matching zero, or more than one, disconnected electrical
    island is ``erc.unconnected_net`` (nothing carries that name at all, or
    the intended net is split into pieces that never actually touch).

    Two *different* declared net names whose matched nets share a
    ``cluster_id`` are electrically the very same net regardless of their
    separate labels -- a short. Reported per unordered pair (deterministic,
    sorted): ``erc.supply_short`` when both are declared
    ``"kind": "supply"``, else the more general ``erc.multiply_driven_net``.
    """
    if not nets_decl:
        return []

    findings: list[dict[str, Any]] = []
    matches: dict[str, list[Any]] = {}
    for decl in nets_decl:
        matched = _match_net_clusters(circuit, decl["name"])
        matches[decl["name"]] = matched
        if len(matched) == 0:
            findings.append(
                _finding(
                    "erc.unconnected_net",
                    (
                        f"declared net {decl['name']!r} matches no labelled "
                        "geometry in this layout"
                    ),
                    net=decl["name"],
                )
            )
        elif len(matched) > 1:
            findings.append(
                _finding(
                    "erc.unconnected_net",
                    (
                        f"declared net {decl['name']!r} resolves to "
                        f"{len(matched)} disconnected electrical islands "
                        "(expected exactly one)"
                    ),
                    net=decl["name"],
                )
            )

    cluster_to_names: dict[int, list[str]] = {}
    for decl in nets_decl:
        for net in matches[decl["name"]]:
            cluster_to_names.setdefault(net.cluster_id, []).append(decl["name"])

    kind_by_name = {decl["name"]: decl["kind"] for decl in nets_decl}
    for names in cluster_to_names.values():
        distinct = sorted(set(names))
        if len(distinct) < 2:
            continue
        for i in range(len(distinct)):
            for j in range(i + 1, len(distinct)):
                a, b = distinct[i], distinct[j]
                both_supply = (
                    kind_by_name[a] == "supply" and kind_by_name[b] == "supply"
                )
                rule = "erc.supply_short" if both_supply else "erc.multiply_driven_net"
                findings.append(
                    _finding(
                        rule,
                        (
                            f"declared nets {a!r} and {b!r} are electrically "
                            "the same net (shorted together)"
                        ),
                        net=a,
                        other_net=b,
                    )
                )
    return findings


def _tie_findings(
    l2n: Any, circuit: Any, tie_layers: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """``erc.missing_tie`` findings (issue #861), driven by the optional
    ``ties`` spec section.

    For every physically distinct well/tub shape (each merged polygon of a
    tie's ``well_layer``), a tap (``tap_layer``) must be drawn inside it
    *and* that tap must be electrically connected (via this module's own
    connectivity graph -- the tap is wired to ``connect_to``'s ``stackup``
    region during registration, see ``run_erc``) to the declared ``net``.
    Both failure modes are reported under the same rule id: no tap drawn in
    the well at all, or a tap present but wired to a different net (or no
    net at all).

    Locating the tap's own net uses ``LayoutToNetlist.probe_net`` at a
    point known to have tap geometry (mirroring ``extract.py``'s
    ``_probe_abstract_pin_net``) rather than a per-well scan over every
    extracted net, which stays cheap even for a layout with many wells.
    """
    if not tie_layers:
        return []

    # Imported lazily, matching this module's/the codebase's convention (see
    # `load_layout`'s docstring) -- needed here for `kdb.Region(polygon)`.
    import klayout.db as kdb

    findings: list[dict[str, Any]] = []
    for tie in tie_layers:
        matched_cluster_ids = {
            net.cluster_id for net in _match_net_clusters(circuit, tie["net"])
        }

        for well_poly in tie["well_region"].merged().each():
            poly_region = kdb.Region(well_poly)
            tap_here = tie["tap_region"].interacting(poly_region)
            if tap_here.is_empty():
                findings.append(
                    _finding(
                        "erc.missing_tie",
                        (
                            f"well/tub region has no {tie['name']!r} tap "
                            "contact drawn inside it"
                        ),
                        net=tie["net"],
                        layer=tie["name"],
                        bbox=_bbox_dict(well_poly.bbox()),
                    )
                )
                continue

            probe_point = next(iter(tap_here.merged().each())).bbox().center()
            owner_net = l2n.probe_net(tie["tap_region"], probe_point)
            owner_cluster_id = owner_net.cluster_id if owner_net is not None else None

            if owner_cluster_id is None or owner_cluster_id not in matched_cluster_ids:
                findings.append(
                    _finding(
                        "erc.missing_tie",
                        (
                            "well/tub tap is not connected to declared net "
                            f"{tie['net']!r}"
                        ),
                        net=tie["net"],
                        layer=tie["name"],
                        bbox=_bbox_dict(well_poly.bbox()),
                    )
                )
    return findings


def run_erc(
    file: str,
    spec_path: str,
    *,
    top: str | None = None,
    pdk: str | None = None,
) -> dict[str, Any]:
    """Run ``klt erc``'s connectivity-model extraction, antenna-ratio
    check, and core ERC finding checks end to end.

    ``file`` is a routed GDSII/OASIS layout; ``spec_path`` is a JSON file
    with:

    - ``stackup`` (required, >= 2 entries): fabrication order from the gate
      layer up. Each entry is ``{"name", "layer": "<layer>/<datatype>",
      "label_layer": "<layer>/<datatype>" (optional)}``; ``stackup[0]``
      additionally sets ``"role": "gate"``.
    - ``vias`` (optional array, default ``[]``): each entry bridges two
      ``stackup`` names -- ``{"name" (optional, defaults to "via<index>"),
      "layer": "<layer>/<datatype>", "between": ["<role>", "<role>"]}``.
    - ``nets`` (optional array, default ``[]``, issue #861): named nets to
      check for connectivity findings -- ``{"name",
      "kind": "signal" | "supply"` (default ``"signal"``)}``. Drives the
      ``erc.unconnected_net`` / ``erc.multiply_driven_net`` /
      ``erc.supply_short`` findings; omitted entirely -> none of those
      three are computed.
    - ``ties`` (optional array, default ``[]``, issue #861): substrate/well
      tie declarations -- ``{"name" (optional, defaults to "tie<index>"),
      "well_layer": "<layer>/<datatype>", "tap_layer": "<layer>/<datatype>",
      "connect_to": "<stackup role>", "net"}``. Drives the
      ``erc.missing_tie`` finding; omitted entirely -> none are computed.

    ``top`` selects the top cell to analyse when the stream has more than
    one (required in that case, matching ``select_top_cells``'s convention
    -- see ``docs/cli/layers.md``'s ``--top``).

    ``pdk`` (optional, e.g. ``"sky130"``) selects the antenna-ratio limit
    table each non-gate ``stackup`` level's ``antenna_ratio`` is compared
    against -- see :data:`_SKY130_ANTENNA_RATIO_MAX_EGAR`. Omit it to still
    get every level's derived ``antenna_ratio``, just with ``verdict``
    ``"unchecked"`` everywhere (no PDK limit to compare against).

    Returns a dict matching the documented ``klt erc`` JSON schema (see
    ``docs/cli/erc.md``), including ``schema_version``, (issue #861)
    ``erc_findings``/``erc_finding_count``, and (issue #908) each violating
    ``levels[].remedy``. Raises :class:`ErcError` for a malformed spec, an
    unknown ``pdk``, an unresolvable layout/top cell, or a layout in which
    no net carries any geometry on the declared gate role at all.
    """
    antenna_limits = _resolve_antenna_limits(pdk)

    spec = _load_spec(spec_path)
    stackup = _validate_stackup(spec, spec_path)
    stackup_names = [entry["name"] for entry in stackup]
    vias = _validate_vias(spec, spec_path, stackup_names)
    nets_decl = _validate_nets(spec, spec_path)
    ties = _validate_ties(spec, spec_path, stackup_names)

    layout = load_layout(file, ErcError)
    top_cells = select_top_cells(layout, top, ErcError)
    if len(top_cells) != 1:
        raise ErcError(
            f"klt erc needs exactly one top cell to analyse ({len(top_cells)} "
            f"found in '{file}'); pass --top to select one"
        )
    top_cell = top_cells[0]
    dbu = layout.dbu

    # Imported lazily, matching `load_layout`'s own lazy `klayout.db` import.
    import klayout.db as kdb

    l2n = kdb.LayoutToNetlist(top_cell.name, dbu)

    layer_index: dict[str, int] = {}
    regions: dict[str, Any] = {}
    for entry in stackup:
        conductor_region = _region(layout, top_cell, entry["layer"])
        regions[entry["name"]] = conductor_region
        layer_index[entry["name"]] = l2n.register(conductor_region, entry["name"])
        l2n.connect(conductor_region)
        if entry["label_layer"] is not None:
            label_texts = _texts(layout, top_cell, entry["label_layer"])
            l2n.register(label_texts, f"{entry['name']}_label")
            l2n.connect(conductor_region, label_texts)

    via_layer_index: dict[str, int] = {}
    for via in vias:
        via_region = _region(layout, top_cell, via["layer"])
        via_layer_index[via["name"]] = l2n.register(via_region, via["name"])
        l2n.connect(via_region)
        role_a, role_b = via["between"]
        l2n.connect(regions[role_a], via_region)
        l2n.connect(via_region, regions[role_b])

    # ERC tie declarations (issue #861): register each tie's well/tap
    # layers and wire the tap up to its declared `connect_to` stackup
    # region *before* `extract_netlist()` runs, so the single unified
    # connectivity graph below already reflects whether a well's tap
    # actually reaches the declared net -- see `_tie_findings`.
    tie_layers: list[dict[str, Any]] = []
    for tie in ties:
        well_region = _region(layout, top_cell, tie["well_layer"])
        tap_region = _region(layout, top_cell, tie["tap_layer"])
        l2n.register(well_region, f"{tie['name']}__well")
        l2n.register(tap_region, f"{tie['name']}__tap")
        l2n.connect(well_region)
        l2n.connect(tap_region)
        l2n.connect(well_region, tap_region)
        l2n.connect(tap_region, regions[tie["connect_to"]])
        tie_layers.append(
            {
                **tie,
                "well_region": well_region,
                "tap_region": tap_region,
            }
        )

    try:
        l2n.extract_netlist()
    except Exception as exc:  # KLayout raises a bare RuntimeError on internal failure
        raise ErcError(f"connectivity extraction failed: {exc}") from exc

    netlist = l2n.netlist()
    circuit = netlist.circuit_by_name(top_cell.name)
    if circuit is None:
        raise ErcError(
            f"no circuit named '{top_cell.name}' in the extracted connectivity graph"
        )

    gate_role = stackup[0]["name"]
    gate_layer_index = layer_index[gate_role]
    dbu2_um2 = dbu * dbu

    # Every distinct net (cluster) is already one electrically-connected
    # island by construction (`LayoutToNetlist` clusters connected geometry
    # into one `Net` per cluster id) -- unlike `klt power`'s caller-named
    # `power_nets`, no name match is needed to find "a gate": any net whose
    # geometry touches the declared gate role qualifies. Sorted by
    # `cluster_id` for deterministic, reproducible output.
    candidates = sorted(
        (net for net in circuit.each_net() if net.cluster_id != 0),
        key=lambda net: net.cluster_id,
    )

    gates: list[dict[str, Any]] = []
    # Parallel to `gates` (same order/length) -- each gate's own merged
    # gate-role region, kept only for `_floating_gate_findings`'s `bbox`
    # (not part of the `gates[]` JSON schema itself).
    gate_regions: list[Any] = []
    for net in candidates:
        gate_region = l2n.polygons_of_net(net, gate_layer_index).merged()
        gate_area_um2 = gate_region.area() * dbu2_um2
        if gate_area_um2 <= 0:
            continue

        levels: list[dict[str, Any]] = []
        cumulative_um2 = 0.0
        for i, entry in enumerate(stackup):
            step_region = l2n.polygons_of_net(net, layer_index[entry["name"]])
            step_um2 = step_region.merged().area() * dbu2_um2
            cumulative_um2 += step_um2

            # `cumulative_area_um2 / gate_area_um2` per docs/cli/erc.md's
            # own Phase-1a "Phase scope" note on what 1b delivers. The gate
            # role itself (i == 0) is never PDK-checked: its ratio is
            # trivially 1.0 (cumulative == gate area at that level), and
            # the source table's own poly rule measures a different
            # quantity entirely (poly perimeter, not cumulative connected
            # area) -- not something this area-only model computes.
            antenna_ratio = round(cumulative_um2 / gate_area_um2, 6)
            limit_entry = (
                None
                if i == 0 or antenna_limits is None
                else antenna_limits.get(entry["name"])
            )
            if limit_entry is not None:
                limit_value, rule_id = limit_entry
                antenna_ratio_max: float | None = limit_value
                antenna_ratio_source: str | None = (
                    f"{_ANTENNA_SOURCE_URL_BY_PDK[pdk]} rule {rule_id!r}, "
                    "'Max EA/A w/o diode' column"
                )
                verdict = "violate" if antenna_ratio > limit_value else "pass"
            else:
                antenna_ratio_max = None
                antenna_ratio_source = None
                verdict = "unchecked"

            levels.append(
                {
                    "layer": entry["name"],
                    "step_area_um2": round(step_um2, 9),
                    "cumulative_area_um2": round(cumulative_um2, 9),
                    "antenna_ratio": antenna_ratio,
                    "antenna_ratio_max": antenna_ratio_max,
                    "antenna_ratio_source": antenna_ratio_source,
                    "verdict": verdict,
                }
            )

        # Fix guidance (issue #908, epic #713 Phase 3) -- a second pass over
        # the now-complete `levels` list, since a remedy for level `i` may
        # look at `levels[i + 1]` (see `_antenna_remedy`). `None` for every
        # non-violating level (including every level when `pdk` was omitted,
        # since `verdict` is then always "unchecked").
        for i, level in enumerate(levels):
            level["remedy"] = (
                _antenna_remedy(levels, i, net.name or None)
                if level["verdict"] == "violate"
                else None
            )

        level_verdicts = {level["verdict"] for level in levels}
        if "violate" in level_verdicts:
            antenna_verdict = "violate"
        elif "pass" in level_verdicts:
            antenna_verdict = "pass"
        else:
            antenna_verdict = "unchecked"

        gates.append(
            {
                "gate_id": f"gate{len(gates)}",
                "net": net.name or None,
                "gate_area_um2": round(gate_area_um2, 9),
                "antenna_verdict": antenna_verdict,
                "levels": levels,
            }
        )
        gate_regions.append(gate_region)

    if not gates:
        raise ErcError(
            f"no net in '{file}' has any geometry on the declared gate role "
            f"{gate_role!r} (stackup[0], layer {stackup[0]['layer'][0]}/"
            f"{stackup[0]['layer'][1]}) -- check the spec's stackup[0].layer "
            "against the layout's own resolved PDK gate-poly layer number"
        )

    # ERC finding checks (issue #861, Phase 1c) -- see this module's
    # docstring, "ERC finding checks", for what each rule detects.
    erc_findings: list[dict[str, Any]] = []
    erc_findings.extend(
        _floating_gate_findings(list(zip(gates, gate_regions, strict=True)), gate_role)
    )
    erc_findings.extend(_net_connectivity_findings(circuit, nets_decl))
    erc_findings.extend(_tie_findings(l2n, circuit, tie_layers))
    erc_findings.sort(
        key=lambda f: (
            f["rule"],
            f["net"] or "",
            f["other_net"] or "",
            f["gate_id"] or "",
            f["layer"] or "",
        )
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "file": file,
        "spec": spec_path,
        "pdk": pdk,
        "gate_role": gate_role,
        "gate_count": len(gates),
        "gates": gates,
        "erc_findings": erc_findings,
        "erc_finding_count": len(erc_findings),
    }

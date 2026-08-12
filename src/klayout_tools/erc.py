"""Define ``klt erc``, the layer-by-layer connectivity model (issue #859,
Phase 1a), and the per-gate antenna-ratio verdict (issue #860, Phase 1b) --
both part of the antenna + ERC signoff epic #713.

Pure library: :func:`run_erc` returns plain Python data (a JSON-serialisable
``dict``) and never prints -- serialisation and human-readable formatting
live in ``cli/erc_cmd.py``, matching every other ``klt`` verb (see
``layers.py``'s docstring on the same convention).

Scope of these phases (see ``docs/cli/erc.md`` for the full picture): ``klt
erc``'s full, intended interface (per epic #713) is JSON in (a routed
layout, a netlist, and the PDK's antenna/ERC rules) / JSON out (a per-gate
antenna-ratio verdict citing the PDK limit, plus an ERC finding list --
floating gate, missing tie, supply short, ...). **Phase 1a delivered the
interface and the layer-by-layer connectivity model** -- the per-gate
accumulation of connected metal area at each fabrication step. **Phase 1b
(this issue) adds the antenna-ratio verdict**: each level's ``antenna_ratio``
(``cumulative_area_um2 / gate_area_um2``) compared against a real PDK
antenna-ratio limit (``--pdk``), with the limit and its source citation
echoed on every checked level -- see :data:`_SKY130_ANTENNA_RATIO_MAX_EGAR`
below. An ERC finding list (floating gate, missing tie, supply short) is
still not computed here; that is Phase 1c (#861).

Connectivity: geometry is traced with ``klayout.db.LayoutToNetlist`` used
purely for wire/via connectivity (no device recognition registered) --
exactly the same API ``extract.py``'s own metal/via connectivity graph and
``power.py``'s ``run_power`` already use, scoped down to only the
caller-declared gate + stackup layers. This is the "LVS's shared net
extraction" 1c's own issue description names as the connectivity model 1a
builds and 1c reuses -- ``LayoutToNetlist`` is the same engine
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
"""

from __future__ import annotations

import json
import os
from typing import Any

from ._layout import load_layout, select_top_cells
from ._layout import region as _region
from ._layout import texts as _texts

#: `1` -- unchanged since issue #859 (Phase 1a). Phase 1b (this issue) adds
#: `pdk`/`gates[].antenna_verdict`/`levels[].antenna_ratio`/
#: `levels[].antenna_ratio_max`/`levels[].antenna_ratio_source`/
#: `levels[].verdict` fields additively -- no bump needed, per
#: docs/cli/erc.md's "Phase scope" and docs/json-contract.md's additive-
#: envelope design. A future phase adds `erc_findings` the same way.
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


def run_erc(
    file: str,
    spec_path: str,
    *,
    top: str | None = None,
    pdk: str | None = None,
) -> dict[str, Any]:
    """Run ``klt erc``'s connectivity-model extraction and antenna-ratio
    check end to end.

    ``file`` is a routed GDSII/OASIS layout; ``spec_path`` is a JSON file
    with:

    - ``stackup`` (required, >= 2 entries): fabrication order from the gate
      layer up. Each entry is ``{"name", "layer": "<layer>/<datatype>",
      "label_layer": "<layer>/<datatype>" (optional)}``; ``stackup[0]``
      additionally sets ``"role": "gate"``.
    - ``vias`` (optional array, default ``[]``): each entry bridges two
      ``stackup`` names -- ``{"name" (optional, defaults to "via<index>"),
      "layer": "<layer>/<datatype>", "between": ["<role>", "<role>"]}``.

    ``top`` selects the top cell to analyse when the stream has more than
    one (required in that case, matching ``select_top_cells``'s convention
    -- see ``docs/cli/layers.md``'s ``--top``).

    ``pdk`` (optional, e.g. ``"sky130"``) selects the antenna-ratio limit
    table each non-gate ``stackup`` level's ``antenna_ratio`` is compared
    against -- see :data:`_SKY130_ANTENNA_RATIO_MAX_EGAR`. Omit it to still
    get every level's derived ``antenna_ratio``, just with ``verdict``
    ``"unchecked"`` everywhere (no PDK limit to compare against).

    Returns a dict matching the documented ``klt erc`` JSON schema (see
    ``docs/cli/erc.md``), including ``schema_version``. Raises
    :class:`ErcError` for a malformed spec, an unknown ``pdk``, an
    unresolvable layout/top cell, or a layout in which no net carries any
    geometry on the declared gate role at all.
    """
    antenna_limits = _resolve_antenna_limits(pdk)

    spec = _load_spec(spec_path)
    stackup = _validate_stackup(spec, spec_path)
    stackup_names = [entry["name"] for entry in stackup]
    vias = _validate_vias(spec, spec_path, stackup_names)

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

    if not gates:
        raise ErcError(
            f"no net in '{file}' has any geometry on the declared gate role "
            f"{gate_role!r} (stackup[0], layer {stackup[0]['layer'][0]}/"
            f"{stackup[0]['layer'][1]}) -- check the spec's stackup[0].layer "
            "against the layout's own resolved PDK gate-poly layer number"
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "file": file,
        "spec": spec_path,
        "pdk": pdk,
        "gate_role": gate_role,
        "gate_count": len(gates),
        "gates": gates,
    }

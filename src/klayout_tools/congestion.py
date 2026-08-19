"""FLUTE/RUDY-family post-placement congestion pre-check (issue #785, Epic
#700 Phase 1 §3.6 of `docs/design/place-and-route-improvements-survey.md`).

**Research-spike library module, not a shipped `klt` CLI verb.** This is
deliberate, per the issue's own acceptance criteria: nothing here changes
`klt place-and-route`'s request/response contract. The intended caller is
an internal pre-check inside `digital_fleet.py`'s candidate-ranking loop
(#445) -- run this against a candidate's post-placement geometry, before
paying for OpenROAD's own (comparatively expensive) `global_route`/
`detailed_route`, to flag an obviously-congested candidate cheaply. Whether
that integration is actually justified is exactly what
`scripts/research/flute_congestion_correlation.py` and
`docs/design/flute-congestion-precheck-results.md` settle -- this module
provides the estimator the study needs, without presupposing its own
conclusion.

Two independent pieces, matching every other native-engine `klt` module's
own Rust/Python split (see `mom.py`'s docstring for the precedent):

- :func:`nets_from_def` -- pure Python, no native extension needed. Reads a
  **placement-stage** DEF's `COMPONENTS`/`PINS`/`NETS` sections (the same
  declarative-subset-only posture as `lef_header.py`'s LEF reader: no
  routing geometry, no `SPECIALNETS`, no via/track parsing -- only
  component/pin locations and net connectivity) into the plain-JSON net
  list :func:`estimate_congestion` expects. Each net's per-terminal
  location is approximated by the *component's own placed center*
  (`COMPONENTS ... PLACED ( x y )`) rather than that pin's exact geometric
  offset within the cell -- appropriate at this stage: this pre-check runs
  immediately after global placement/legalization, before detailed routing
  has any pin-level access-point information to offer, and the offset
  itself is sub-track-pitch, negligible against the tile granularity this
  estimator operates at (see `native/congestion/src/contract.rs`'s
  `DEFAULT_TRACK_PITCH_UM`).
- :func:`estimate_congestion` -- the thin JSON-in/JSON-out wrapper around
  the `klt_congestion_native` Rust extension (`native/congestion/`), this
  repo's second native component after `native/mom/`. All of the actual
  numerics (RSMT-family per-net length estimate, RUDY-style demand-grid
  distribution) live there; see that crate's own docs for the algorithm.
"""

from __future__ import annotations

import re
from typing import Any

from ._native import _load_native_extension

SCHEMA_VERSION = 1


class CongestionError(Exception):
    """Raised when the congestion pre-check cannot run: a malformed/missing
    DEF, a request the native extension rejects, or the native extension
    not being built.
    """


def _load_native() -> Any:
    return _load_native_extension(
        "klt_congestion_native",
        CongestionError,
        "native/congestion/",
        "uv sync --group congestion",
    )


def estimate_congestion(
    *,
    die_box_um: dict[str, float],
    grid_cols: int,
    grid_rows: int,
    nets: list[dict[str, Any]],
    layer_count: float | None = None,
    track_pitch_um: float | None = None,
) -> dict[str, Any]:
    """Run the RUDY/RSMT-family congestion pre-check over `nets`.

    ``die_box_um``: ``{"x0_um": .., "y0_um": .., "x1_um": .., "y1_um": ..}``
    -- the region the demand grid covers (the core area is the natural
    choice; see :func:`nets_from_def`'s return value).

    ``nets``: ``[{"name": .., "pins": [{"x_um": .., "y_um": ..}, ...]}, ...]``.

    Returns the native extension's response dict verbatim (see
    ``native/congestion/src/contract.rs``'s ``CongestionResponse`` for the
    full field list: ``congestion_score``, ``overflow_tile_count``/
    ``overflow_tile_fraction``, ``mean``/``max``/``p95_density_um_per_um2``,
    ``total_rsmt_length_um``, plus the resolved grid/capacity parameters).
    """
    import json

    native = _load_native()
    request: dict[str, Any] = {
        "die_box_um": die_box_um,
        "grid_cols": grid_cols,
        "grid_rows": grid_rows,
        "nets": nets,
    }
    if layer_count is not None:
        request["layer_count"] = layer_count
    if track_pitch_um is not None:
        request["track_pitch_um"] = track_pitch_um

    try:
        response_json = native.estimate_congestion_json(json.dumps(request))
    except ValueError as exc:
        raise CongestionError(str(exc)) from exc
    return json.loads(response_json)  # type: ignore[no-any-return]


# --------------------------------------------------------------------------- #
# Placement-stage DEF -> net list
# --------------------------------------------------------------------------- #

#: ``UNITS DISTANCE MICRONS <n> ;`` -- the DEF database-unit scale (DEF
#: coordinates are integers in 1/n micron units). Every real DEF this repo
#: writes (`place_and_route.py`'s own `write_def`) sets this; a DEF missing
#: it is malformed, not just unusual.
_UNITS_RE = re.compile(r"UNITS\s+DISTANCE\s+MICRONS\s+(\d+)\s*;")

#: ``DIEAREA ( x0 y0 ) ( x1 y1 ) ;`` -- DEF's DIEAREA is technically an
#: arbitrary rectilinear polygon (more than 2 points for a non-rectangular
#: die), but every shape `klt place-and-route` itself generates
#: (`_floorplan_init_lines`) is a plain rectangle -- this reads the first
#: and last point pair as the bounding box, which is exact for a rectangle
#: and a conservative (over-)approximation for anything else.
_DIEAREA_RE = re.compile(r"DIEAREA\s+((?:\(\s*-?\d+\s+-?\d+\s*\)\s*)+);")
_POINT_RE = re.compile(r"\(\s*(-?\d+)\s+(-?\d+)\s*\)")

#: One `COMPONENTS` entry: ``- <name> <cell> ... PLACED|FIXED ( x y ) <orient> ...;``
#: Deliberately does not require `PLACED`/`FIXED` to be the *last* token
#: before `;` (a placed component may carry additional `+` properties after
#: its location) -- just that the location appears somewhere in the entry.
#: A component with neither `PLACED` nor `FIXED` (still `UNPLACED`) has no
#: usable location and is skipped -- see `_parse_components`'s own handling.
_COMPONENT_RE = re.compile(r"^\s*-\s+(\S+)\s+(\S+)(.*?);", re.MULTILINE | re.DOTALL)
_COMPONENT_LOCATION_RE = re.compile(r"(?:PLACED|FIXED)\s*\(\s*(-?\d+)\s+(-?\d+)\s*\)")

#: One `PINS` entry (top-level IO): ``- <name> ... PLACED|FIXED ( x y ) <orient> ;``
#: -- multi-line in practice (`place_and_route.py`'s own written DEF wraps
#: each PIN's `+ PORT`/`+ LAYER`/`+ PLACED` clauses onto separate lines), so
#: this matches across newlines up to the terminating `;`, mirroring
#: `_COMPONENT_RE`.
_PIN_RE = re.compile(r"^\s*-\s+(\S+)(.*?);", re.MULTILINE | re.DOTALL)
_PIN_LOCATION_RE = _COMPONENT_LOCATION_RE

#: One `NETS` entry: ``- <net_name> ( <ref> <pin> ) ( <ref> <pin> ) ... ;``
#: where ``<ref>`` is either a component instance name or the literal
#: ``PIN`` (a top-level IO connection, DEF's own convention). Multi-line in
#: general (a high-fanout net can wrap), so this matches across newlines.
_NET_RE = re.compile(r"^\s*-\s+(\S+)\s+((?:\(.*?\)\s*)+)\+", re.MULTILINE | re.DOTALL)
_NET_CONN_RE = re.compile(r"\(\s*(\S+)\s+(\S+)\s*\)")


def _section(text: str, name: str) -> str | None:
    """Extract the body of a top-level ``NAME <count> ;  ... END NAME``
    section (``COMPONENTS``, ``PINS``, ``NETS``), or ``None`` if absent.
    ``NETS``/``SPECIALNETS`` share the ``END NETS`` closer's own prefix, so
    the section name is matched as a whole word at both ends to avoid
    ``NETS`` matching inside ``SPECIALNETS``.
    """
    begin = re.search(rf"(?<![A-Z])\b{name}\s+\d+\s*;", text)
    if begin is None:
        return None
    end = re.search(rf"\bEND\s+{name}\b", text[begin.end() :])
    if end is None:
        raise CongestionError(f"DEF section '{name}' has no matching 'END {name}'")
    return text[begin.end() : begin.end() + end.start()]


def _parse_scale(text: str) -> float:
    match = _UNITS_RE.search(text)
    if match is None:
        raise CongestionError("DEF has no 'UNITS DISTANCE MICRONS <n> ;' line")
    return float(match.group(1))


def _parse_die_box_um(text: str, scale: float) -> dict[str, float]:
    match = _DIEAREA_RE.search(text)
    if match is None:
        raise CongestionError("DEF has no 'DIEAREA' statement")
    points = [
        (int(x) / scale, int(y) / scale) for x, y in _POINT_RE.findall(match.group(1))
    ]
    if len(points) < 2:
        raise CongestionError("DEF 'DIEAREA' statement has fewer than 2 points")
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return {
        "x0_um": min(xs),
        "y0_um": min(ys),
        "x1_um": max(xs),
        "y1_um": max(ys),
    }


def _parse_components(text: str, scale: float) -> dict[str, tuple[float, float]]:
    section = _section(text, "COMPONENTS")
    if section is None:
        raise CongestionError("DEF has no 'COMPONENTS' section")
    locations: dict[str, tuple[float, float]] = {}
    for match in _COMPONENT_RE.finditer(section):
        name = match.group(1)
        loc = _COMPONENT_LOCATION_RE.search(match.group(3))
        if loc is None:
            # UNPLACED component -- no usable location, skip (this net
            # terminal is simply omitted; a net entirely made of unplaced
            # components degenerates to <2 usable pins and is skipped by
            # the native extension, reported in skipped_single_pin_nets).
            continue
        locations[name] = (int(loc.group(1)) / scale, int(loc.group(2)) / scale)
    return locations


def _parse_pins(text: str, scale: float) -> dict[str, tuple[float, float]]:
    section = _section(text, "PINS")
    if section is None:
        # A DEF with no top-level PINS section is unusual but not invalid
        # for this purpose (e.g. a block with no IO) -- every net's
        # terminals then come entirely from COMPONENTS.
        return {}
    locations: dict[str, tuple[float, float]] = {}
    for match in _PIN_RE.finditer(section):
        name = match.group(1)
        loc = _PIN_LOCATION_RE.search(match.group(0))
        if loc is None:
            continue
        locations[name] = (int(loc.group(1)) / scale, int(loc.group(2)) / scale)
    return locations


def nets_from_def(def_path: str) -> tuple[list[dict[str, Any]], dict[str, float]]:
    """Read a placement-stage DEF into ``(nets, die_box_um)``, ready to pass
    to :func:`estimate_congestion`.

    ``nets`` is ``[{"name": .., "pins": [{"x_um": .., "y_um": ..}, ...]}, ...]``
    -- one entry per `NETS` block entry, terminal locations resolved via
    each connection's referenced `COMPONENTS` placement (or the top-level
    `PINS` location for a `( PIN <name> )` IO connection). A connection
    naming a component this DEF never placed (`UNPLACED`, or simply absent
    -- both defensive cases, not expected against a real OpenROAD-written
    DEF) is silently dropped from that net's terminal list rather than
    raising, matching :func:`_parse_components`'s own skip.

    Raises :class:`CongestionError` for a DEF missing any of the three
    required sections (`COMPONENTS`/`NETS`) or the `UNITS`/`DIEAREA`
    header lines every `place_and_route.py`-written DEF carries.
    """
    try:
        with open(def_path) as f:
            text = f.read()
    except OSError as exc:
        raise CongestionError(f"could not read DEF '{def_path}': {exc}") from exc

    scale = _parse_scale(text)
    die_box_um = _parse_die_box_um(text, scale)
    component_locations = _parse_components(text, scale)
    pin_locations = _parse_pins(text, scale)

    nets_section = _section(text, "NETS")
    if nets_section is None:
        raise CongestionError(f"DEF '{def_path}' has no 'NETS' section")

    nets: list[dict[str, Any]] = []
    for match in _NET_RE.finditer(nets_section):
        name = match.group(1)
        pins: list[dict[str, float]] = []
        for ref, pin in _NET_CONN_RE.findall(match.group(2)):
            if ref == "PIN":
                loc = pin_locations.get(pin)
            else:
                loc = component_locations.get(ref)
            if loc is None:
                continue
            pins.append({"x_um": loc[0], "y_um": loc[1]})
        nets.append({"name": name, "pins": pins})

    return nets, die_box_um

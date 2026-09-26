"""Define ``klt characterize``: SPICE-driven NLDM timing characterization of
one standard cell at one PVT corner (issue #2502, split from #2498).

``klt`` could already *consume* a Liberty model from three verbs
(``synthesize``, ``place-and-route``, ``post_route_sta``, all through
``pdk_cells.resolve_liberty_for_cell_library``) and ``native/statime`` could
*parse* one, but nothing in the tree produced one -- characterization was
the last stage of the cell flow with no open answer. This module is that
answer's first increment: one cell, one corner, delay and transition time
only.

Pure library: :func:`run_characterize` returns plain Python data (a
JSON-serialisable ``dict``) and never prints -- serialisation and
human-readable formatting live in ``cli/characterize_cmd.py``, matching
every other ``klt`` verb.

**How it works.** The whole input-slew x output-load grid is measured in
**one** ``klt sim`` request, not one request per point: the generated
testbench instantiates the cell once per (arc, slew, load) triple, each
instance with its own private ramp source and load capacitor, and a single
``tran`` analysis drives every instance's input high and then low again. One
``.meas`` card per (grid point, table) reads the four NLDM quantities out of
that run. This matters for more than tidiness -- a per-point request would
be an N-unit sweep, which on a shared host is exactly the
hand-rolled-simulation-grid pattern ``klt sim``'s corner matrix exists to
replace; as written, a 7x7 grid of a two-input cell is a single-corner,
single-unit ``local``-backend run.

Scope of this increment -- what the emitted ``.lib`` deliberately does
**not** contain (issue #2503 owns the first two):

- no power/energy tables (``rise_power``/``fall_power``/``leakage_power``);
- one cell per invocation (no multi-cell batch mode), and one corner per
  invocation (no multi-corner ``.lib``);
- no sequential-cell constraint arcs (``setup``/``hold``/``recovery``): a
  cell whose output ``function`` references anything that is not a declared
  input pin is refused with a named error, never characterized as if it were
  combinational (see :mod:`klayout_tools.characterize_arcs`);
- no measured pin capacitance -- ``pins[].capacitance_pf`` is echoed from
  the request when given and the attribute is omitted when it is not, rather
  than fabricated.

The emitted file is checked against ``native/statime``'s own Liberty reader
before the run reports success (:func:`roundtrip_check`), so "it parses"
is a property of every run rather than only of this module's tests. When the
``klt_statime_native`` extension is not installed the check reports
``status: "skipped"`` with the reason -- never a fabricated pass.

See ``docs/cli/characterize.md`` for the request/response contract.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from . import liberty_writer, sim
from ._paths import _load_request_json, _resolve_relative
from ._provenance import INPUT_ROLE_NETLIST, build_provenance
from .characterize_arcs import DerivedArc, FunctionError, derive_arcs
from .env_provenance import repo_relative_path
from .pdk import PdkNotFoundError, find_pdk

#: Version of this command's JSON response shape (per-command, per
#: ``docs/json-contract.md``).
SCHEMA_VERSION = 1

#: Name of the native extension whose Liberty reader the round-trip check
#: runs the emitted ``.lib`` through.
ROUNDTRIP_ENGINE = "klt_statime_native"

#: Default directory (relative to the request file) for the generated
#: testbench, the generated ``klt sim`` request, the raw sim report, and the
#: emitted ``.lib`` -- the same "next to the input" convention ``klt sim``'s
#: own ``artifacts_dir`` and ``klt render``'s output directory use.
DEFAULT_OUTDIR_NAME = os.path.join(".klt", "characterize")

#: Fraction of the settle window a measured (delay + transition) may occupy
#: before the run is refused. A grid point whose output is still moving when
#: the next input edge arrives produces a number that looks plausible and is
#: wrong, so this is a hard error with an actionable message rather than a
#: warning -- see :func:`_check_settle_margin`.
_SETTLE_MARGIN_FRACTION = 0.9

#: Default per-corner ngspice timeout (s) for the generated sim request.
DEFAULT_TIMEOUT_S = 900.0

_SUBCKT_RE = re.compile(r"^\s*\.subckt\s+(\S+)((?:\s+\S+)*)\s*$", re.IGNORECASE)

#: The four NLDM tables, and the measurement-name suffix each is read from.
_TABLES = (
    ("cell_rise", "dr"),
    ("cell_fall", "df"),
    ("rise_transition", "tr"),
    ("fall_transition", "tf"),
)

#: The two NLDM internal-energy tables every arc also carries (issue #2503).
POWER_TABLES = ("rise_power", "fall_power")

#: Every per-arc table in the response, in report order.
TABLE_NAMES = tuple(name for name, _ in _TABLES) + POWER_TABLES


class CharacterizeError(Exception):
    """Raised for any failure to produce a characterization: a malformed
    request, an unresolvable/unparseable cell netlist, pin metadata that does
    not describe a combinational cell, a simulation that did not complete, a
    grid point whose ``.meas`` card produced no value, or an emitted ``.lib``
    that the round-trip reader rejects.

    Deliberately a single error type with a specific message, mirroring
    ``SimError``/``PostRouteStaError``: the CLI turns it into the documented
    error envelope (exit ``1``).
    """


def run_characterize(
    request_path: str,
    *,
    outdir: str | None = None,
    lib_path: str | None = None,
    backend: str | None = None,
    keep_artifacts: bool | None = None,
    cells: list[str] | tuple[str, ...] | None = None,
    compare_to: str | None = None,
) -> dict[str, Any]:
    """Characterize one cell -- or a batch of cells -- at one corner and emit
    one NLDM ``.lib`` covering all of them.

    ``request_path`` is a ``klt characterize`` request document (see
    ``docs/cli/characterize.md``). Paths inside it resolve against the
    request file's own directory, as in every other request-taking verb. It
    carries either ``cell`` (one cell, issue #2502's form) or ``cells`` (an
    array of the same objects, issue #2503's batch mode); every cell shares
    the request's one ``corner``/``grid``/``thresholds``/``models``/
    ``options``, which is what lets their tables live in one library.

    ``cells`` restricts a batch request to the named subset (the CLI's
    repeatable ``--cell``). ``compare_to`` names a reference Liberty file
    (typically the vendor library for the same corner) to compare the
    emitted one against arc by arc -- see
    :mod:`klayout_tools.liberty_compare`; the result lands in the response's
    ``comparison`` block (``null`` when not asked for).

    ``outdir`` overrides where the generated testbench / sim request / sim
    report / ``.lib`` are written (default: a ``.klt/characterize/``
    directory next to the request file; batch mode writes each cell's
    simulation artifacts under ``<outdir>/cells/<cell>/``). ``lib_path``
    overrides the emitted Liberty path (default: ``<outdir>/<library
    name>.lib``, or the request's own ``output.lib``). ``backend``/
    ``keep_artifacts`` are forwarded to ``klt sim``.

    Raises :class:`CharacterizeError` for every failure mode; never returns
    a partially-populated table, and a batch run in which any one cell fails
    emits nothing (the error names the cell).
    """
    request = _load_request(request_path)
    request_dir = os.path.dirname(os.path.abspath(request_path)) or os.getcwd()
    batch = "cells" in request
    if cells and not batch:
        raise CharacterizeError(
            "--cell selects cells out of a batch request's `cells` array; this "
            "request uses the single-cell `cell` form"
        )

    corner = _resolve_corner(request)
    grid = _resolve_grid(request)
    thresholds = _resolve_thresholds(request)
    options = _resolve_options(request, grid, thresholds, request_dir)

    # Resolve *every* cell before simulating *any*: a typo in the fifth
    # cell's pin list must not cost the first four cells' simulation time.
    resolved: list[tuple[dict[str, Any], tuple[DerivedArc, ...]]] = []
    for where, spec in _cell_entries(request, tuple(cells) if cells else None):
        cell = _resolve_cell_spec(spec, request_dir, where)
        if batch:
            arcs = _resolve_arcs_for(spec.get("arcs"), cell, f"{where}.arcs")
        else:
            arcs = _resolve_arcs(request, cell)
        resolved.append((cell, arcs))

    work_dir = _resolve_outdir(outdir, request, request_dir)
    os.makedirs(work_dir, exist_ok=True)

    results: list[dict[str, Any]] = []
    for cell, arcs in resolved:
        cell_dir = (
            os.path.join(work_dir, "cells", _slug(cell["name"])) if batch else work_dir
        )
        try:
            results.append(
                _characterize_cell(
                    cell=cell,
                    arcs=arcs,
                    corner=corner,
                    grid=grid,
                    thresholds=thresholds,
                    options=options,
                    request=request,
                    request_dir=request_dir,
                    work_dir=cell_dir,
                    backend=backend,
                    keep_artifacts=keep_artifacts,
                )
            )
        except CharacterizeError as exc:
            if not batch:
                raise
            raise CharacterizeError(f"cell '{cell['name']}': {exc}") from exc

    library = _build_library(
        request=request,
        results=results,
        corner=corner,
        grid=grid,
        thresholds=thresholds,
        request_path=request_path,
        batch=batch,
    )
    emitted_path = _resolve_lib_path(lib_path, request, request_dir, work_dir, library)
    try:
        lib_text = liberty_writer.render_library(library)
    except liberty_writer.LibertyWriteError as exc:
        raise CharacterizeError(f"could not emit Liberty: {exc}") from exc
    with open(emitted_path, "w", encoding="utf-8") as handle:
        handle.write(lib_text)

    roundtrip = roundtrip_check_cells(
        lib_path=emitted_path,
        cells=[
            (
                result["cell"]["name"],
                result["cell"]["input_pins"],
                result["cell"]["output_pins"],
            )
            for result in results
        ],
        work_dir=work_dir,
    )
    if roundtrip["status"] == "fail":
        raise CharacterizeError(
            f"the emitted Liberty at '{emitted_path}' does not parse through "
            f"{ROUNDTRIP_ENGINE}'s reader: {roundtrip['message']}"
        )

    comparison = None
    if compare_to is not None:
        from . import liberty_compare  # local: only needed when asked for

        try:
            comparison = liberty_compare.compare_libraries(
                emitted_path,
                os.path.abspath(compare_to),
                cells=[result["cell"]["name"] for result in results],
            )
        except liberty_compare.LibertyCompareError as exc:
            raise CharacterizeError(
                f"could not compare against '{compare_to}': {exc}"
            ) from exc

    return _build_response(
        request_path=request_path,
        grid=grid,
        corner=corner,
        thresholds=thresholds,
        options=options,
        results=results,
        library=library,
        emitted_path=emitted_path,
        roundtrip=roundtrip,
        comparison=comparison,
        request=request,
        batch=batch,
    )


def _characterize_cell(
    *,
    cell: dict[str, Any],
    arcs: tuple[DerivedArc, ...],
    corner: dict[str, Any],
    grid: dict[str, tuple[float, ...]],
    thresholds: liberty_writer.Thresholds,
    options: dict[str, Any],
    request: dict[str, Any],
    request_dir: str,
    work_dir: str,
    backend: str | None,
    keep_artifacts: bool | None,
) -> dict[str, Any]:
    """Run one cell's whole grid (timing, power, and leakage) as a single
    ``klt sim`` request and reshape the results into tables.

    This is #2502's single-cell mechanism unchanged in structure; batch mode
    is this function in a loop, each cell in its own ``work_dir``.
    """
    os.makedirs(work_dir, exist_ok=True)
    plan = _build_stimulus_plan(
        cell=cell,
        corner=corner,
        grid=grid,
        thresholds=thresholds,
        options=options,
        arcs=arcs,
    )
    testbench_path = os.path.join(work_dir, "testbench.spice")
    with open(testbench_path, "w", encoding="utf-8") as handle:
        handle.write(plan["netlist_text"])

    sim_request = _build_sim_request(
        request=request,
        request_dir=request_dir,
        testbench_path=testbench_path,
        corner=corner,
        plan=plan,
        options=options,
        keep_artifacts=keep_artifacts,
    )
    sim_request_path = os.path.join(work_dir, "sim-request.json")
    with open(sim_request_path, "w", encoding="utf-8") as handle:
        json.dump(sim_request, handle, indent=2)
        handle.write("\n")

    try:
        sim_report = sim.run_sim(
            sim_request_path,
            artifacts_dir=os.path.join(work_dir, "sim"),
            backend=backend,
        )
    except sim.SimError as exc:
        raise CharacterizeError(f"simulation failed: {exc}") from exc

    sim_report_path = os.path.join(work_dir, "sim-report.json")
    with open(sim_report_path, "w", encoding="utf-8") as handle:
        json.dump(sim_report, handle, indent=2)
        handle.write("\n")

    values = _extract_measurements(sim_report, sim_report_path)
    tables = _build_tables(plan, grid, values, thresholds)
    _check_settle_margin(plan, grid, tables, options)
    power_tables = _build_power_tables(plan, grid, values, corner)
    leakage = _build_leakage(plan, values, corner)

    return {
        "cell": cell,
        "arcs": arcs,
        "plan": plan,
        "tables": tables,
        "power_tables": power_tables,
        "leakage": leakage,
        "testbench_path": testbench_path,
        "sim_request_path": sim_request_path,
        "sim_report_path": sim_report_path,
        "sim_report": sim_report,
    }


# --------------------------------------------------------------------------- #
# Request parsing
# --------------------------------------------------------------------------- #


def _load_request(request_path: str) -> dict[str, Any]:
    data = _load_request_json(request_path, CharacterizeError)
    if not isinstance(data, dict):
        raise CharacterizeError(
            f"request '{request_path}' must be a JSON object, not {type(data).__name__}"
        )
    # Exactly one of `cell` (the single-cell form, issue #2502) and `cells`
    # (batch mode, issue #2503) -- both, or neither, is ambiguous.
    has_cell = "cell" in data
    has_cells = "cells" in data
    if has_cell and has_cells:
        raise CharacterizeError(
            "request.cell and request.cells are mutually exclusive: use `cell` "
            "for one cell or `cells` (an array of the same objects) for a batch"
        )
    if not has_cell and not has_cells:
        raise CharacterizeError(
            "request.cell is required (or request.cells, an array of cell "
            "objects, to characterize several cells into one `.lib`)"
        )
    if has_cell and not isinstance(data["cell"], dict):
        raise CharacterizeError("request.cell must be an object")
    if has_cells:
        cells = data["cells"]
        if not isinstance(cells, list) or not cells:
            raise CharacterizeError(
                "request.cells must be a non-empty array of cell objects"
            )
        for index, entry in enumerate(cells):
            if not isinstance(entry, dict):
                raise CharacterizeError(f"request.cells[{index}] must be an object")
        if "arcs" in data:
            raise CharacterizeError(
                "request.arcs is the single-cell override; in batch mode put "
                "an `arcs` array on the request.cells[] entry it applies to"
            )
    for field in ("corner", "grid"):
        if field not in data:
            raise CharacterizeError(f"request.{field} is required")
        if not isinstance(data[field], dict):
            raise CharacterizeError(f"request.{field} must be an object")
    return data


def _cell_entries(
    request: dict[str, Any], selected: tuple[str, ...] | None
) -> list[tuple[str, dict[str, Any]]]:
    """Return ``(field_prefix, cell_spec)`` for every cell this run covers.

    The single-cell form yields one entry whose error-message prefix is
    ``request.cell`` (unchanged from #2502); batch mode yields one per
    ``request.cells[i]``, filtered to ``selected`` names when given (the
    CLI's repeatable ``--cell``). A selected name the request does not
    declare is an error, never a silent no-op -- "characterize these
    cells" that quietly characterizes fewer is exactly the kind of
    under-delivery a caller cannot see.
    """
    if "cell" in request:
        entries = [("request.cell", request["cell"])]
    else:
        entries = [
            (f"request.cells[{index}]", spec)
            for index, spec in enumerate(request["cells"])
        ]
    names = [spec.get("name") for _, spec in entries]
    duplicates = sorted(
        {name for name in names if isinstance(name, str) and names.count(name) > 1}
    )
    if duplicates:
        raise CharacterizeError(
            f"request.cells declares {', '.join(duplicates)} more than once -- "
            "a Liberty library holds one cell() group per name"
        )
    if selected:
        unknown = [name for name in selected if name not in names]
        if unknown:
            raise CharacterizeError(
                f"--cell names {', '.join(unknown)}, which the request does not "
                f"declare (declared: {', '.join(str(name) for name in names)})"
            )
        wanted = set(selected)
        entries = [entry for entry in entries if entry[1].get("name") in wanted]
    return entries


def _resolve_cell(request: dict[str, Any], request_dir: str) -> dict[str, Any]:
    """Resolve the single-cell form's ``request.cell`` (kept for callers of
    the #2502 shape; batch mode goes through :func:`_resolve_cell_spec`)."""
    return _resolve_cell_spec(request["cell"], request_dir, "request.cell")


def _resolve_cell_spec(
    spec: dict[str, Any], request_dir: str, where: str
) -> dict[str, Any]:
    name = spec.get("name")
    if not isinstance(name, str) or not name.strip():
        raise CharacterizeError(f"{where}.name is required (the cell's name)")
    netlist = spec.get("netlist")
    if not isinstance(netlist, str) or not netlist.strip():
        raise CharacterizeError(
            f"{where}.netlist is required (a SPICE file defining the "
            "cell's .subckt -- post-extraction where available)"
        )
    netlist_path = _resolve_relative(netlist, request_dir)
    if not os.path.isfile(netlist_path):
        raise CharacterizeError(f"cell netlist not found: {netlist_path}")

    subckt = spec.get("subckt") or name
    terminals = _read_subckt_terminals(netlist_path, subckt)

    pins = _resolve_pins(spec, subckt, where)
    power = _resolve_power_pins(spec, terminals, pins, where)

    declared = {pin["name"] for pin in pins} | set(power.values())
    missing = [terminal for terminal in terminals if terminal not in declared]
    if missing:
        raise CharacterizeError(
            f"subcircuit '{subckt}' declares terminal(s) "
            f"{', '.join(missing)} that {where}.pins / "
            f"{where}.power_pins do not account for -- every terminal "
            "must be either a signal pin or a named supply, so the generated "
            "testbench cannot leave one floating"
        )
    unknown = [pin["name"] for pin in pins if pin["name"] not in terminals]
    if unknown:
        raise CharacterizeError(
            f"{where}.pins names {', '.join(unknown)}, which "
            f"subcircuit '{subckt}' does not declare as a terminal"
        )

    return {
        "name": name,
        "subckt": subckt,
        "netlist_path": netlist_path,
        "terminals": terminals,
        "pins": pins,
        "power_pins": power,
        "input_pins": tuple(pin["name"] for pin in pins if pin["direction"] == "input"),
        "output_pins": tuple(
            pin["name"] for pin in pins if pin["direction"] == "output"
        ),
        "area": spec.get("area"),
        "where": where,
    }


def _resolve_pins(
    spec: dict[str, Any], subckt: str, where: str = "request.cell"
) -> tuple[dict[str, Any], ...]:
    raw = spec.get("pins")
    if not isinstance(raw, list) or not raw:
        raise CharacterizeError(
            f"{where}.pins is required: an array of "
            '{"name", "direction"} objects (output pins additionally need '
            '"function", the cell\'s Liberty boolean expression)'
        )
    pins: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise CharacterizeError(f"{where}.pins[{index}] must be an object")
        pin_name = entry.get("name")
        if not isinstance(pin_name, str) or not pin_name.strip():
            raise CharacterizeError(f"{where}.pins[{index}].name is required")
        if pin_name in seen:
            raise CharacterizeError(
                f"{where}.pins declares '{pin_name}' more than once"
            )
        seen.add(pin_name)
        direction = entry.get("direction")
        if direction not in ("input", "output"):
            raise CharacterizeError(
                f'{where}.pins[{index}].direction must be "input" or '
                f'"output" (got {direction!r}); `inout` pins are out of scope '
                "for this command"
            )
        function = entry.get("function")
        if direction == "output" and (
            not isinstance(function, str) or not function.strip()
        ):
            raise CharacterizeError(
                f"{where}.pins[{index}] ('{pin_name}') is an output pin "
                "and needs a Liberty `function` expression -- that is what "
                "the timing arcs and their side-input states are derived from"
            )
        pins.append(
            {
                "name": pin_name,
                "direction": direction,
                "function": function if direction == "output" else None,
                "capacitance_pf": _optional_positive(
                    entry.get("capacitance_pf"),
                    f"{where}.pins[{index}].capacitance_pf",
                ),
            }
        )
    if not any(pin["direction"] == "input" for pin in pins):
        raise CharacterizeError(
            f"subcircuit '{subckt}' has no input pin declared in "
            f"{where}.pins -- there is nothing to drive"
        )
    if not any(pin["direction"] == "output" for pin in pins):
        raise CharacterizeError(
            f"subcircuit '{subckt}' has no output pin declared in "
            f"{where}.pins -- there is nothing to measure"
        )
    return tuple(pins)


def _resolve_power_pins(
    spec: dict[str, Any],
    terminals: tuple[str, ...],
    pins: tuple[dict[str, Any], ...],
    where: str = "request.cell",
) -> dict[str, str]:
    raw = spec.get("power_pins")
    if raw is None:
        signal = {pin["name"] for pin in pins}
        remaining = [terminal for terminal in terminals if terminal not in signal]
        raise CharacterizeError(
            f"{where}.power_pins is required: "
            '{"vdd": "<terminal>", '
            '"gnd": "<terminal>"} naming which subcircuit terminals are the '
            "supply rails"
            + (
                f" (unaccounted-for terminals: {', '.join(remaining)})"
                if remaining
                else ""
            )
        )
    if not isinstance(raw, dict):
        raise CharacterizeError(f"{where}.power_pins must be an object")
    resolved: dict[str, str] = {}
    for key in ("vdd", "gnd"):
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            raise CharacterizeError(f"{where}.power_pins.{key} is required")
        if value not in terminals:
            raise CharacterizeError(
                f"{where}.power_pins.{key} names '{value}', which is not "
                "a terminal of the cell's subcircuit"
            )
        resolved[key] = value
    for extra_key, extra_value in raw.items():
        if extra_key in ("vdd", "gnd"):
            continue
        if not isinstance(extra_value, str) or extra_value not in terminals:
            raise CharacterizeError(
                f"{where}.power_pins.{extra_key} must name a terminal of "
                "the cell's subcircuit"
            )
        resolved[extra_key] = extra_value
    return resolved


def _read_subckt_terminals(netlist_path: str, subckt: str) -> tuple[str, ...]:
    """Return ``subckt``'s terminal names, in declaration order.

    The order is load-bearing: a SPICE ``X`` instance line is positional, so
    the generated testbench binds nodes by the position each terminal
    appears in here, never by the order the request happens to list pins in.
    """
    try:
        with open(netlist_path, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError as exc:
        raise CharacterizeError(f"could not read '{netlist_path}': {exc}") from exc

    folded: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("+") and folded:
            folded[-1] = folded[-1] + " " + stripped[1:].strip()
            continue
        folded.append(line)

    for line in folded:
        match = _SUBCKT_RE.match(line.split("$")[0])
        if match is None:
            continue
        if match.group(1) != subckt:
            continue
        terminals = tuple(
            token
            for token in match.group(2).split()
            if "=" not in token and token.lower() != "params:"
        )
        if not terminals:
            raise CharacterizeError(
                f"subcircuit '{subckt}' in '{netlist_path}' declares no terminals"
            )
        return terminals
    raise CharacterizeError(
        f"no `.subckt {subckt}` found in '{netlist_path}' -- set "
        "the cell's `subckt` field if the subcircuit name differs from "
        "its `name`"
    )


def _resolve_corner(request: dict[str, Any]) -> dict[str, Any]:
    spec = request["corner"]
    name = spec.get("name")
    if not isinstance(name, str) or not name.strip():
        raise CharacterizeError(
            "request.corner.name is required (the corner label the emitted "
            "`.lib` reports as its operating_conditions)"
        )
    supply_v = spec.get("supply_v")
    if not isinstance(supply_v, (int, float)) or isinstance(supply_v, bool):
        raise CharacterizeError("request.corner.supply_v is required (volts)")
    if supply_v <= 0:
        raise CharacterizeError("request.corner.supply_v must be positive")
    temperature_c = spec.get("temperature_c")
    if not isinstance(temperature_c, (int, float)) or isinstance(temperature_c, bool):
        raise CharacterizeError("request.corner.temperature_c is required (Celsius)")
    process = spec.get("process")
    if process is not None and not isinstance(process, str):
        raise CharacterizeError(
            "request.corner.process must be a model-library section name "
            '(e.g. "mos_tt") or omitted'
        )
    nom_process = spec.get("nom_process", 1.0)
    if not isinstance(nom_process, (int, float)) or isinstance(nom_process, bool):
        raise CharacterizeError("request.corner.nom_process must be a number")
    return {
        "name": name,
        "process": process,
        "supply_v": float(supply_v),
        "temperature_c": float(temperature_c),
        "nom_process": float(nom_process),
    }


def _resolve_grid(request: dict[str, Any]) -> dict[str, tuple[float, ...]]:
    spec = request["grid"]
    return {
        "input_transition_ns": _resolve_axis(
            spec.get("input_transition_ns"), "request.grid.input_transition_ns"
        ),
        "output_load_pf": _resolve_axis(
            spec.get("output_load_pf"), "request.grid.output_load_pf"
        ),
    }


def _resolve_axis(raw: Any, field: str) -> tuple[float, ...]:
    if not isinstance(raw, list) or not raw:
        raise CharacterizeError(
            f"{field} is required: a non-empty, strictly ascending array of grid values"
        )
    values: list[float] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, (int, float)) or isinstance(entry, bool):
            raise CharacterizeError(f"{field}[{index}] must be a number")
        if entry <= 0:
            raise CharacterizeError(f"{field}[{index}] must be positive")
        values.append(float(entry))
    if any(
        later <= earlier for earlier, later in zip(values, values[1:], strict=False)
    ):
        raise CharacterizeError(
            f"{field} must be strictly ascending -- an NLDM index axis is "
            "interpolated over, so a repeated or out-of-order entry silently "
            "corrupts every lookup against it"
        )
    return tuple(values)


def _resolve_thresholds(request: dict[str, Any]) -> liberty_writer.Thresholds:
    spec = request.get("thresholds")
    if spec is None:
        return liberty_writer.Thresholds()
    if not isinstance(spec, dict):
        raise CharacterizeError("request.thresholds must be an object")
    defaults = liberty_writer.Thresholds()
    resolved: dict[str, float] = {}
    for field_name in defaults.__dataclass_fields__:
        value = spec.get(field_name, getattr(defaults, field_name))
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise CharacterizeError(f"request.thresholds.{field_name} must be a number")
        resolved[field_name] = float(value)
    unknown = sorted(set(spec) - set(defaults.__dataclass_fields__))
    if unknown:
        raise CharacterizeError(
            f"request.thresholds has unknown field(s): {', '.join(unknown)}"
        )
    for edge in ("rise", "fall"):
        lower = resolved[f"slew_lower_threshold_pct_{edge}"]
        upper = resolved[f"slew_upper_threshold_pct_{edge}"]
        if not 0.0 < lower < upper < 100.0:
            raise CharacterizeError(
                f"request.thresholds slew_lower/upper_threshold_pct_{edge} "
                f"must satisfy 0 < lower < upper < 100 (got {lower}, {upper})"
            )
    if resolved["slew_derate_from_library"] <= 0:
        raise CharacterizeError(
            "request.thresholds.slew_derate_from_library must be positive"
        )
    return liberty_writer.Thresholds(**resolved)


def _resolve_options(
    request: dict[str, Any],
    grid: dict[str, tuple[float, ...]],
    thresholds: liberty_writer.Thresholds,
    request_dir: str,
) -> dict[str, Any]:
    spec = request.get("options") or {}
    if not isinstance(spec, dict):
        raise CharacterizeError("request.options must be an object")

    ramps = [_ramp_ns(value, thresholds) for value in grid["input_transition_ns"]]
    max_ramp = max(ramps)
    min_ramp = min(ramps)

    settle_ns = spec.get("settle_ns")
    if settle_ns is None:
        # Enough room for the slowest edge to finish plus the output to
        # settle at the largest grid load. Verified after the fact by
        # `_check_settle_margin`, which turns a too-small window into a named
        # error rather than a quietly wrong table.
        settle_ns = max(2.0, 3.0 * max_ramp)
    settle_ns = _positive_number(settle_ns, "request.options.settle_ns")

    step_ns = spec.get("tran_step_ns")
    if step_ns is None:
        step_ns = min(0.001, min_ramp / 10.0)
    step_ns = _positive_number(step_ns, "request.options.tran_step_ns")

    timeout_s = _positive_number(
        spec.get("timeout_s", DEFAULT_TIMEOUT_S), "request.options.timeout_s"
    )
    keep_artifacts = spec.get("keep_artifacts", False)
    if not isinstance(keep_artifacts, bool):
        raise CharacterizeError("request.options.keep_artifacts must be a boolean")
    return {
        "settle_ns": settle_ns,
        "tran_step_ns": step_ns,
        "timeout_s": timeout_s,
        "keep_artifacts": keep_artifacts,
        "ramps_ns": tuple(ramps),
        "osdi_preload": _resolve_osdi_preload(spec, request_dir),
    }


def _resolve_osdi_preload(spec: dict[str, Any], request_dir: str) -> tuple[str, ...]:
    """Resolve ``options.osdi_preload``: OSDI shared libraries the simulation
    must ``pre_osdi`` before the circuit is parsed.

    ngspice can only instantiate a Verilog-A compact model through an OSDI
    shared library, and several open PDKs ship their core devices that way --
    IHP's ``sg13g2`` MOSFETs are PSP103 Verilog-A, compiled to
    ``libs.tech/ngspice/osdi/*.osdi`` by
    ``scripts/fetch-sg13g2-sim-toolchain.sh`` (see ``pdks/README.md``).
    Without the preload every device in the cell's netlist fails with
    ``Unable to find definition of model ...`` and the whole grid errors, so
    this is not an exotic option on those PDKs -- it is the difference
    between a run and no run.

    Delegates to ``klt sim``'s own ``options.osdi_preload`` resolver (issue
    #2513) -- same shape, same resolution rule, same up-front existence
    check -- and is called here, before any testbench is written, so a bad
    path is named against *this* request rather than the generated one. The
    resolved absolute paths are forwarded into the generated ``klt sim``
    request's ``options.osdi_preload``; the testbench itself carries no
    ``.control`` block.
    """
    try:
        return sim._resolve_osdi_preload(spec, request_dir)
    except sim.SimError as exc:
        raise CharacterizeError(str(exc)) from exc


def _resolve_arcs(
    request: dict[str, Any], cell: dict[str, Any]
) -> tuple[DerivedArc, ...]:
    """Derive every combinational arc of the cell, or validate an explicit
    ``request.arcs`` override.

    The override exists for a cell whose ``function`` this command cannot
    derive from (or a caller who wants a specific side-input state measured);
    it is not a shortcut around the pin metadata, which still has to declare
    the pins the arc names.
    """
    return _resolve_arcs_for(request.get("arcs"), cell, "request.arcs")


def _resolve_arcs_for(
    override: Any, cell: dict[str, Any], where: str
) -> tuple[DerivedArc, ...]:
    if override is not None:
        return _arcs_from_request(override, cell, where)

    arcs: list[DerivedArc] = []
    for pin in cell["pins"]:
        if pin["direction"] != "output":
            continue
        try:
            arcs.extend(
                derive_arcs(
                    output_pin=pin["name"],
                    function=pin["function"],
                    input_pins=cell["input_pins"],
                )
            )
        except FunctionError as exc:
            raise CharacterizeError(
                f"cell '{cell['name']}' pin '{pin['name']}': {exc}"
            ) from exc
    if not arcs:
        raise CharacterizeError(
            f"cell '{cell['name']}' has no combinational timing arc: no "
            "declared output pin's `function` depends on any declared input "
            "pin (a tie/constant cell has nothing to characterize)"
        )
    return tuple(arcs)


def _arcs_from_request(
    override: Any, cell: dict[str, Any], where: str = "request.arcs"
) -> tuple[DerivedArc, ...]:
    if not isinstance(override, list) or not override:
        raise CharacterizeError(f"{where} must be a non-empty array when given")
    arcs: list[DerivedArc] = []
    for index, entry in enumerate(override):
        if not isinstance(entry, dict):
            raise CharacterizeError(f"{where}[{index}] must be an object")
        output_pin = entry.get("output_pin")
        related_pin = entry.get("related_pin")
        if output_pin not in cell["output_pins"]:
            raise CharacterizeError(
                f"{where}[{index}].output_pin must name a declared "
                f"output pin (got {output_pin!r})"
            )
        if related_pin not in cell["input_pins"]:
            raise CharacterizeError(
                f"{where}[{index}].related_pin must name a declared "
                f"input pin (got {related_pin!r})"
            )
        timing_sense = entry.get("timing_sense")
        if timing_sense not in liberty_writer.TIMING_SENSES:
            raise CharacterizeError(
                f"{where}[{index}].timing_sense must be one of "
                f"{', '.join(liberty_writer.TIMING_SENSES)} (got "
                f"{timing_sense!r})"
            )
        measured_sense = entry.get("measured_sense", timing_sense)
        if measured_sense not in ("positive_unate", "negative_unate"):
            raise CharacterizeError(
                f"{where}[{index}].measured_sense must be "
                '"positive_unate" or "negative_unate" -- it says which input '
                "edge produces the rising output edge under this arc's own "
                "side-input state, so it cannot itself be non-unate"
            )
        arcs.append(
            DerivedArc(
                output_pin=output_pin,
                related_pin=related_pin,
                timing_sense=timing_sense,
                measured_sense=measured_sense,
                side_inputs=_side_inputs_from_request(
                    entry.get("side_inputs") or {},
                    cell=cell,
                    related_pin=related_pin,
                    index=index,
                    where=where,
                ),
            )
        )
    return tuple(arcs)


def _side_inputs_from_request(
    side_inputs: Any,
    *,
    cell: dict[str, Any],
    related_pin: str,
    index: int,
    where: str = "request.arcs",
) -> tuple[tuple[str, bool], ...]:
    """Validate one explicit ``request.arcs[i].side_inputs`` object."""
    if not isinstance(side_inputs, dict):
        raise CharacterizeError(
            f"{where}[{index}].side_inputs must be an object mapping "
            "pin name -> boolean"
        )
    resolved: list[tuple[str, bool]] = []
    for pin_name in sorted(side_inputs):
        if pin_name not in cell["input_pins"] or pin_name == related_pin:
            raise CharacterizeError(
                f"{where}[{index}].side_inputs names {pin_name!r}, "
                "which is not another declared input pin of this cell"
            )
        value = side_inputs[pin_name]
        if not isinstance(value, bool):
            raise CharacterizeError(
                f"{where}[{index}].side_inputs.{pin_name} must be a boolean"
            )
        resolved.append((pin_name, value))
    return tuple(resolved)


def _optional_positive(value: Any, field: str) -> float | None:
    if value is None:
        return None
    return _positive_number(value, field)


def _positive_number(value: Any, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise CharacterizeError(f"{field} must be a number")
    if value <= 0:
        raise CharacterizeError(f"{field} must be positive")
    return float(value)


# --------------------------------------------------------------------------- #
# Stimulus
# --------------------------------------------------------------------------- #


def _ramp_ns(transition_ns: float, thresholds: liberty_writer.Thresholds) -> float:
    """Rail-to-rail ramp duration whose threshold-to-threshold transition
    equals the Liberty index value ``transition_ns``.

    Liberty's ``index_1`` values are transition times measured between the
    library's own slew thresholds and scaled by
    ``slew_derate_from_library``, so recovering the ramp the stimulus must
    actually apply undoes both: divide out the derate to get the measured
    threshold-to-threshold time, then divide by the threshold span fraction
    to get the full 0->VDD ramp of a linear edge. With the defaults (20/80%,
    derate 1) that is ``index / 0.6``.
    """
    span = (
        thresholds.slew_upper_threshold_pct_rise
        - thresholds.slew_lower_threshold_pct_rise
    ) / 100.0
    return transition_ns / thresholds.slew_derate_from_library / span


def _build_stimulus_plan(
    *,
    cell: dict[str, Any],
    corner: dict[str, Any],
    grid: dict[str, tuple[float, ...]],
    thresholds: liberty_writer.Thresholds,
    options: dict[str, Any],
    arcs: tuple[DerivedArc, ...],
) -> dict[str, Any]:
    """Generate the single-deck testbench and its ``.meas`` card set.

    One cell instance per (arc, input transition, output load) triple, each
    with a private ramp source and load capacitor on a private input/output
    node pair, all fed from one supply. The window drives every input low ->
    high -> low once, so one instance yields both output edges and therefore
    all four NLDM timing quantities for its grid point.

    **Power instrumentation (issue #2503)** rides on the same instances:
    each one reaches the shared ``vdd``/``0`` rails through its own
    zero-volt ammeter pair (``Vpd<tag>``/``Vpg<tag>``), so the charge each
    rail delivers to *that* instance over *that* edge is one ``INTEG`` card
    away -- no second simulation campaign, and no cross-talk between the
    instances sharing the deck. See :func:`_build_power_tables` for how the
    four charges per point become ``rise_power``/``fall_power``.

    **Leakage** is measured on ``2**n`` extra, never-switching instances --
    one per input state, supply (and every input held high) through its own
    ammeter, low inputs tied to ground -- whose average supply current over
    the whole run is the static current of that state. Same deck, same run.
    """
    vdd = corner["supply_v"]
    ramps = options["ramps_ns"]
    settle = options["settle_ns"]
    max_ramp = max(ramps)
    rise_start = settle
    fall_start = rise_start + max_ramp + settle
    stop_ns = fall_start + max_ramp + settle

    lines = [
        "* klt characterize -- generated testbench, do not edit",
        f"* cell: {cell['name']} (.subckt {cell['subckt']})",
        f"* corner: {corner['name']}",
        "*",
        "* One instance per (arc, input transition, output load) grid point;",
        "* a single tran run drives every input low->high->low once. Every",
        "* instance reaches the rails through its own 0 V ammeter pair (Vpd*/Vpg*)",
        "* so its per-edge supply charge can be integrated; lk* instances never",
        "* switch and measure the static (leakage) current of one input state.",
    ]
    # No `.control` block here, OSDI preload or not: the testbench is a
    # circuit body per `klt sim`'s netlist convention, and any
    # `options.osdi_preload` rides the generated sim request instead (see
    # `_build_sim_request`), which `klt sim` emits as `pre_osdi` lines in the
    # `.control` block it generates itself (issue #2513).
    lines.append(f".include {cell['netlist_path']}")
    lines.append(f"Vdd vdd 0 DC {_spice_number(vdd)}")

    vdd_terminal = cell["power_pins"]["vdd"]
    gnd_terminal = cell["power_pins"]["gnd"]
    rail_nodes = {vdd_terminal: "vdd", gnd_terminal: "0"}
    for key, terminal in cell["power_pins"].items():
        if key not in ("vdd", "gnd"):
            rail_nodes.setdefault(terminal, "vdd" if "v" in key.lower() else "0")

    # Integration windows: the first input edge (rising) owns
    # [rise_start, fall_start], the second (falling) owns [fall_start, stop].
    # Each window starts before its own edge and ends after the output has
    # settled (the settle check below enforces that), so the whole switching
    # event -- short-circuit current included -- lands inside it.
    windows = {
        "RISE": (rise_start, fall_start),
        "FALL": (fall_start, stop_ns),
    }

    measurements: list[dict[str, Any]] = []
    points: list[dict[str, Any]] = []
    for arc_index, arc in enumerate(arcs):
        side_state = _full_side_state(arc, cell)
        for slew_index, transition_ns in enumerate(grid["input_transition_ns"]):
            ramp_ns = ramps[slew_index]
            for load_index, load_pf in enumerate(grid["output_load_pf"]):
                tag = f"a{arc_index}s{slew_index}l{load_index}"
                lines.extend(
                    _instance_lines(
                        tag=tag,
                        cell=cell,
                        arc=arc,
                        side_state=side_state,
                        rail_nodes=rail_nodes,
                        vdd=vdd,
                        ramp_ns=ramp_ns,
                        load_pf=load_pf,
                        rise_start=rise_start,
                        fall_start=fall_start,
                    )
                )
                cards = _measurement_cards(
                    tag=tag,
                    arc=arc,
                    vdd=vdd,
                    thresholds=thresholds,
                )
                cards.extend(_power_cards(tag=tag, arc=arc, windows=windows))
                measurements.extend(cards)
                points.append(
                    {
                        "tag": tag,
                        "arc_index": arc_index,
                        "slew_index": slew_index,
                        "load_index": load_index,
                        "input_transition_ns": transition_ns,
                        "output_load_pf": load_pf,
                        "ramp_ns": ramp_ns,
                    }
                )

    leakage_states = _leakage_states(cell)
    for state_index, state in enumerate(leakage_states):
        tag = f"lk{state_index}"
        lines.extend(
            _leakage_instance_lines(
                tag=tag, cell=cell, state=state, rail_nodes=rail_nodes
            )
        )
        measurements.append(
            {
                "name": f"{tag}_i",
                "spice": (
                    f".meas tran {tag}_i AVG i(Vpl{tag}) "
                    f"FROM=0 TO={_spice_number(stop_ns)}n"
                ),
                "unit": "A",
            }
        )

    return {
        "netlist_text": "\n".join(lines) + "\n",
        "measurements": measurements,
        "points": points,
        "arc_count": len(arcs),
        "leakage_states": leakage_states,
        "window": {
            "rise_start_ns": rise_start,
            "fall_start_ns": fall_start,
            "stop_ns": stop_ns,
            "step_ns": options["tran_step_ns"],
        },
    }


def _full_side_state(
    arc: DerivedArc, cell: dict[str, Any]
) -> tuple[tuple[str, bool], ...]:
    """Every input pin other than the arc's related pin, with the constant it
    is held at.

    Pins the output's ``function`` does not reference (a second output's
    inputs on a multi-output cell, an unused pin) are held **low** -- a
    floating input on a CMOS gate is not a valid operating point, so leaving
    one undriven is never an option.
    """
    derived = arc.side_input_map
    return tuple(
        (pin, derived.get(pin, False))
        for pin in cell["input_pins"]
        if pin != arc.related_pin
    )


def _metered_rail_nodes(
    tag: str, cell: dict[str, Any], rail_nodes: dict[str, str]
) -> dict[str, str]:
    """``rail_nodes`` with the cell's primary ``vdd``/``gnd`` terminals moved
    onto this instance's private ammeter nodes."""
    nodes = dict(rail_nodes)
    nodes[cell["power_pins"]["vdd"]] = f"pd_{tag}"
    nodes[cell["power_pins"]["gnd"]] = f"pg_{tag}"
    return nodes


def _instance_lines(
    *,
    tag: str,
    cell: dict[str, Any],
    arc: DerivedArc,
    side_state: tuple[tuple[str, bool], ...],
    rail_nodes: dict[str, str],
    vdd: float,
    ramp_ns: float,
    load_pf: float,
    rise_start: float,
    fall_start: float,
) -> list[str]:
    side_map = dict(side_state)
    node_of: dict[str, str] = {}
    for pin in cell["pins"]:
        name = pin["name"]
        if pin["direction"] == "output":
            node_of[name] = f"o_{tag}_{_slug(name)}"
        elif name == arc.related_pin:
            node_of[name] = f"i_{tag}"
        else:
            node_of[name] = "vdd" if side_map[name] else "0"

    metered = _metered_rail_nodes(tag, cell, rail_nodes)
    nodes = [
        node_of.get(terminal) or metered[terminal] for terminal in cell["terminals"]
    ]
    lines = [
        f"V{tag} i_{tag} 0 PWL(0 0 "
        f"{_spice_number(rise_start)}n 0 "
        f"{_spice_number(rise_start + ramp_ns)}n {_spice_number(vdd)} "
        f"{_spice_number(fall_start)}n {_spice_number(vdd)} "
        f"{_spice_number(fall_start + ramp_ns)}n 0)",
        # 0 V ammeters: i(Vpd) is the current the supply delivers INTO the
        # cell's vdd pin, i(Vpg) the current flowing OUT of its gnd pin.
        f"Vpd{tag} vdd pd_{tag} DC 0",
        f"Vpg{tag} pg_{tag} 0 DC 0",
        f"X{tag} {' '.join(nodes)} {cell['subckt']}",
    ]
    for output in cell["output_pins"]:
        lines.append(
            f"C{tag}_{_slug(output)} {node_of[output]} 0 {_spice_number(load_pf)}p"
        )
    return lines


def _leakage_states(cell: dict[str, Any]) -> tuple[tuple[tuple[str, bool], ...], ...]:
    """Every assignment of the cell's input pins, in a fixed order (the first
    input is the most significant bit), each as ``((pin, value), ...)``.

    Exhaustive over ``2**n`` for the same reason arc derivation is: a
    standard cell has a handful of inputs, and a vendor ``.lib`` reports one
    ``leakage_power`` group per state.
    """
    inputs = cell["input_pins"]
    states = []
    for code in range(2 ** len(inputs)):
        states.append(
            tuple(
                (pin, bool((code >> (len(inputs) - 1 - position)) & 1))
                for position, pin in enumerate(inputs)
            )
        )
    return tuple(states)


def _leakage_instance_lines(
    *,
    tag: str,
    cell: dict[str, Any],
    state: tuple[tuple[str, bool], ...],
    rail_nodes: dict[str, str],
) -> list[str]:
    values = dict(state)
    node_of: dict[str, str] = {}
    for pin in cell["pins"]:
        name = pin["name"]
        if pin["direction"] == "output":
            node_of[name] = f"o_{tag}_{_slug(name)}"
        else:
            # A high input is tied to the *metered* supply node, not the
            # shared rail: its gate leakage is drawn from the supply too, and
            # static power is every source's V * I -- a low input's source
            # sits at 0 V and delivers none. Measured on IHP sg13g2_inv_1
            # with A high, routing the input around the ammeter under-reports
            # the state's leakage by roughly a third (the NMOS gate current).
            node_of[name] = f"pl_{tag}" if values[name] else "0"
    nodes = []
    for terminal in cell["terminals"]:
        if terminal in node_of:
            nodes.append(node_of[terminal])
        elif terminal == cell["power_pins"]["vdd"]:
            nodes.append(f"pl_{tag}")
        else:
            nodes.append(rail_nodes[terminal])
    return [
        f"Vpl{tag} vdd pl_{tag} DC 0",
        f"X{tag} {' '.join(nodes)} {cell['subckt']}",
    ]


def _measurement_cards(
    *,
    tag: str,
    arc: DerivedArc,
    vdd: float,
    thresholds: liberty_writer.Thresholds,
) -> list[dict[str, Any]]:
    output_node = f"o_{tag}_{_slug(arc.output_pin)}"
    input_node = f"i_{tag}"

    # Which *input* edge produces the rising output edge, under this arc's
    # own measured polarity.
    rise_input_edge = "RISE" if arc.measured_sense == "positive_unate" else "FALL"
    fall_input_edge = "FALL" if arc.measured_sense == "positive_unate" else "RISE"

    v_in_rise = vdd * thresholds.input_threshold_pct_rise / 100.0
    v_in_fall = vdd * thresholds.input_threshold_pct_fall / 100.0
    v_out_rise = vdd * thresholds.output_threshold_pct_rise / 100.0
    v_out_fall = vdd * thresholds.output_threshold_pct_fall / 100.0
    v_slew_lo_rise = vdd * thresholds.slew_lower_threshold_pct_rise / 100.0
    v_slew_hi_rise = vdd * thresholds.slew_upper_threshold_pct_rise / 100.0
    v_slew_lo_fall = vdd * thresholds.slew_lower_threshold_pct_fall / 100.0
    v_slew_hi_fall = vdd * thresholds.slew_upper_threshold_pct_fall / 100.0

    trig_rise_v = v_in_rise if rise_input_edge == "RISE" else v_in_fall
    trig_fall_v = v_in_rise if fall_input_edge == "RISE" else v_in_fall

    cards = [
        (
            f"{tag}_dr",
            f"TRIG v({input_node}) VAL={_spice_number(trig_rise_v)} "
            f"{rise_input_edge}=1 "
            f"TARG v({output_node}) VAL={_spice_number(v_out_rise)} RISE=1",
        ),
        (
            f"{tag}_df",
            f"TRIG v({input_node}) VAL={_spice_number(trig_fall_v)} "
            f"{fall_input_edge}=1 "
            f"TARG v({output_node}) VAL={_spice_number(v_out_fall)} FALL=1",
        ),
        (
            f"{tag}_tr",
            f"TRIG v({output_node}) VAL={_spice_number(v_slew_lo_rise)} RISE=1 "
            f"TARG v({output_node}) VAL={_spice_number(v_slew_hi_rise)} RISE=1",
        ),
        (
            f"{tag}_tf",
            f"TRIG v({output_node}) VAL={_spice_number(v_slew_hi_fall)} FALL=1 "
            f"TARG v({output_node}) VAL={_spice_number(v_slew_lo_fall)} FALL=1",
        ),
    ]
    return [
        {"name": name, "spice": f".meas tran {name} {body}", "unit": "s"}
        for name, body in cards
    ]


#: The four supply-charge integrals per grid point, and what each measures:
#: ``(suffix, ammeter prefix, output edge)``. ``qv*`` is charge drawn from
#: the vdd rail, ``qg*`` charge returned to the gnd rail, over the window
#: of the input edge that produces the named *output* edge.
_POWER_CARDS = (
    ("qvr", "Vpd", "rise"),
    ("qgr", "Vpg", "rise"),
    ("qvf", "Vpd", "fall"),
    ("qgf", "Vpg", "fall"),
)


def _power_cards(
    *,
    tag: str,
    arc: DerivedArc,
    windows: dict[str, tuple[float, float]],
) -> list[dict[str, Any]]:
    """``INTEG`` cards for the four per-edge rail charges of one grid point.

    The output-rise window is the *input* edge's window that produces it --
    the rising input's for a positive-unate measured polarity, the falling
    input's otherwise -- mirroring :func:`_measurement_cards`'s trigger
    choice so a table can never pair one edge's delay with the other edge's
    energy.
    """
    rise_input_edge = "RISE" if arc.measured_sense == "positive_unate" else "FALL"
    fall_input_edge = "FALL" if arc.measured_sense == "positive_unate" else "RISE"
    window_of = {
        "rise": windows[rise_input_edge],
        "fall": windows[fall_input_edge],
    }
    cards = []
    for suffix, ammeter, edge in _POWER_CARDS:
        start, stop = window_of[edge]
        cards.append(
            {
                "name": f"{tag}_{suffix}",
                "spice": (
                    f".meas tran {tag}_{suffix} INTEG i({ammeter}{tag}) "
                    f"FROM={_spice_number(start)}n TO={_spice_number(stop)}n"
                ),
                "unit": "C",
            }
        )
    return cards


def _spice_number(value: float) -> str:
    return liberty_writer.format_number(value, precision=10)


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", name)


# --------------------------------------------------------------------------- #
# Simulation
# --------------------------------------------------------------------------- #


def _build_sim_request(
    *,
    request: dict[str, Any],
    request_dir: str,
    testbench_path: str,
    corner: dict[str, Any],
    plan: dict[str, Any],
    options: dict[str, Any],
    keep_artifacts: bool | None,
) -> dict[str, Any]:
    window = plan["window"]
    step = _spice_number(window["step_ns"])
    stop = _spice_number(window["stop_ns"])
    corners: dict[str, Any] = {
        "supply_v": {"vdd": [corner["supply_v"]]},
        "temperature_c": [corner["temperature_c"]],
    }
    if corner["process"] is not None:
        corners["process"] = [corner["process"]]

    sim_request: dict[str, Any] = {
        "netlist": testbench_path,
        "engine": "ngspice",
        "corners": corners,
        # `tmax` (the fourth `tran` argument) is pinned to the step: the
        # smallest grid transition is tens of picoseconds, and ngspice's
        # default max step of (tstop-tstart)/50 would resolve it with a
        # handful of timepoints, so the `.meas` interpolation error would
        # dominate the measurement it is supposed to report.
        "analysis": {"kind": "tran", "args": f"{step}n {stop}n 0 {step}n"},
        "measurements": plan["measurements"],
        "options": {
            "timeout_s": options["timeout_s"],
            "keep_artifacts": (
                options["keep_artifacts"] if keep_artifacts is None else keep_artifacts
            ),
        },
    }
    if options["osdi_preload"]:
        # Issue #2513: already resolved to absolute paths against the
        # characterize request's directory, so they mean the same thing
        # from the generated request's own (work) directory.
        sim_request["options"]["osdi_preload"] = list(options["osdi_preload"])
    models = request.get("models")
    if models is not None:
        if not isinstance(models, dict):
            raise CharacterizeError("request.models must be an object")
        sim_request["models"] = _rebase_models(models, request_dir)
    elif corner["process"] is not None:
        raise CharacterizeError(
            "request.corner.process names a model-library section but "
            "request.models is absent -- a process axis needs a model library "
            "to select the section from (same shape as `klt sim`'s `models`)"
        )
    netlist_source = request.get("netlist_source")
    if netlist_source is not None:
        sim_request["netlist_source"] = netlist_source
    return sim_request


def _rebase_models(models: dict[str, Any], request_dir: str) -> dict[str, Any]:
    """Re-anchor a relative ``models.lib`` onto the **characterize** request's
    own directory.

    ``klt sim`` resolves a relative ``models.lib`` against *its* request
    file's directory, and this command's generated sim request lives in the
    output directory, not beside the caller's request -- so a request saying
    ``"lib": "models.lib"`` would otherwise be looked for under
    ``.klt/characterize/``. Absolutising here keeps "paths resolve against the
    request file's own directory" true for every path in a `klt characterize`
    request, which is the convention every other request-taking verb states.
    Left untouched when ``pdk``/``pdk_root`` is given: `klt sim` then joins
    ``lib`` against the resolved PDK variant directory, which has nothing to
    do with either request's location.
    """
    lib = models.get("lib")
    if not isinstance(lib, str) or not lib.strip():
        return models
    if models.get("pdk") is not None or models.get("pdk_root") is not None:
        return models
    return {**models, "lib": _resolve_relative(lib, request_dir)}


def _extract_measurements(
    sim_report: dict[str, Any], sim_report_path: str
) -> dict[str, float]:
    corners = sim_report.get("corners") or []
    if len(corners) != 1:
        raise CharacterizeError(
            f"expected exactly one simulated corner, got {len(corners)} -- "
            "`klt characterize` is single-corner by construction"
        )
    entry = corners[0]
    if entry.get("status") != "pass":
        diagnostics = entry.get("diagnostics") or []
        detail = "; ".join(
            f"{item.get('code')}: {item.get('message')}" for item in diagnostics[:3]
        )
        raise CharacterizeError(
            f"characterization sweep corner '{entry.get('corner_id')}' reported "
            f"status '{entry.get('status')}'"
            + (f" ({detail})" if detail else "")
            + f" -- full simulation report: {sim_report_path}"
        )
    values: dict[str, float] = {}
    missing: list[str] = []
    for measurement in entry.get("measurements") or []:
        value = measurement.get("value")
        if value is None:
            missing.append(measurement["name"])
            continue
        values[measurement["name"]] = float(value)
    if missing:
        raise CharacterizeError(
            f"{len(missing)} grid measurement(s) produced no value (e.g. "
            f"{', '.join(missing[:5])}) -- the emitted `.lib` would carry a "
            "hole, so nothing was written. Check the transient window "
            "(request.options.settle_ns) and the simulation report: "
            f"{sim_report_path}"
        )
    return values


# --------------------------------------------------------------------------- #
# Table assembly
# --------------------------------------------------------------------------- #


def _build_tables(
    plan: dict[str, Any],
    grid: dict[str, tuple[float, ...]],
    values: dict[str, float],
    thresholds: liberty_writer.Thresholds,
) -> dict[int, dict[str, liberty_writer.Table2D]]:
    """Reshape the flat ``.meas`` results into one
    ``{arc_index: {table_name: Table2D}}`` structure, converting seconds to
    the library's nanosecond time unit.

    Transition values additionally carry ``slew_derate_from_library``, the
    inverse of the conversion :func:`_ramp_ns` applies to the index axis --
    so a library declaring a derate reports a derated transition beside its
    derated index, exactly as a reader interpolating one against the other
    assumes.
    """
    slews = grid["input_transition_ns"]
    loads = grid["output_load_pf"]
    arc_count = plan["arc_count"]

    tables: dict[int, dict[str, liberty_writer.Table2D]] = {}
    for arc_index in range(arc_count):
        per_table: dict[str, liberty_writer.Table2D] = {}
        for table_name, suffix in _TABLES:
            derate = (
                thresholds.slew_derate_from_library
                if table_name.endswith("_transition")
                else 1.0
            )
            rows: list[tuple[float, ...]] = []
            for slew_index in range(len(slews)):
                row: list[float] = []
                for load_index in range(len(loads)):
                    key = f"a{arc_index}s{slew_index}l{load_index}_{suffix}"
                    if key not in values:
                        raise CharacterizeError(
                            f"no measured value for '{key}' -- the simulation "
                            "report does not cover the whole requested grid"
                        )
                    row.append(values[key] * 1e9 * derate)
                rows.append(tuple(row))
            per_table[table_name] = liberty_writer.Table2D(
                index_1=slews,
                index_2=loads,
                values=tuple(rows),
            )
        tables[arc_index] = per_table
    return tables


def _build_power_tables(
    plan: dict[str, Any],
    grid: dict[str, tuple[float, ...]],
    values: dict[str, float],
    corner: dict[str, Any],
) -> dict[int, dict[str, liberty_writer.Table2D]]:
    """Reshape the per-edge rail charges into ``rise_power``/``fall_power``
    tables, one pair per arc, in the library's energy unit (pJ -- Liberty's
    internal-energy unit is ``voltage_unit**2 * capacitive_load_unit``, i.e.
    1 V * 1 V * 1 pF here).

    **The convention (Liberty *internal* energy, not total energy).** A
    consumer (``klt power``, OpenSTA's power engine, any Liberty power
    reader) adds the load's own switching energy -- ``C_load * V**2 / 2`` per
    output transition -- on top of the table, so the table must exclude it
    or the load would be counted twice. Per output edge this computes::

        E_edge = V * (Q_vdd + Q_gnd) / 2  -  C_load * V**2 / 2

    where ``Q_vdd`` is the charge drawn from the vdd rail and ``Q_gnd`` the
    charge returned to the gnd rail over that edge's window. Averaging the
    two rails is what makes the split *symmetric*: on an output rise
    ``Q_vdd`` carries the load *and* internal-node charge while ``Q_gnd``
    carries only the short-circuit current, and on a fall it is the other way
    round -- so each edge ends up owning half the internal-node charging
    energy plus its own short-circuit energy, and the two edges sum to the
    whole-cycle internal energy exactly. (The alternative, "vdd only, subtract
    ``C_load * V**2`` on the rise", sums to the same cycle total but piles all
    the internal-node energy onto the rise edge; measured against IHP's
    ``sg13g2_stdcell`` it is the worse fit by several femtojoules per point.

    The vendor's own split *does* differ from both conventions on arcs that
    switch a series-stack internal node -- ``sg13g2_nand2_1``'s ``B->Y`` and
    ``sg13g2_nor2_1``'s ``A->Y`` (issue #2527) -- and it is not a rail-choice
    artifact: node-voltage tracing on a standalone instance of each cell
    shows the internal node *discharging* through the just-switched
    transistor in well under a nanosecond (driven hard on) but *recharging*
    through the other, permanently-on transistor over a many-tens-of-ns
    subthreshold tail (it settles near ``Vdd - Vth``, not the rail, because
    that transistor never turns fully off). Any window short enough to be
    practical -- ours included, ``settle_ns`` below is a few input ramps'
    worth, not the ~100 ns the tail needs -- captures the fast discharge in
    full but only the leading edge of the slow recharge, so whichever output
    edge is the *recharge* edge reads low relative to its true,
    infinite-settle energy. That is a window-*length* effect: it shows up on
    ``Q_vdd`` and ``Q_gnd`` alike, so no per-edge rail-attribution convention
    -- symmetric or otherwise -- corrects it, and matching the vendor's own
    split would mean guessing their (undocumented, and almost certainly
    different) window length rather than adopting a principled convention.
    See ``docs/cli/characterize.md``'s "Accuracy against a vendor library"
    for the measured deltas this leaves in place.)

    Supply leakage integrated over the (settle-dominated) window is left in:
    at the static currents a standard cell draws (tens to ~100 pW on IHP
    ``sg13g2_stdcell`` at typ) over a 7x7 grid's ~17 ns window it is a few
    attojoules, three orders of magnitude below the femtojoule internal
    energy, and subtracting an estimate would add more noise than it removes.
    """
    slews = grid["input_transition_ns"]
    loads = grid["output_load_pf"]
    vdd = corner["supply_v"]
    tables: dict[int, dict[str, liberty_writer.Table2D]] = {}
    for arc_index in range(plan["arc_count"]):
        per_table: dict[str, liberty_writer.Table2D] = {}
        for table_name, edge in (("rise_power", "r"), ("fall_power", "f")):
            rows: list[tuple[float, ...]] = []
            for slew_index in range(len(slews)):
                row: list[float] = []
                for load_index, load_pf in enumerate(loads):
                    tag = f"a{arc_index}s{slew_index}l{load_index}"
                    charges = []
                    for rail in ("qv", "qg"):
                        key = f"{tag}_{rail}{edge}"
                        if key not in values:
                            raise CharacterizeError(
                                f"no measured value for '{key}' -- the "
                                "simulation report does not cover the whole "
                                "requested grid"
                            )
                        charges.append(values[key])
                    # Coulombs * volts = joules; * 1e12 = pJ.
                    rail_energy_pj = vdd * (charges[0] + charges[1]) / 2.0 * 1e12
                    load_energy_pj = 0.5 * load_pf * vdd * vdd
                    row.append(rail_energy_pj - load_energy_pj)
                rows.append(tuple(row))
            per_table[table_name] = liberty_writer.Table2D(
                index_1=slews,
                index_2=loads,
                values=tuple(rows),
            )
        tables[arc_index] = per_table
    return tables


def _build_leakage(
    plan: dict[str, Any],
    values: dict[str, float],
    corner: dict[str, Any],
) -> dict[str, Any]:
    """Per-input-state static power (pW) and their mean, the
    ``cell_leakage_power`` a vendor library reports alongside the states.

    Each state's ``when`` is the conjunction of its input literals
    (``"A&!B"``), which is the form a Liberty reader matches against; the
    mean over all ``2**n`` states is the unweighted average IHP's own
    ``sg13g2_stdcell`` uses for ``cell_leakage_power`` (its ``sg13g2_inv_1``
    reports 63.0032 = the mean of its two states, 82.469 and 43.5374).
    """
    vdd = corner["supply_v"]
    states: list[dict[str, Any]] = []
    for state_index, state in enumerate(plan["leakage_states"]):
        key = f"lk{state_index}_i"
        if key not in values:
            raise CharacterizeError(
                f"no measured value for '{key}' -- the simulation report does "
                "not cover every leakage state"
            )
        states.append(
            {
                "when": "&".join(pin if value else f"!{pin}" for pin, value in state),
                "inputs": {pin: value for pin, value in state},
                # Amps * volts = watts; * 1e12 = pW (the library's
                # leakage_power_unit).
                "value_pw": values[key] * vdd * 1e12,
            }
        )
    mean = sum(entry["value_pw"] for entry in states) / len(states)
    return {"cell_leakage_power_pw": mean, "states": states}


def _check_settle_margin(
    plan: dict[str, Any],
    grid: dict[str, tuple[float, ...]],
    tables: dict[int, dict[str, liberty_writer.Table2D]],
    options: dict[str, Any],
) -> None:
    """Refuse a run whose transient window was too short for the slowest grid
    point to finish settling.

    A ``.meas`` card still finds a crossing when the previous edge has not
    finished -- the reported delay is just measured against a starting level
    that never reached the rail -- so an under-sized window produces a table
    that is wrong rather than absent. Comparing each point's own
    ``delay + transition`` against the settle window is the cheap, direct
    check for it.
    """
    limit = options["settle_ns"] * _SETTLE_MARGIN_FRACTION
    worst: tuple[float, str] | None = None
    for arc_index, per_table in tables.items():
        for delay_name, transition_name in (
            ("cell_rise", "rise_transition"),
            ("cell_fall", "fall_transition"),
        ):
            delay = per_table[delay_name]
            transition = per_table[transition_name]
            for slew_index, _ in enumerate(grid["input_transition_ns"]):
                for load_index, _ in enumerate(grid["output_load_pf"]):
                    total = (
                        delay.values[slew_index][load_index]
                        + transition.values[slew_index][load_index]
                    )
                    if worst is None or total > worst[0]:
                        worst = (
                            total,
                            f"arc {arc_index} {delay_name}[{slew_index}][{load_index}]",
                        )
    if worst is not None and worst[0] > limit:
        raise CharacterizeError(
            f"transient window too short: {worst[1]} measured "
            f"{worst[0]:.4g} ns of delay+transition against a "
            f"{options['settle_ns']:.4g} ns settle window "
            f"(limit {limit:.4g} ns). The next input edge would arrive before "
            "the output settled, which corrupts the measurement rather than "
            "failing it -- raise request.options.settle_ns and re-run."
        )


# --------------------------------------------------------------------------- #
# Liberty emission
# --------------------------------------------------------------------------- #


def _build_library(
    *,
    request: dict[str, Any],
    results: list[dict[str, Any]],
    corner: dict[str, Any],
    grid: dict[str, tuple[float, ...]],
    thresholds: liberty_writer.Thresholds,
    request_path: str,
    batch: bool,
) -> liberty_writer.Library:
    spec = request.get("library") or {}
    if not isinstance(spec, dict):
        raise CharacterizeError("request.library must be an object")
    if batch:
        default_name = f"klt_characterize_{corner['name']}"
    else:
        default_name = f"{results[0]['cell']['name']}_{corner['name']}"
    library_name = spec.get("name") or default_name
    if not isinstance(library_name, str) or not library_name.strip():
        raise CharacterizeError("request.library.name must be a non-empty string")

    cells = tuple(_build_liberty_cell(result=result, grid=grid) for result in results)
    cell_names = ", ".join(result["cell"]["name"] for result in results)
    comment = (
        f"Generated by `klt characterize` from {os.path.basename(request_path)}. "
        f"Delay, transition, internal power, and leakage. Single corner "
        f"({corner['name']}); {len(results)} cell(s): {cell_names}."
    )
    return liberty_writer.Library(
        name=library_name,
        operating_conditions=liberty_writer.OperatingConditions(
            name=corner["name"],
            process=corner["nom_process"],
            temperature_c=corner["temperature_c"],
            voltage_v=corner["supply_v"],
        ),
        cells=cells,
        thresholds=thresholds,
        comment=comment,
    )


def _build_liberty_cell(
    *, result: dict[str, Any], grid: dict[str, tuple[float, ...]]
) -> liberty_writer.Cell:
    cell = result["cell"]
    tables = result["tables"]
    power_tables = result["power_tables"]

    arcs_by_output: dict[str, list[liberty_writer.TimingArc]] = {}
    power_by_output: dict[str, list[liberty_writer.InternalPower]] = {}
    for arc_index, arc in enumerate(result["arcs"]):
        per_table = tables[arc_index]
        arcs_by_output.setdefault(arc.output_pin, []).append(
            liberty_writer.TimingArc(
                related_pin=arc.related_pin,
                timing_sense=arc.timing_sense,
                cell_rise=per_table["cell_rise"],
                cell_fall=per_table["cell_fall"],
                rise_transition=per_table["rise_transition"],
                fall_transition=per_table["fall_transition"],
            )
        )
        power_by_output.setdefault(arc.output_pin, []).append(
            liberty_writer.InternalPower(
                related_pin=arc.related_pin,
                rise_power=power_tables[arc_index]["rise_power"],
                fall_power=power_tables[arc_index]["fall_power"],
            )
        )

    pins = tuple(
        liberty_writer.Pin(
            name=pin["name"],
            direction=pin["direction"],
            capacitance_pf=pin["capacitance_pf"],
            function=pin["function"],
            max_capacitance_pf=(
                max(grid["output_load_pf"]) if pin["direction"] == "output" else None
            ),
            arcs=tuple(arcs_by_output.get(pin["name"], ())),
            internal_power=tuple(power_by_output.get(pin["name"], ())),
        )
        for pin in cell["pins"]
    )
    leakage = result["leakage"]
    return liberty_writer.Cell(
        name=cell["name"],
        pins=pins,
        area=cell["area"],
        cell_leakage_power=leakage["cell_leakage_power_pw"],
        leakage_power=tuple(
            liberty_writer.LeakagePower(when=state["when"], value=state["value_pw"])
            for state in leakage["states"]
        ),
    )


def _resolve_outdir(
    outdir: str | None, request: dict[str, Any], request_dir: str
) -> str:
    if outdir is not None:
        return os.path.abspath(outdir)
    spec = request.get("output") or {}
    if not isinstance(spec, dict):
        raise CharacterizeError("request.output must be an object")
    declared = spec.get("outdir")
    if isinstance(declared, str) and declared.strip():
        return _resolve_relative(declared, request_dir)
    return os.path.join(request_dir, DEFAULT_OUTDIR_NAME)


def _resolve_lib_path(
    lib_path: str | None,
    request: dict[str, Any],
    request_dir: str,
    work_dir: str,
    library: liberty_writer.Library,
) -> str:
    if lib_path is not None:
        resolved = os.path.abspath(lib_path)
    else:
        declared = (request.get("output") or {}).get("lib")
        if isinstance(declared, str) and declared.strip():
            resolved = _resolve_relative(declared, request_dir)
        else:
            resolved = os.path.join(work_dir, f"{library.name}.lib")
    parent = os.path.dirname(resolved)
    if parent:
        os.makedirs(parent, exist_ok=True)
    return resolved


# --------------------------------------------------------------------------- #
# Round trip
# --------------------------------------------------------------------------- #


def roundtrip_check(
    *,
    lib_path: str,
    cell_name: str,
    input_pins: tuple[str, ...],
    output_pins: tuple[str, ...],
    work_dir: str,
) -> dict[str, Any]:
    """Parse ``lib_path`` back through ``native/statime``'s Liberty reader,
    probing one cell. See :func:`roundtrip_check_cells`."""
    return roundtrip_check_cells(
        lib_path=lib_path,
        cells=[(cell_name, input_pins, output_pins)],
        work_dir=work_dir,
    )


def roundtrip_check_cells(
    *,
    lib_path: str,
    cells: list[tuple[str, tuple[str, ...], tuple[str, ...]]],
    work_dir: str,
) -> dict[str, Any]:
    """Parse ``lib_path`` back through ``native/statime``'s Liberty reader.

    The reader is not exposed on its own across the pyo3 boundary -- the
    extension's single entry point is ``critical_path_json(netlist, liberty,
    top, ...)`` -- so this writes a structural Verilog wrapper instantiating
    **every** characterized cell once and asks the engine to analyse it.
    That exercises strictly more than a bare parse: ``liberty.rs`` has to
    parse the file, ``sta.rs`` has to find each cell and its pins, and
    ``nldm.rs`` has to interpolate the emitted tables to produce the reported
    delay. A file that parses structurally but carries a malformed table
    therefore still fails here -- and in batch mode a combined library whose
    second cell the reader cannot find fails too, rather than passing on the
    strength of its first.

    Returns ``{"engine", "status", "message", "probe_netlist"}`` with
    ``status`` one of:

    - ``"pass"`` -- the reader accepted the file and the engine reported a
      path delay through the probe.
    - ``"skipped"`` -- the ``klt_statime_native`` extension is not installed,
      so nothing was verified. Never a fabricated pass; ``message`` says so.
    - ``"fail"`` -- the reader (or the engine behind it) rejected the file.
    """
    from .sta import StaError, compute_critical_path  # local: optional extension

    probe_path = os.path.join(work_dir, "roundtrip-probe.v")
    with open(probe_path, "w", encoding="utf-8") as handle:
        handle.write(_probe_netlist_cells(cells))
    try:
        result = compute_critical_path(probe_path, lib_path, "klt_characterize_probe")
    except StaError as exc:
        message = str(exc)
        if "extension is not installed" in message:
            return {
                "engine": ROUNDTRIP_ENGINE,
                "status": "skipped",
                "message": (
                    "the emitted Liberty was NOT verified against "
                    f"{ROUNDTRIP_ENGINE}'s reader: {message}"
                ),
                "probe_netlist": probe_path,
            }
        return {
            "engine": ROUNDTRIP_ENGINE,
            "status": "fail",
            "message": message,
            "probe_netlist": probe_path,
        }
    worst = result.get("worst_path") or {}
    num_cells = result.get("num_cells")
    if isinstance(num_cells, int) and num_cells < len(cells):
        return {
            "engine": ROUNDTRIP_ENGINE,
            "status": "fail",
            "message": (
                f"the reader resolved {num_cells} of the {len(cells)} probe "
                "instance(s) -- the emitted library does not define every "
                "characterized cell"
            ),
            "probe_netlist": probe_path,
        }
    return {
        "engine": ROUNDTRIP_ENGINE,
        "status": "pass",
        "message": (
            f"parsed and interpolated: worst probe-path delay "
            f"{worst.get('delay_ns')} ns through {num_cells} cell(s)"
        ),
        "probe_netlist": probe_path,
    }


def _probe_netlist(
    *, cell_name: str, input_pins: tuple[str, ...], output_pins: tuple[str, ...]
) -> str:
    return _probe_netlist_cells([(cell_name, input_pins, output_pins)])


def _probe_netlist_cells(
    cells: list[tuple[str, tuple[str, ...], tuple[str, ...]]],
) -> str:
    """A structural wrapper instantiating each cell once (``u0``, ``u1``,
    ...). A single cell keeps its own pin names as the module's ports (the
    #2502 shape); several cells get per-instance ``u<i>_<pin>`` ports so no
    two instances share a net."""
    single = len(cells) == 1

    def port(index: int, pin: str) -> str:
        return pin if single else f"u{index}_{pin}"

    inputs = [
        port(index, pin) for index, (_, ins, _) in enumerate(cells) for pin in ins
    ]
    outputs = [
        port(index, pin) for index, (_, _, outs) in enumerate(cells) for pin in outs
    ]
    lines = [
        "/* Generated by `klt characterize` -- a wrapper instantiating each",
        " * characterized cell once, whose only purpose is to make",
        " * native/statime's Liberty reader parse and interpolate the emitted",
        " * .lib. Not a design. */",
        f"module klt_characterize_probe({', '.join(inputs + outputs)});",
    ]
    for name in inputs:
        lines.append(f"  input {name};")
        lines.append(f"  wire {name};")
    for name in outputs:
        lines.append(f"  output {name};")
        lines.append(f"  wire {name};")
    for index, (cell_name, ins, outs) in enumerate(cells):
        lines.append(f"  {cell_name} u{index} (")
        connections = [f"    .{pin}({port(index, pin)})" for pin in ins + outs]
        lines.append(",\n".join(connections))
        lines.append("  );")
    lines.append("endmodule")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Response
# --------------------------------------------------------------------------- #


def _table_json(table: liberty_writer.Table2D) -> dict[str, Any]:
    return {
        "index_1": list(table.index_1),
        "index_2": list(table.index_2),
        "values": [list(row) for row in table.values],
    }


def _cell_entry(result: dict[str, Any]) -> dict[str, Any]:
    """One characterized cell's response entry: the echo of the resolved
    cell, its arcs with all six tables, its leakage states, and its own
    simulation run."""
    cell = result["cell"]
    tables = result["tables"]
    power_tables = result["power_tables"]
    sim_report = result["sim_report"]
    corner_entry = sim_report["corners"][0]
    environment = sim_report.get("environment") or {}
    window = result["plan"]["window"]
    return {
        "cell": {
            "name": cell["name"],
            "subckt": cell["subckt"],
            "netlist": repo_relative_path(cell["netlist_path"]),
            "pins": [
                {
                    "name": pin["name"],
                    "direction": pin["direction"],
                    "function": pin["function"],
                    "capacitance_pf": pin["capacitance_pf"],
                }
                for pin in cell["pins"]
            ],
        },
        "arcs": [
            {
                "output_pin": arc.output_pin,
                "related_pin": arc.related_pin,
                "timing_sense": arc.timing_sense,
                "measured_sense": arc.measured_sense,
                "side_inputs": {
                    pin: value for pin, value in _full_side_state(arc, cell)
                },
                **{name: _table_json(tables[index][name]) for name, _ in _TABLES},
                **{
                    name: _table_json(power_tables[index][name])
                    for name in POWER_TABLES
                },
            }
            for index, arc in enumerate(result["arcs"])
        ],
        "leakage": result["leakage"],
        "simulation": {
            "testbench": result["testbench_path"],
            "request": result["sim_request_path"],
            "report": result["sim_report_path"],
            "corner_id": corner_entry["corner_id"],
            "status": corner_entry["status"],
            "runtime_s": corner_entry["runtime_s"],
            "engine": environment.get("engine"),
            "engine_version": environment.get("engine_version"),
            "window": {
                "settle_ns": window["rise_start_ns"],
                "tran_step_ns": window["step_ns"],
            },
        },
    }


def _build_response(
    *,
    request_path: str,
    grid: dict[str, tuple[float, ...]],
    corner: dict[str, Any],
    thresholds: liberty_writer.Thresholds,
    options: dict[str, Any],
    results: list[dict[str, Any]],
    library: liberty_writer.Library,
    emitted_path: str,
    roundtrip: dict[str, Any],
    comparison: dict[str, Any] | None,
    request: dict[str, Any],
    batch: bool,
) -> dict[str, Any]:
    grid_points = len(grid["input_transition_ns"]) * len(grid["output_load_pf"])
    arc_count = sum(len(result["arcs"]) for result in results)
    leakage_count = sum(len(result["plan"]["leakage_states"]) for result in results)
    timing_count = grid_points * arc_count * len(_TABLES)
    power_count = grid_points * arc_count * len(_POWER_CARDS)
    entries = [_cell_entry(result) for result in results]
    # Single-cell form: the top-level `cell`/`arcs`/`leakage`/`simulation`
    # mirror `cells[0]`, exactly as #2502 shipped them. Batch form: they are
    # `null` -- there is no one cell to describe -- and `cells[]` is the
    # data. A batch request is a request shape that did not exist before
    # #2503, so no schema-1 consumer can meet the nulls unawares.
    single = None if batch else entries[0]

    return {
        "schema_version": SCHEMA_VERSION,
        "cell": single["cell"] if single else None,
        "corner": {
            "name": corner["name"],
            "process": corner["process"],
            "supply_v": corner["supply_v"],
            "temperature_c": corner["temperature_c"],
        },
        "grid": {
            "input_transition_ns": list(grid["input_transition_ns"]),
            "output_load_pf": list(grid["output_load_pf"]),
            "points": grid_points,
            "arc_count": arc_count,
            "measurement_count": timing_count,
            "power_measurement_count": power_count,
            "leakage_measurement_count": leakage_count,
            "total_measurement_count": timing_count + power_count + leakage_count,
        },
        "thresholds": {
            name: getattr(thresholds, name) for name in thresholds.__dataclass_fields__
        },
        # The units every table/value in this response (and the emitted
        # `.lib`) is expressed in. Fixed by `liberty_writer.Library`'s
        # defaults (1ns / 1pF / 1V / 1pW); internal energy is Liberty's
        # voltage_unit**2 * capacitive_load_unit = pJ.
        "units": {
            "time": "ns",
            "capacitance": "pF",
            "internal_energy": "pJ",
            "leakage_power": "pW",
        },
        "arcs": single["arcs"] if single else None,
        "leakage": single["leakage"] if single else None,
        "cells": entries,
        "liberty": {
            # Plain absolute string, not the `{path, scope}` envelope: this is
            # a *generated artifact a caller chains onward* (into `klt sta`'s
            # `pdk.liberty`, say), the same role `klt extract`'s
            # `netlist_path` and `klt sim`'s `corners[].artifacts.*` fill --
            # see docs/json-contract.md's "Output-artifact path fields" table,
            # which lists both shapes and which fields use each. `cell.netlist`
            # above is an *input* and does use the envelope, matching
            # `klt sim`'s own `netlist`.
            "path": emitted_path,
            "library_name": library.name,
            "cell_count": len(library.cells),
            "time_unit": library.time_unit,
            "capacitive_load_unit": list(library.capacitive_load_unit),
            "roundtrip": {
                "engine": roundtrip["engine"],
                "status": roundtrip["status"],
                "message": roundtrip["message"],
                "probe_netlist": roundtrip["probe_netlist"],
            },
        },
        "simulation": single["simulation"] if single else None,
        "comparison": comparison,
        "provenance": build_provenance(
            pdk=_best_effort_pdk(request),
            # Batch mode pins the first cell's netlist -- the whole batch in
            # the common case of one library-wide SPICE file; every
            # `cells[].cell.netlist` names its own file regardless.
            input_path=results[0]["cell"]["netlist_path"],
            input_role=INPUT_ROLE_NETLIST,
        ),
    }


def _best_effort_pdk(request: dict[str, Any]) -> dict[str, Any] | None:
    """Resolve ``request.models.pdk``/``pdk_root`` for the provenance block,
    or ``None``.

    Best-effort by design, matching ``klt sim``'s own treatment of the same
    field: a request pointing straight at a model library with no PDK
    variant is legitimate, and a PDK that no longer resolves must not fail a
    run whose simulation already completed.
    """
    models = request.get("models")
    if not isinstance(models, dict):
        return None
    variant = models.get("pdk")
    root = models.get("pdk_root")
    if variant is None and root is None:
        return None
    try:
        return find_pdk(variant=variant, root=root)
    except PdkNotFoundError:
        return None

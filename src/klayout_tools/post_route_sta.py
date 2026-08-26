"""``klt sta``: standalone timing/power analysis of an already-implemented
(placed & routed) design, independent of ``klt place-and-route``'s own
in-flow STA (issue #1099).

**Naming note.** ``src/klayout_tools/sta.py`` already exists and is a
different module entirely -- the thin Python side of the
``klt_statime_native`` Rust boundary backing ``klt synthesize``'s
integrated, *pre-layout*, gate-level ``sta`` report (issue #925, Epic #704
Phase 3; imported by ``synthesize.py``/``restructure.py`` as
``from .sta import StaError, compute_critical_path``). This module is named
``post_route_sta.py`` instead, purely to avoid clobbering that unrelated,
already-shipped module and its own ``StaError`` class -- the CLI verb this
backs is still ``klt sta`` (see ``cli/sta_cmd.py``/``cli/parser.py``), and
nothing about the request/response contract below refers to the native
Rust engine at all: this module drives ``openroad``/OpenSTA as a
subprocess, exactly like ``place_and_route.py``'s own in-flow STA does.

``klt place-and-route`` reports ``worst_slack_ns``/``fmax_mhz``/
``estimated_power_mw`` (and friends) as a *by-product* of its own stages --
the only way to get a timing number for an existing routed DEF was to
re-run the entire implementation flow. That makes correct corner
characterization impossible (re-running place-and-route per corner produces
N different placements/routings, not a characterization of one fixed piece
of geometry) and is expensive (a full flow per corner against a ~2500-
instance design, where the analysis itself is seconds).

This module is a small, self-contained standalone-analysis verb: given a
routed DEF, a resolved PDK/corner, and a clock constraint, it runs a single
fresh OpenSTA session (``read_lef`` x2, ``read_def`` -- no
``-floorplan_initialize``, unlike ``place_and_route.py``'s own floorplan-
stage load -- ``read_liberty``, ``create_clock``, optionally ``read_spef``)
and reports the same timing/power fields ``place-and-route``'s response
already carries. It never places, routes, or runs CTS -- there is no
``target_stage``, no netlist, no ``link_design``; the DEF handed in is the
one and only geometry analysed.

Deliberately duplicates several small helpers already defined in
``place_and_route.py`` (``_clock_lines``, ``_run_openroad``, ``_read_metrics``,
``_count_violations``, the SPEF net-name-correlation Tcl) rather than
importing them -- this repo's own stated convention (see
``place_and_route.py``'s ``_resolve_liberty`` docstring) is that each verb
module stays self-contained; every existing cross-module import between verb
modules in this repo is of a *public* name (``run_place_and_route``,
``PlaceAndRouteError``, ``run_extract``, ...), never a private one.

Scope deliberately excluded from this first version (tracked as follow-up,
not required for this issue): a ``propagated_clock`` request option (the
in-flow STA -- and this module -- both time an ideal SDC-only clock even
once a real clock tree exists) and a bisected (rather than
``report_fmax_metric``'s ``1/(T-WNS)`` extrapolated) ``fmax_mhz``.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any

from ._paths import _load_request_json, validate_request_shape
from ._provenance import build_provenance
from .pdk import PdkNotFoundError, find_pdk, lef_files, list_cell_libraries

#: Bumped only on a non-additive (breaking) change to this command's own
#: JSON shape -- versioned independently of every other verb's own
#: ``SCHEMA_VERSION`` (docs/json-contract.md: "versioned per command, not
#: globally").
SCHEMA_VERSION = 1

_SETUP_VIOLATIONS_BEGIN = "===KLT_STA_SETUP_VIOLATIONS_BEGIN==="
_SETUP_VIOLATIONS_END = "===KLT_STA_SETUP_VIOLATIONS_END==="
_HOLD_VIOLATIONS_BEGIN = "===KLT_STA_HOLD_VIOLATIONS_BEGIN==="
_HOLD_VIOLATIONS_END = "===KLT_STA_HOLD_VIOLATIONS_END==="
_SPEF_NET_CHECK_BEGIN = "===KLT_STA_SPEF_NET_CHECK_BEGIN==="
_SPEF_NET_CHECK_END = "===KLT_STA_SPEF_NET_CHECK_END==="
_SPEF_NET_CHECK_RE = re.compile(r"(\d+)\s+(\d+)\s+(\d+)\s+(\d+)")
#: ``*D_NET <name> <total_cap>`` -- the SPEF net-declaration line every
#: writer (including this repo's own ``extract.py::_write_spef``) emits one
#: of per net. Used only to recover the *name set* a caller-supplied
#: ``spef`` file declares, for the net-name-correlation sanity check below --
#: never to parse capacitance/resistance values.
_SPEF_D_NET_RE = re.compile(r"^\*D_NET\s+(\S+)", re.MULTILINE)
#: Reverses ``extract_spef.py``'s ``_spef_name()`` escaping (backslash
#: before every character outside ``[A-Za-z0-9_]``) -- see
#: :func:`_unescape_spef_name`.
_SPEF_ESCAPED_CHAR_RE = re.compile(r"\\(.)")

#: Delimits the (capped) sample of design-side net names that failed to
#: correlate against the SPEF's own declared name set -- see
#: :func:`_spef_net_check_lines` and :func:`_parse_spef_missing_nets`.
_SPEF_MISSING_NETS_BEGIN = "===KLT_STA_SPEF_MISSING_NETS_BEGIN==="
_SPEF_MISSING_NETS_END = "===KLT_STA_SPEF_MISSING_NETS_END==="
#: Upper bound on how many uncorrelated design-side net names
#: :func:`_spef_net_check_lines` collects into ``klt_design_missing`` -- a
#: diagnostic sample, not an exhaustive list, so a design with thousands of
#: misnamed nets doesn't balloon the OpenSTA stdout this module parses.
_SPEF_MISSING_NETS_SAMPLE_LIMIT = 20

_OPENROAD_VERSION_RE = re.compile(r"OpenROAD\s+(\S+)")


class PostRouteStaError(Exception):
    """Raised when a standalone STA run cannot be completed: a missing/
    malformed request file, an unresolvable DEF/``pdk.cell_library``/
    ``corner``/LEF/SPEF, a missing clock constraint, or an OpenROAD engine
    error.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback -- matching every other ``klt`` verb's own module-specific
    error class (``PlaceAndRouteError``, ``SynthesizeError``, ...).
    """


def load_request(request_path: str) -> dict[str, Any]:
    """Read and minimally validate a ``klt sta`` request JSON file.

    Raises :class:`PostRouteStaError` if the file is missing/unreadable,
    not valid JSON, or missing a required top-level field (``def``, ``pdk``,
    ``constraints``). Does not require a ``schema`` field, matching every
    other request-taking verb's ``load_request``.
    """
    request = _load_request_json(request_path, PostRouteStaError)
    return validate_request_shape(
        request,
        "request file",
        error_cls=PostRouteStaError,
        required_fields=("def", "pdk", "constraints"),
    )


def run_sta(
    request_path: str,
    *,
    pdk_variant: str | None = None,
    pdk_root: str | None = None,
) -> dict[str, Any]:
    """Run a standalone OpenSTA timing/power analysis over an
    already-routed DEF declared by the request at ``request_path``.

    ``pdk_variant``/``pdk_root`` (the CLI's ``--pdk``/``--pdk-root`` flags)
    optionally pin a specific installed PDK variant/root, passed straight
    through to :func:`_resolve_liberty`'s own ``find_pdk()`` call. ``None``
    (the default) leaves ``find_pdk()``'s own default search order in
    effect.

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/sta.md``). Raises :class:`PostRouteStaError` for anything
    that prevents the analysis from completing.

    Generated Tcl scripts and the raw OpenROAD ``-metrics`` JSON dump are
    written to ``.klt/sta/`` next to the request file (mirroring ``klt
    place-and-route``'s own ``.klt/place-and-route/`` convention) and kept
    as debuggable artifacts, never deleted.
    """
    request = load_request(request_path)
    request_dir = os.path.dirname(os.path.abspath(request_path))

    def_path = _resolve_def(request["def"], request_dir)
    hdl_toplevel = request.get("hdl_toplevel")
    if hdl_toplevel is not None and (
        not isinstance(hdl_toplevel, str) or not hdl_toplevel
    ):
        raise PostRouteStaError(
            "request.hdl_toplevel must be a non-empty string when given"
        )

    pdk_spec = request["pdk"]
    if not isinstance(pdk_spec, dict):
        raise PostRouteStaError("request.pdk must be a JSON object")
    cell_library = pdk_spec.get("cell_library")
    if not isinstance(cell_library, str) or not cell_library:
        raise PostRouteStaError("request.pdk.cell_library is required")
    requested_corner = pdk_spec.get("corner")
    if requested_corner is not None and not (
        isinstance(requested_corner, str) and requested_corner
    ):
        raise PostRouteStaError(
            "request.pdk.corner must be a non-empty string when given"
        )

    clock_port, clock_period_ns = _validate_constraints(request["constraints"])
    spef_path = _resolve_spef(request.get("spef"), request_dir)

    liberty_path, corner, pdk_info = _resolve_liberty(
        cell_library, requested_corner, variant=pdk_variant, root=pdk_root
    )
    tech_lef, cell_lef = _resolve_lef(cell_library, pdk_info)

    output_dir = os.path.join(request_dir, ".klt", "sta")
    try:
        os.makedirs(output_dir, exist_ok=True)
    except OSError as exc:
        raise PostRouteStaError(
            f"could not create output directory '{output_dir}': {exc}"
        ) from exc

    basename = os.path.splitext(os.path.basename(def_path))[0]
    script_path = os.path.join(output_dir, f"sta_{basename}.tcl")
    metrics_path = os.path.join(output_dir, f"{basename}_metrics.json")

    spef_net_names = _spef_net_names(spef_path) if spef_path is not None else None
    lines = _sta_script_lines(
        tech_lef=tech_lef,
        cell_lef=cell_lef,
        def_path=def_path,
        liberty_path=liberty_path,
        clock_port=clock_port,
        clock_period_ns=clock_period_ns,
        spef_path=spef_path,
        spef_net_names=spef_net_names,
    )
    _write_script(script_path, lines)

    completed = _run_openroad(script_path, metrics_path)
    if completed.returncode != 0:
        raise PostRouteStaError(_engine_error_message(completed))

    metrics = _read_metrics(metrics_path)
    setup_violation_count = _count_violations(
        completed.stdout, _SETUP_VIOLATIONS_BEGIN, _SETUP_VIOLATIONS_END
    )
    hold_violation_count = _count_violations(
        completed.stdout, _HOLD_VIOLATIONS_BEGIN, _HOLD_VIOLATIONS_END
    )

    worst_slack = metrics.get("timing__setup__ws")
    tns = metrics.get("timing__setup__tns")
    fmax_hz = metrics.get("timing__fmax")
    power_w = metrics.get("power__total")
    clock_skew = metrics.get("clock__skew__setup")

    engine_version = _openroad_version()
    deck_name = f"{cell_library}__{corner}"
    provenance = build_provenance(
        deck_name=deck_name,
        deck_path=liberty_path,
        pdk=pdk_info,
        input_path=def_path,
    )

    response: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "engine": "openroad",
        "engine_version": engine_version,
        "hdl_toplevel": hdl_toplevel,
        "status": "ok",
        "def_path": def_path,
        "spef_path": spef_path,
        "worst_slack_ns": round(worst_slack, 5) if worst_slack is not None else None,
        "total_negative_slack_ns": round(tns, 5) if tns is not None else None,
        "fmax_mhz": round(fmax_hz / 1e6, 4) if fmax_hz is not None else None,
        "setup_violation_count": setup_violation_count,
        "hold_violation_count": hold_violation_count,
        "clock_skew_ns": round(clock_skew, 5) if clock_skew is not None else None,
        "estimated_power_mw": (
            round(power_w * 1000, 4) if power_w is not None else None
        ),
        "provenance": provenance,
    }

    if spef_path is not None:
        nets_check = _count_spef_nets_annotated(completed.stdout)
        nets_annotated, nets_total, design_nets_annotated, design_nets_total = (
            nets_check
            if nets_check is not None
            else (0, len(spef_net_names or []), 0, 0)
        )
        annotation_complete = (
            design_nets_total > 0 and design_nets_annotated == design_nets_total
        )
        annotation_warning = None
        design_nets_missing_sample: list[str] = []
        if not annotation_complete:
            annotation_warning = (
                f"only {design_nets_annotated} of {design_nets_total} nets in "
                "the linked design are named by this SPEF -- the "
                "worst_slack_ns/etc. fields above are NOT a real-parasitics "
                "measurement to the extent annotation is missing."
            )
            design_nets_missing_sample = _parse_spef_missing_nets(completed.stdout)
        response["spef_annotation"] = {
            "nets_annotated": nets_annotated,
            "nets_total": nets_total,
            "design_nets_annotated": design_nets_annotated,
            "design_nets_total": design_nets_total,
            "design_nets_missing_sample": design_nets_missing_sample,
            "annotation_complete": annotation_complete,
            "annotation_warning": annotation_warning,
        }
    else:
        response["spef_annotation"] = None

    return response


# --------------------------------------------------------------------------- #
# Request-field resolution
# --------------------------------------------------------------------------- #


def _resolve_def(def_field: Any, request_dir: str) -> str:
    if not isinstance(def_field, str) or not def_field:
        raise PostRouteStaError("request.def must be a non-empty string")
    path = (
        def_field if os.path.isabs(def_field) else os.path.join(request_dir, def_field)
    )
    if not os.path.isfile(path):
        raise PostRouteStaError(f"def not found: {def_field}")
    try:
        with open(path, "rb"):
            pass
    except OSError as exc:
        raise PostRouteStaError(f"could not read def '{def_field}': {exc}") from exc
    return os.path.abspath(path)


def _resolve_spef(spef_field: Any, request_dir: str) -> str | None:
    if spef_field is None:
        return None
    if not isinstance(spef_field, str) or not spef_field:
        raise PostRouteStaError("request.spef must be a non-empty string when given")
    path = (
        spef_field
        if os.path.isabs(spef_field)
        else os.path.join(request_dir, spef_field)
    )
    if not os.path.isfile(path):
        raise PostRouteStaError(f"spef not found: {spef_field}")
    return os.path.abspath(path)


def _validate_constraints(constraints: Any) -> tuple[str, float]:
    """Unlike ``place_and_route.py``'s own ``_validate_constraints`` (where
    a clock is optional -- a ``target_stage: "floorplan"`` run has no
    meaningful clock yet), a standalone STA run has no meaning *without* a
    clock: there is no stage short of "timed" to fall back to. Both fields
    are therefore required here, not just required-together."""
    if not isinstance(constraints, dict):
        raise PostRouteStaError("request.constraints must be a JSON object")
    clock_port = constraints.get("clock_port")
    clock_period_ns = constraints.get("clock_period_ns")
    if not (isinstance(clock_port, str) and clock_port):
        raise PostRouteStaError("request.constraints.clock_port is required")
    if (
        isinstance(clock_period_ns, bool)
        or not isinstance(clock_period_ns, (int, float))
        or clock_period_ns <= 0
    ):
        raise PostRouteStaError(
            "request.constraints.clock_period_ns must be a positive number"
        )
    return clock_port, float(clock_period_ns)


def _resolve_liberty(
    cell_library: str,
    requested_corner: str | None,
    *,
    variant: str | None = None,
    root: str | None = None,
) -> tuple[str, str, dict[str, Any]]:
    """Resolve ``(liberty_path, corner, pdk_info)`` for ``cell_library`` --
    this module's own copy of ``place_and_route.py``'s identical resolution
    (each verb module in this repo is self-contained). Raises
    :class:`PostRouteStaError` (never
    :class:`~klayout_tools.pdk.PdkNotFoundError`)."""
    try:
        info = find_pdk(variant=variant, root=root)
    except PdkNotFoundError as exc:
        raise PostRouteStaError(str(exc)) from exc

    libs_ref = info["assets"]["libs_ref"]
    if libs_ref is None:
        raise PostRouteStaError(
            f"liberty not found for deck: resolved PDK install "
            f"'{info['variant']}' at '{info['root']}' ships no libs_ref asset"
        )

    lib_dir = os.path.join(libs_ref, cell_library)
    if not os.path.isdir(lib_dir):
        raise PostRouteStaError(
            f"liberty not found for deck: standard-cell library "
            f"'{cell_library}' not found under resolved PDK install "
            f"'{info['variant']}' at '{info['root']}'"
        )

    corner = requested_corner
    if corner is None:
        libraries = list_cell_libraries(variant=info["variant"], root=info["root"])
        entry = next(
            (lib for lib in libraries["libraries"] if lib["name"] == cell_library),
            None,
        )
        corner = entry["nominal_corner"] if entry else None
        if corner is None:
            raise PostRouteStaError(
                f"liberty not found for deck: could not determine a nominal "
                f"corner for '{cell_library}' -- pass request.pdk.corner "
                "explicitly"
            )

    liberty_path = os.path.join(lib_dir, "lib", f"{cell_library}__{corner}.lib")
    if not os.path.isfile(liberty_path):
        raise PostRouteStaError(
            f"liberty not found for deck: no '{corner}' corner for "
            f"'{cell_library}' under resolved PDK install '{info['variant']}' "
            f"(expected '{liberty_path}')"
        )
    return liberty_path, corner, info


def _resolve_lef(cell_library: str, pdk_info: dict[str, Any]) -> tuple[str, str]:
    """Resolve the tech + merged-cell LEF pair for ``cell_library``, pinned
    to the *same* PDK install/variant :func:`_resolve_liberty` already
    resolved (never re-searches). Raises :class:`PostRouteStaError` when
    either file is missing."""
    lefs = lef_files(cell_library, variant=pdk_info["variant"], root=pdk_info["root"])
    tech_lef = lefs["tech_lef"]
    cell_lef = lefs["cell_lef"]
    if tech_lef is None or cell_lef is None:
        missing = [
            name
            for name, path in (("tech_lef", tech_lef), ("cell_lef", cell_lef))
            if path is None
        ]
        raise PostRouteStaError(
            f"LEF not found for deck: standard-cell library '{cell_library}' "
            f"under resolved PDK install '{pdk_info['variant']}' at "
            f"'{pdk_info['root']}' is missing: {', '.join(missing)}"
        )
    return tech_lef, cell_lef


# --------------------------------------------------------------------------- #
# Tcl script generation
# --------------------------------------------------------------------------- #


def _write_script(script_path: str, lines: list[str]) -> None:
    try:
        with open(script_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError as exc:
        raise PostRouteStaError(
            f"could not write sta script '{script_path}': {exc}"
        ) from exc


def _clock_lines(clock_port: str, clock_period_ns: float) -> list[str]:
    return [
        f"create_clock -name {clock_port} -period {clock_period_ns} "
        f"[get_ports {clock_port}]"
    ]


def _tcl_net_list(names: list[str]) -> str:
    """Render ``names`` as a brace-quoted Tcl list body -- see
    ``place_and_route.py``'s identical ``_tcl_net_list`` docstring for the
    escaping rationale (brace-quoting, not glob-escaping)."""
    return " ".join("{" + name + "}" for name in names)


def _spef_net_check_lines(net_names: list[str]) -> list[str]:
    """The net-name-correlation sanity check (``place_and_route.py``'s
    ``_spef_sta_script_lines`` docstring explains the rationale in full):
    a caller-supplied ``spef`` has the identical name-mismatch risk a
    ``klt extract --parasitics``-produced one does, so this reuses the same
    two-directional (SPEF-side / design-side) measurement, run **before**
    ``read_spef`` so an uncaught Tcl error partway through that call cannot
    also silently skip this check.

    ``net_names`` must already be *unescaped* (real design spelling, e.g.
    ``a[10]``/``u_sub/net``, not SPEF's ``a\\[10\\]``/``u_sub\\/net``) --
    :func:`_spef_net_names` guarantees this. ``get_full_name`` (design side)
    never returns SPEF's backslash-escaped spelling, so a still-escaped
    ``klt_spef_have`` key would silently fail to match any design net whose
    name contains a SPEF-reserved character (issue #1422).

    Also collects a capped sample (:data:`_SPEF_MISSING_NETS_SAMPLE_LIMIT`)
    of design-side net names that fail to correlate, so a caller can see
    *which* nets are missing rather than only the aggregate counts."""
    return [
        f"set klt_spef_nets [list {_tcl_net_list(net_names)}]",
        "set klt_spef_annotated 0",
        "foreach klt_spef_net $klt_spef_nets {",
        "    set klt_spef_have($klt_spef_net) 1",
        "    if {[llength [get_nets -quiet $klt_spef_net]] > 0} {",
        "        incr klt_spef_annotated",
        "    }",
        "}",
        "set klt_design_total 0",
        "set klt_design_annotated 0",
        "set klt_design_missing {}",
        f"set klt_design_missing_limit {_SPEF_MISSING_NETS_SAMPLE_LIMIT}",
        "foreach klt_design_net [get_nets -quiet *] {",
        "    incr klt_design_total",
        "    if {[info exists klt_spef_have([get_full_name $klt_design_net])]} {",
        "        incr klt_design_annotated",
        "    } elseif {[llength $klt_design_missing] < $klt_design_missing_limit} {",
        "        lappend klt_design_missing [get_full_name $klt_design_net]",
        "    }",
        "}",
        f'puts "{_SPEF_NET_CHECK_BEGIN}"',
        'puts "$klt_spef_annotated [llength $klt_spef_nets]"',
        'puts "$klt_design_annotated $klt_design_total"',
        f'puts "{_SPEF_NET_CHECK_END}"',
        f'puts "{_SPEF_MISSING_NETS_BEGIN}"',
        "foreach klt_missing_net $klt_design_missing {",
        "    puts $klt_missing_net",
        "}",
        f'puts "{_SPEF_MISSING_NETS_END}"',
    ]


def _violation_count_lines() -> list[str]:
    return [
        f'puts "{_SETUP_VIOLATIONS_BEGIN}"',
        "report_check_types -max_delay -violators -format end",
        f'puts "{_SETUP_VIOLATIONS_END}"',
        f'puts "{_HOLD_VIOLATIONS_BEGIN}"',
        "report_check_types -min_delay -violators -format end",
        f'puts "{_HOLD_VIOLATIONS_END}"',
    ]


def _sta_script_lines(
    *,
    tech_lef: str,
    cell_lef: str,
    def_path: str,
    liberty_path: str,
    clock_port: str,
    clock_period_ns: float,
    spef_path: str | None = None,
    spef_net_names: list[str] | None = None,
) -> list[str]:
    """Build the Tcl script for the single, from-scratch OpenSTA session
    this verb runs: load the LEF/DEF pair directly (no netlist, no
    ``link_design``, and deliberately **no** ``-floorplan_initialize`` --
    that flag is ``place_and_route.py``'s own floorplan-*stage* load of a
    caller-supplied DEF as a starting point for further implementation; this
    verb's ``read_def`` instead loads an already-complete, already-routed
    design as the analysis target itself, the one and only geometry this
    session ever times), then ``read_liberty``/``create_clock`` and,
    optionally, a caller-supplied ``spef``.
    """
    lines = [
        f"read_lef {tech_lef}",
        f"read_lef {cell_lef}",
        f"read_def {def_path}",
        f"read_liberty {liberty_path}",
    ]
    lines += _clock_lines(clock_port, clock_period_ns)
    if spef_path is not None:
        lines += _spef_net_check_lines(spef_net_names or [])
        lines.append(f"read_spef {spef_path}")
    lines += [
        "report_worst_slack_metric -setup",
        "report_tns_metric -setup",
        "report_fmax_metric",
        "report_power_metric",
        "report_clock_skew_metric -setup",
    ]
    lines += _violation_count_lines()
    return lines


# --------------------------------------------------------------------------- #
# SPEF net-name parsing
# --------------------------------------------------------------------------- #


def _unescape_spef_name(name: str) -> str:
    """The exact inverse of ``extract_spef.py``'s ``_spef_name()``: strips
    the backslash SPEF's own (IEEE 1481-1999) identifier grammar requires
    before every *special* character (anything outside ``[A-Za-z0-9_]``),
    e.g. ``a\\[10\\]`` -> ``a[10]``, ``u_sub\\/net`` -> ``u_sub/net``.

    A SPEF-declared ``*D_NET`` name is written *escaped*
    (:func:`extract_spef._spef_name`'s own docstring: "Reading tools strip
    the backslashes back off, so the name an STA session matches against its
    own netlist is the unescaped one"). :func:`_spef_net_names` must apply
    this before a recovered name is used as an OpenSTA ``get_nets``/Tcl
    array-key value, or every name containing a SPEF-reserved character
    (bus-index brackets, hierarchical-path slashes, ...) silently fails to
    correlate against the design's real, unescaped net names (issue #1422:
    measured ~51% vs. a true ~99.5% structural agreement on a real routed
    design, split exactly along "name contains `[`/`]`/`/`")."""
    return _SPEF_ESCAPED_CHAR_RE.sub(r"\1", name)


def _spef_net_names(spef_path: str) -> list[str]:
    """Recover the net *name set* a caller-supplied ``spef`` file declares,
    by scanning its own ``*D_NET <name> <total_cap>`` lines (the SPEF net-
    declaration line every writer -- including this repo's own
    ``extract.py::_write_spef`` -- emits one of per net), then un-escaping
    each recovered name (:func:`_unescape_spef_name`) back to its real,
    design-side spelling. Used only to feed :func:`_spef_net_check_lines`'s
    correlation check; never to parse capacitance/resistance values. Raises
    :class:`PostRouteStaError` if the file cannot be read."""
    try:
        with open(spef_path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError as exc:
        raise PostRouteStaError(f"could not read spef '{spef_path}': {exc}") from exc
    return sorted({_unescape_spef_name(name) for name in _SPEF_D_NET_RE.findall(text)})


# --------------------------------------------------------------------------- #
# OpenROAD subprocess invocation + output parsing
# --------------------------------------------------------------------------- #


def _run_openroad(script_path: str, metrics_path: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["openroad", "-no_init", "-exit", "-metrics", metrics_path, script_path],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise PostRouteStaError(f"could not launch openroad: {exc}") from exc


def _engine_error_message(completed: subprocess.CompletedProcess) -> str:
    """Build an actionable error message from a failed OpenROAD run.

    Prefers a bracketed ``[ERROR ...]`` diagnostic OpenROAD itself printed
    over a bare ``Error:`` trailer line, mirroring
    ``place_and_route.py``'s own ``_engine_error_message`` (minus that
    function's route-stage-only ``DRT-0305`` diagnosis, which cannot occur
    here -- this verb never runs TritonRoute)."""
    bracket_lines: list[str] = []
    bare_error_lines: list[str] = []
    for stream in (completed.stdout or "", completed.stderr or ""):
        for line in stream.splitlines():
            stripped = line.strip()
            if "[ERROR" in stripped:
                bracket_lines.append(stripped)
            elif stripped.startswith("Error:"):
                bare_error_lines.append(stripped)

    if bracket_lines:
        return f"openroad sta run failed: {bracket_lines[0]}"
    if bare_error_lines:
        return f"openroad sta run failed: {bare_error_lines[-1]}"

    tail_source = (completed.stderr or completed.stdout or "").strip().splitlines()
    snippet = " ".join(tail_source[-3:]) if tail_source else "no output captured"
    return f"openroad sta run exited with code {completed.returncode}: {snippet}"


def _read_metrics(metrics_path: str) -> dict[str, Any]:
    if not os.path.isfile(metrics_path):
        raise PostRouteStaError(
            "openroad exited successfully but did not produce the expected "
            f"sta metrics file '{metrics_path}'"
        )
    try:
        with open(metrics_path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, UnicodeDecodeError) as exc:
        raise PostRouteStaError(
            f"could not read sta metrics '{metrics_path}': {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise PostRouteStaError(
            f"sta metrics '{metrics_path}' is not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise PostRouteStaError(
            f"sta metrics '{metrics_path}' must contain a JSON object"
        )
    return data


def _count_violations(stdout: str, begin: str, end: str) -> int:
    try:
        start_idx = stdout.index(begin) + len(begin)
        stop_idx = stdout.index(end, start_idx)
    except ValueError:
        return 0
    return stdout[start_idx:stop_idx].count("(VIOLATED)")


def _count_spef_nets_annotated(stdout: str) -> tuple[int, int, int, int] | None:
    """``(nets_annotated, nets_total, design_nets_annotated,
    design_nets_total)`` parsed from :func:`_spef_net_check_lines`'s own
    ``===KLT_STA_SPEF_NET_CHECK_BEGIN===``/``===END===``-delimited stdout
    block, or ``None`` when the markers aren't found (defensive; should not
    happen for a successful run)."""
    try:
        start_idx = stdout.index(_SPEF_NET_CHECK_BEGIN) + len(_SPEF_NET_CHECK_BEGIN)
        stop_idx = stdout.index(_SPEF_NET_CHECK_END, start_idx)
    except ValueError:
        return None
    match = _SPEF_NET_CHECK_RE.search(stdout[start_idx:stop_idx])
    if match is None:
        return None
    return (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
        int(match.group(4)),
    )


def _parse_spef_missing_nets(stdout: str) -> list[str]:
    """A capped sample of design-side net names that failed to correlate
    against the SPEF's own declared name set, parsed from
    :func:`_spef_net_check_lines`'s own
    ``===KLT_STA_SPEF_MISSING_NETS_BEGIN===``/``===END===``-delimited stdout
    block (one net name per line, already the design's real spelling --
    ``get_full_name``'s own output). Returns ``[]`` when the markers aren't
    found (defensive; should not happen for a successful run) or the block
    is empty (nothing missing)."""
    try:
        start_idx = stdout.index(_SPEF_MISSING_NETS_BEGIN) + len(
            _SPEF_MISSING_NETS_BEGIN
        )
        stop_idx = stdout.index(_SPEF_MISSING_NETS_END, start_idx)
    except ValueError:
        return []
    block = stdout[start_idx:stop_idx]
    return [line.strip() for line in block.splitlines() if line.strip()]


def _openroad_version() -> str | None:
    """The resolved OpenROAD build string, or ``None`` if unresolvable --
    never raises. Mirrors ``place_and_route.py``'s own ``_openroad_version``."""
    try:
        completed = subprocess.run(
            ["openroad", "-version"], capture_output=True, text=True
        )
    except OSError:
        return None
    stdout = completed.stdout.strip()
    if not stdout:
        return None
    banner_match = _OPENROAD_VERSION_RE.search(stdout)
    if banner_match:
        return banner_match.group(1)
    return stdout.split()[0]

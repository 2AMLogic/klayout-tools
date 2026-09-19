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

Deliberately duplicates a couple of small helpers already defined in
``place_and_route.py`` (``_clock_lines``, ``_read_metrics``) rather than
importing them -- this repo's own stated convention (see
``place_and_route.py``'s ``_resolve_layer_map`` docstring) is that each verb
module stays self-contained. Every existing cross-module import between verb
modules in this repo is of a *public* name (``run_place_and_route``,
``PlaceAndRouteError``, ``run_extract``, ...), never a private one.
``_run_openroad``, ``_count_violations``, ``_openroad_version``, and (issue
#1703) the SPEF net-name-correlation Tcl helpers ``_tcl_net_list``/
``_count_spef_nets_annotated`` are the exceptions: those were byte-identical
(modulo exception class/docstring length, or -- for
``_count_spef_nets_annotated`` -- module-local marker constants) between this
module and ``place_and_route.py`` with no caller-visible behavioral
difference worth preserving per-copy, so they were consolidated into the
shared ``_openroad_engine``/``_paths.py`` internal utility modules both
import from (issues #1637/#1703) -- following the same precedent as
``_paths.py`` (issue #642). ``_openroad_engine``/``_paths.py`` are shared
utility modules, not verb modules, so importing their (underscore-prefixed)
helpers by name does not violate the convention above. ``_resolve_liberty``
(this module's own thin wrapper, below) follows the same precedent one level
further: its body is a one-line call into
:func:`klayout_tools.pdk.resolve_liberty_for_cell_library`, a *public* name
in the ``pdk.py`` shared utility module (issue #1652) -- PDK-resolution
logic is the one part of the "self-contained" convention that must not
drift silently between verb modules, since a resolution bug in one copy
(issue #1790's missed ``post_route_sta.py`` fallback) is a live correctness
bug, not just duplicated code.

Scope deliberately excluded from this first version (tracked as follow-up,
not required for this issue): a ``propagated_clock`` request option (the
in-flow STA -- and this module -- both time an ideal SDC-only clock even
once a real clock tree exists) and a bisected (rather than
``report_fmax_metric``'s ``1/(T-WNS)`` extrapolated) ``fmax_mhz``.

**Pre-route DEFs (issue #1826).** Nothing about this module's OpenSTA session
construction (``read_lef`` x2, ``read_def`` -- no ``-floorplan_initialize``,
``read_liberty``, ``create_clock``) actually requires the DEF to be *routed*
specifically; a placement- or CTS-stage DEF (``klt place-and-route``'s own
``unrouted_def_path``, populated when ``target_stage`` is ``"place"`` or
``"cts"``) loads and times the same way. This closes issue #1826's "gap 1":
a caller wanting an SDC-driven, clock-constrained setup/hold slack number
*before* a full route now has a real path to one, reusing the DEF
``place_and_route.py``'s own ``"place"``/``"cts"`` stages already write
(issue #785 originally kept the ``"place"``-stage one internal-only; #1826
reverses that for exactly this use case) rather than giving this module a
from-scratch netlist-input mode (the alternative shape issue #1825 proposes
for the same gap -- see that issue for the cross-reference and rationale).

Because a bare DEF file carries no metadata declaring which stage produced
it, this module cannot infer "was this routed" on its own -- the caller must
say so via the optional ``request.geometry_source`` field
(``"routed"``, the default, vs. ``"placement_estimate"``), echoed back
verbatim as the response's own ``geometry_source`` field. This exists
specifically so a response built on a pre-route DEF is never silently
shaped identically to a routed, detailed-SPEF-eligible signoff result: a
placement-stage (or CTS-stage) DEF's parasitics come from
``estimate_parasitics -placement`` (a placement/bounding-box estimate), not
routing-derived RC, so its ``worst_slack_ns``/``worst_hold_slack_ns``/etc.
are a real number but a less accurate one than the same fields on a routed
DEF -- ``geometry_source: "placement_estimate"`` is the caller's own
declaration of that distinction, machine-readable rather than left to prose.

**From-scratch netlist input (issue #1825).** #1826 (above) closes the gap
for a caller already willing to invoke ``klt place-and-route`` (even if only
as far as its ``"place"``/``"cts"`` stage). This closes the *stricter* gap:
an SDC-constrained setup/hold slack number reachable from a synthesized
structural Verilog netlist alone, with **no place-and-route invocation of
any kind** -- no floorplan, no macro placement, no PDN. The request's
``def``/``verilog`` fields are mutually exclusive alternate geometry
sources (exactly one required); when ``verilog`` is given, this module runs
``read_lef`` x2 -> ``read_verilog`` -> ``link_design`` -> ``create_clock``
instead of ``read_def`` -- the same sequence ``place_and_route.py``'s own
``"floorplan"``-stage load already runs (its docstring explains the
liberty-before-LEF-before-verilog ordering), minus that stage's
floorplan-init/macro-placement/PDN steps, which have no meaning without any
placement geometry at all.

With no placement and no routing, there is no *measured* wire parasitic of
any kind to load -- unlike the ``"placement_estimate"``/``"routed"`` modes
above, which both load a real (if approximate) geometry. The only estimate
available is OpenSTA's own liberty-driven wire-load model
(``set_wire_load_mode``/``set_wire_load_model``, both request-optional via
``constraints.wire_load_model``/``constraints.wire_load_mode``) -- distinct
from both ``synthesize.py``'s ABC-derived ``WireLoad`` estimate (drives
ABC's own ``stime``, an entirely different tool from the OpenSTA session
this module runs) and ``place_and_route.py``'s ``estimate_parasitics
-placement``/``-global_routing`` (which require an actual placement to
measure a bounding box or global route from). This response labels itself
``geometry_source: "netlist_estimate"`` -- a third, distinct value from
``"routed"``/``"placement_estimate"`` -- so a consumer can never conflate a
from-scratch, wire-load-model-only estimate with either a real placement's
bounding-box RC or a routed design's actual RC. ``response.wire_load_model``/
``wire_load_mode`` echo exactly what was (or was not) requested, mirroring
this repo's existing precedent for a provenance-bearing estimate knob
(``sta.py``'s ``compute_critical_path`` echoing its own
``input_transition_ns``/``output_load_pf`` boundary condition).

A caller-supplied ``spef`` is rejected together with ``verilog`` -- there is
no routed or placement geometry in this mode for a SPEF to annotate real
parasitics onto.

**Multi-corner characterization (issue #1871).** ``request.pdk.corners`` (a
list) is the additive, multi-corner alternative to the scalar
``request.pdk.corner`` documented throughout this module -- the two are
mutually exclusive (:func:`_validate_corners`). Before this field existed,
this command's own reason to exist (characterizing *one fixed geometry* at N
corners) still required a caller to hand-roll an external loop of N separate
``klt sta`` invocations, each re-reading the same LEF/DEF and re-parsing a
liberty file from scratch, and none of which could itself assert the one
invariant the whole exercise depends on: that every run actually
characterized the *same* geometry. A single-corner response can never prove
that -- each invocation only ever sees its own run.

``pdk.corners`` closes that gap natively: :func:`_run_multi_corner` runs
:func:`_run_corner_session` once per requested corner name (the same
complete, fresh-session mechanics the scalar path always has), hashes
``def``/``verilog`` once before and once after the loop to assert it never
changed mid-run, and returns a single response with every
structurally-shared field (``def_path``/``verilog_path``/
``geometry_source``/``wire_load_model``/``wire_load_mode``/``spef_path``,
``provenance.pdk``/``.input``) hoisted to the top level and a ``corners``
array carrying each corner's own name, slack/power/violation fields, and
``deck`` provenance. See ``docs/cli/sta.md``'s "Multi-corner
characterization" section for the full request/response shape.

This is deliberately N separate engine sessions, not one shared
``define_corners``/``read_liberty -corner`` session with the LEF/DEF loaded
once: ``place_and_route.py``'s own post-route corner sweep
(``pdk.sweep_corners``, issue #1092) already established, against a real
OpenROAD session, that ``report_worst_slack_metric`` has no way to scope its
result back to one corner once more than one is loaded -- so recovering
distinct per-corner numbers needs one engine invocation per corner either
way. A deeper optimization (checkpointing the loaded LEF/DEF once via
``write_db``/``read_db``, the way that same sweep reuses the ``"route"``
stage's own already-loaded design, so only the liberty deck differs per
invocation) is tracked as follow-up work, not required for this field's
initial scope.
"""

from __future__ import annotations

import json
import os
import re
import subprocess  # noqa: F401 -- tests patch post_route_sta.subprocess.run
from typing import Any

from ._openroad_engine import (
    _count_violations,
    _openroad_version,
    _OpenRoadResult,
    _run_openroad,
    _timing_status,
)
from ._paths import (
    _count_spef_nets_annotated,
    _load_request_json,
    _tcl_net_list,
    validate_request_shape,
)
from ._provenance import (
    INPUT_ROLE_LAYOUT,
    INPUT_ROLE_NETLIST,
    _deck_block,
    build_provenance,
    sha256_file,
)
from .pdk import lef_files
from .pdk_cells import resolve_liberty_for_cell_library

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

#: Brackets the ``read_spef`` call itself, so every diagnostic OpenSTA's own
#: SPEF *reader* emits while parsing the file lands in one delimited stdout
#: region -- see :func:`_spef_reader_warnings` (issue #1624).
_SPEF_READ_BEGIN = "===KLT_STA_SPEF_READ_BEGIN==="
_SPEF_READ_END = "===KLT_STA_SPEF_READ_END==="
#: Brackets the two ``report_checks`` delay fingerprints (before/after
#: ``read_spef``) whose byte-comparison backs ``delay_changed`` -- see
#: :func:`_delay_fingerprint_lines` and :func:`_parse_delay_changed`.
_DELAY_PRE_BEGIN = "===KLT_STA_DELAY_PRE_BEGIN==="
_DELAY_PRE_END = "===KLT_STA_DELAY_PRE_END==="
_DELAY_POST_BEGIN = "===KLT_STA_DELAY_POST_BEGIN==="
_DELAY_POST_END = "===KLT_STA_DELAY_POST_END==="
#: Brackets ``report_parasitic_annotation``'s own output -- OpenSTA's
#: *post*-``read_spef`` account of what it actually holds. See
#: :func:`_parasitic_annotation_lines` / :func:`_parse_parasitic_annotation`.
_PARASITIC_ANNOTATION_BEGIN = "===KLT_STA_PARASITIC_ANNOTATION_BEGIN==="
_PARASITIC_ANNOTATION_END = "===KLT_STA_PARASITIC_ANNOTATION_END==="
#: Any OpenSTA-tagged warning. Only ever matched *inside* the
#: :data:`_SPEF_READ_BEGIN`/:data:`_SPEF_READ_END` stdout region, where the
#: SPEF reader is the only thing running.
_STA_WARNING_RE = re.compile(r"\[WARNING STA-\d+\]")
#: The SPEF reader's own two warnings -- ``STA-1650`` (``net <name> not
#: found.``) and ``STA-1648`` (``instance <name>:<pin> not found.``). Matched
#: stream-wide (not region-delimited) because OpenROAD builds differ in
#: whether the logger writes to stdout or stderr, and stderr cannot be
#: interleaved with the Tcl ``puts`` markers above.
_SPEF_READER_WARNING_RE = re.compile(r"\[WARNING STA-(?:1648|1650)\]")
#: Upper bound on how many verbatim reader-warning lines are echoed into the
#: response -- a diagnostic sample, not an exhaustive log (a wholesale
#: name-convention mismatch emits one per SPEF net).
_SPEF_READER_WARNING_SAMPLE_LIMIT = 5
#: ``report_parasitic_annotation``'s own two summary lines. OpenSTA prints
#: the plural noun unconditionally (``Found 1 unannotated drivers.``); the
#: optional ``s`` is tolerated anyway.
_UNANNOTATED_DRIVERS_RE = re.compile(r"Found\s+(\d+)\s+unannotated\s+drivers?\.")
_PARTIAL_DRIVERS_RE = re.compile(
    r"Found\s+(\d+)\s+partially\s+unannotated\s+drivers?\."
)
#: ``report_checks`` prints this (and nothing else) for a design with no
#: timing path to report -- an empty fingerprint pair that must degrade
#: ``delay_changed`` to ``null`` (unknown), never to ``false``.
_NO_PATHS_FOUND = "no paths found"

#: Issue #1826: the two ``request.geometry_source`` values valid for a
#: ``def``-mode request -- ``"routed"`` (the default, this command's
#: original and only behaviour) declares the ``def`` a fully-routed,
#: detailed-SPEF-eligible signoff geometry; ``"placement_estimate"``
#: declares it a pre-route (placement- or CTS-stage) DEF whose parasitics
#: are a bounding-box estimate, not routing-derived RC. See this module's
#: own docstring "Pre-route DEFs" section for the full rationale.
#: ``"netlist_estimate"`` (issue #1825) is the third, ``verilog``-mode-only
#: value -- validated separately in :func:`run_sta` (not through this
#: tuple/:func:`_validate_geometry_source`) since it is the *only* legal
#: value in that mode, never a caller choice among several.
_GEOMETRY_SOURCES = ("routed", "placement_estimate")

#: Issue #1825: the ``constraints.wire_load_mode`` values OpenSTA/Liberty's
#: wire-load-model mechanism accepts (``set_wire_load_mode``) -- only
#: meaningful alongside ``constraints.wire_load_model`` in a ``verilog``-mode
#: request (see this module's own docstring "From-scratch netlist input"
#: section).
_WIRE_LOAD_MODES = ("top", "enclosed", "segmented")


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
    not valid JSON, or missing a required top-level field (``pdk``,
    ``constraints``). Does not require a ``schema`` field, matching every
    other request-taking verb's ``load_request``.

    Unlike ``pdk``/``constraints``, neither ``def`` nor ``verilog`` is a
    required top-level field here -- exactly one of them is required, a
    relationship :func:`validate_request_shape`'s flat "these keys must all
    be present" check cannot express. :func:`run_sta` validates that
    mutual-exclusion/require-one-of relationship itself, right after calling
    this function (see its own "Request-field resolution" section, issue
    #1825).
    """
    request = _load_request_json(request_path, PostRouteStaError)
    return validate_request_shape(
        request,
        "request file",
        error_cls=PostRouteStaError,
        required_fields=("pdk", "constraints"),
    )


def run_sta(
    request_path: str,
    *,
    pdk_variant: str | None = None,
    pdk_root: str | None = None,
) -> dict[str, Any]:
    """Run a standalone OpenSTA timing/power analysis over the geometry
    declared by the request at ``request_path`` -- either an already-routed
    (or pre-route, issue #1826) DEF (``request.def``), or, for issue #1825's
    from-scratch mode, a structural Verilog netlist (``request.verilog``)
    linked directly with no DEF at all. Exactly one of the two is required;
    see this module's own docstring for the full rationale of each mode.

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

    # Issue #1825: `def`/`verilog` are mutually exclusive alternate geometry
    # sources -- exactly one is required (neither is a `load_request`-level
    # required field; see that function's own docstring for why).
    has_def = "def" in request
    has_verilog = "verilog" in request
    if has_def and has_verilog:
        raise PostRouteStaError(
            "request must not include both 'def' and 'verilog' -- provide "
            "exactly one geometry source"
        )
    if not has_def and not has_verilog:
        raise PostRouteStaError("request must include one of 'def' or 'verilog'")

    def_path: str | None = None
    verilog_path: str | None = None
    if has_def:
        def_path = _resolve_def(request["def"], request_dir)
    else:
        verilog_path = _resolve_verilog(request["verilog"], request_dir)

    hdl_toplevel = request.get("hdl_toplevel")
    if has_verilog:
        # Unlike the `def`-mode echo-only field below, `verilog` mode
        # actually needs this for `link_design` -- required, not optional.
        if not isinstance(hdl_toplevel, str) or not hdl_toplevel:
            raise PostRouteStaError(
                "request.hdl_toplevel is required when request.verilog is given"
            )
    elif hdl_toplevel is not None and (
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
    # Issue #1871: `pdk.corners` (a list) is the additive, multi-corner
    # alternative to the scalar `pdk.corner` above -- see this module's own
    # docstring "Multi-corner characterization" section. Shape-validated
    # here; membership needs no further validation (unlike
    # `place_and_route.py`'s `sweep_corners`, which narrows an
    # already-derived shipped-corner set, `corners` here *is* the full
    # corner selection, and an unresolvable name simply fails the same way
    # an unresolvable scalar `pdk.corner` already does, per-corner, inside
    # the run loop below).
    requested_corners = _validate_corners(pdk_spec.get("corners"))
    if requested_corner is not None and requested_corners is not None:
        raise PostRouteStaError(
            "request.pdk.corner and request.pdk.corners are mutually "
            "exclusive -- give at most one: a single scalar corner for the "
            "existing single-corner response shape, or a list for "
            "multi-corner characterization (see docs/cli/sta.md)"
        )

    (
        clock_port,
        clock_period_ns,
        input_delay_ns,
        output_delay_ns,
    ) = _validate_constraints(request["constraints"])
    wire_load_model, wire_load_mode = _validate_wire_load_estimate(
        request["constraints"], has_verilog
    )

    if has_verilog:
        if request.get("spef") is not None:
            raise PostRouteStaError(
                "request.spef is not supported together with request.verilog "
                "-- there is no routed or placement geometry in this mode "
                "for a spef to annotate real parasitics onto"
            )
        spef_path = None
        requested_geometry_source = request.get("geometry_source")
        if requested_geometry_source not in (None, "netlist_estimate"):
            raise PostRouteStaError(
                "request.geometry_source must be 'netlist_estimate' (or "
                "omitted) when request.verilog is given"
            )
        geometry_source = "netlist_estimate"
    else:
        spef_path = _resolve_spef(request.get("spef"), request_dir)
        geometry_source = _validate_geometry_source(request.get("geometry_source"))

    output_dir = os.path.join(request_dir, ".klt", "sta")
    try:
        os.makedirs(output_dir, exist_ok=True)
    except OSError as exc:
        raise PostRouteStaError(
            f"could not create output directory '{output_dir}': {exc}"
        ) from exc

    input_path = def_path if has_def else verilog_path
    assert input_path is not None
    basename = os.path.splitext(os.path.basename(input_path))[0]

    # Issue #1871: `pdk.corners` (a list) is the multi-corner alternative to
    # the scalar `pdk.corner` above -- both share every request field
    # resolved up to this point (`def`/`verilog` are loaded fresh in *every*
    # corner's own OpenSTA session, never modified in between), and this is
    # the one branch point where their responses actually diverge.
    if requested_corners is not None:
        return _run_multi_corner(
            requested_corners,
            cell_library=cell_library,
            pdk_variant=pdk_variant,
            pdk_root=pdk_root,
            has_def=has_def,
            def_path=def_path,
            verilog_path=verilog_path,
            hdl_toplevel=hdl_toplevel,
            geometry_source=geometry_source,
            clock_port=clock_port,
            clock_period_ns=clock_period_ns,
            input_delay_ns=input_delay_ns,
            output_delay_ns=output_delay_ns,
            spef_path=spef_path,
            wire_load_model=wire_load_model,
            wire_load_mode=wire_load_mode,
            output_dir=output_dir,
            input_path=input_path,
            basename=basename,
        )

    corner_fields, resolution = _run_corner_session(
        corner_name=requested_corner,
        cell_library=cell_library,
        pdk_variant=pdk_variant,
        pdk_root=pdk_root,
        has_def=has_def,
        def_path=def_path,
        verilog_path=verilog_path,
        hdl_toplevel=hdl_toplevel,
        clock_port=clock_port,
        clock_period_ns=clock_period_ns,
        input_delay_ns=input_delay_ns,
        output_delay_ns=output_delay_ns,
        spef_path=spef_path,
        wire_load_model=wire_load_model,
        wire_load_mode=wire_load_mode,
        output_dir=output_dir,
        script_tag=basename,
    )

    engine_version = _openroad_version()
    deck_name = f"{cell_library}__{resolution['corner']}"
    provenance = build_provenance(
        deck_name=deck_name,
        deck_path=resolution["liberty_path"],
        pdk=resolution["pdk_info"],
        input_path=input_path,
        # Issue #2027: `input_path` is the DEF (a layout stream) when
        # the request supplied one, and the gate-level Verilog netlist
        # otherwise -- the two are not comparable artifacts, so the
        # role follows the same `has_def` branch `input_path` itself
        # does.
        input_role=INPUT_ROLE_LAYOUT if has_def else INPUT_ROLE_NETLIST,
    )

    response: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "engine": "openroad",
        "engine_version": engine_version,
        "hdl_toplevel": hdl_toplevel,
        "status": "ok",
        "def_path": def_path,
        # Additive field (issue #1825): `null` unless `request.verilog` was
        # given -- the mutually-exclusive counterpart to `def_path` above.
        "verilog_path": verilog_path,
        # Additive field (issue #1826): echo of `request.geometry_source` --
        # see this module's own docstring "Pre-route DEFs" section. Always
        # present (never `null`): `"routed"` is the default when the request
        # omits it, matching this command's pre-#1826 behaviour byte-for-
        # byte. Issue #1825 adds the third `"netlist_estimate"` value,
        # forced (never caller-chosen) whenever `request.verilog` is given.
        "geometry_source": geometry_source,
        # Additive fields (issue #1825): the wire-load estimate knob
        # actually used, for provenance -- `null`/`null` unless
        # `request.verilog` was given (see this module's own docstring
        # "From-scratch netlist input" section). Never present on a
        # `def`-mode response with anything but `null`/`null`, since a
        # `def`-mode run's parasitics never come from a liberty wire-load
        # model.
        "wire_load_model": wire_load_model,
        "wire_load_mode": wire_load_mode,
        "spef_path": spef_path,
        "worst_slack_ns": corner_fields["worst_slack_ns"],
        "total_negative_slack_ns": corner_fields["total_negative_slack_ns"],
        "worst_hold_slack_ns": corner_fields["worst_hold_slack_ns"],
        "total_negative_hold_slack_ns": corner_fields["total_negative_hold_slack_ns"],
        "fmax_mhz": corner_fields["fmax_mhz"],
        # Additive field (issue #1865): `"constrained"` / `"unconstrained"` /
        # `null` -- whether the four slack fields above are measurements at
        # all, or OpenSTA's own unconstrained-design sentinel (`1e+39`)
        # restated. A design whose only timing paths run input-port ->
        # register / register -> output-port reports that sentinel unless
        # `constraints.input_delay_ns`/`.output_delay_ns` are given, and
        # `1e+39` is a *positive* number -- so a naive `worst_slack_ns >= 0`
        # gate would otherwise report "timing closed" on a design that was
        # never timed. Check this field before trusting any slack number in
        # this response.
        "timing_status": corner_fields["timing_status"],
        "setup_violation_count": corner_fields["setup_violation_count"],
        "hold_violation_count": corner_fields["hold_violation_count"],
        "clock_skew_ns": corner_fields["clock_skew_ns"],
        "estimated_power_mw": corner_fields["estimated_power_mw"],
        "provenance": provenance,
    }

    response["spef_annotation"] = corner_fields["spef_annotation"]
    response["engine_log"] = corner_fields["engine_log"]

    return response


def _run_corner_session(
    *,
    corner_name: str | None,
    cell_library: str,
    pdk_variant: str | None,
    pdk_root: str | None,
    has_def: bool,
    def_path: str | None,
    verilog_path: str | None,
    hdl_toplevel: str | None,
    clock_port: str,
    clock_period_ns: float,
    input_delay_ns: float | None,
    output_delay_ns: float | None,
    spef_path: str | None,
    wire_load_model: str | None,
    wire_load_mode: str | None,
    output_dir: str,
    script_tag: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run one complete, fresh OpenSTA session (``read_lef`` x2, ``read_def``
    or ``read_verilog``/``link_design``, ``read_liberty``, ``create_clock``,
    optionally ``read_spef``) at ``corner_name`` (the nominal corner when
    ``None``), exactly like :func:`run_sta`'s single-corner path always has.

    Both the original scalar (``request.pdk.corner``) path and issue #1871's
    additive multi-corner (``request.pdk.corners``) path call this once per
    corner -- this is the extraction point that lets the latter reuse the
    former's exact engine-invocation/metrics-parsing logic instead of a
    second, independently-drifting copy.

    Returns ``(corner_fields, resolution)``:

    - ``corner_fields`` -- every field this command's response has always
      reported *per corner* (``worst_slack_ns``, ..., ``spef_annotation``) --
      the exact dict this module built inline before this refactor, now
      returned instead of assembled directly into ``response``.
    - ``resolution`` -- ``{"corner": str, "liberty_path": str, "pdk_info":
      dict}``, everything the caller needs to build its own
      ``provenance``/``deck`` block(s); kept separate from ``corner_fields``
      because the multi-corner caller hoists ``pdk_info`` to the top-level
      response once (shared across every corner) while nesting only
      ``deck`` per corner (see :func:`_run_multi_corner`).

    Raises :class:`PostRouteStaError` for an unresolvable
    ``cell_library``/``corner_name``/LEF, or an OpenROAD engine failure --
    identical to this command's pre-#1871 single-corner behaviour.
    """
    liberty_path, corner, pdk_info = _resolve_liberty(
        cell_library, corner_name, variant=pdk_variant, root=pdk_root
    )
    tech_lef, cell_lef = _resolve_lef(cell_library, pdk_info)

    script_path = os.path.join(output_dir, f"sta_{script_tag}.tcl")
    metrics_path = os.path.join(output_dir, f"{script_tag}_metrics.json")

    if has_def:
        assert def_path is not None
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
            input_delay_ns=input_delay_ns,
            output_delay_ns=output_delay_ns,
        )
    else:
        spef_net_names = None
        assert verilog_path is not None and hdl_toplevel is not None
        lines = _sta_netlist_script_lines(
            tech_lef=tech_lef,
            cell_lef=cell_lef,
            verilog_path=verilog_path,
            liberty_path=liberty_path,
            hdl_toplevel=hdl_toplevel,
            clock_port=clock_port,
            clock_period_ns=clock_period_ns,
            wire_load_model=wire_load_model,
            wire_load_mode=wire_load_mode,
            input_delay_ns=input_delay_ns,
            output_delay_ns=output_delay_ns,
        )
    _write_script(script_path, lines)

    completed = _run_openroad(script_path, metrics_path, error_cls=PostRouteStaError)
    with completed.diagnostics(PostRouteStaError):
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
    worst_hold_slack = metrics.get("timing__hold__ws")
    hold_tns = metrics.get("timing__hold__tns")
    fmax_hz = metrics.get("timing__fmax")
    power_w = metrics.get("power__total")
    clock_skew = metrics.get("clock__skew__setup")

    corner_fields: dict[str, Any] = {
        "engine_log": completed.engine_log,
        "worst_slack_ns": round(worst_slack, 5) if worst_slack is not None else None,
        "total_negative_slack_ns": round(tns, 5) if tns is not None else None,
        "worst_hold_slack_ns": (
            round(worst_hold_slack, 5) if worst_hold_slack is not None else None
        ),
        "total_negative_hold_slack_ns": (
            round(hold_tns, 5) if hold_tns is not None else None
        ),
        "fmax_mhz": round(fmax_hz / 1e6, 4) if fmax_hz is not None else None,
        "timing_status": _timing_status((worst_slack, worst_hold_slack)),
        "setup_violation_count": setup_violation_count,
        "hold_violation_count": hold_violation_count,
        "clock_skew_ns": round(clock_skew, 5) if clock_skew is not None else None,
        "estimated_power_mw": (
            round(power_w * 1000, 4) if power_w is not None else None
        ),
    }
    if spef_path is not None:
        corner_fields["spef_annotation"] = _spef_annotation_block(
            completed, spef_net_names
        )
    else:
        corner_fields["spef_annotation"] = None

    resolution = {"corner": corner, "liberty_path": liberty_path, "pdk_info": pdk_info}
    return corner_fields, resolution


def _run_multi_corner(
    corners: list[str],
    *,
    cell_library: str,
    pdk_variant: str | None,
    pdk_root: str | None,
    has_def: bool,
    def_path: str | None,
    verilog_path: str | None,
    hdl_toplevel: str | None,
    geometry_source: str,
    clock_port: str,
    clock_period_ns: float,
    input_delay_ns: float | None,
    output_delay_ns: float | None,
    spef_path: str | None,
    wire_load_model: str | None,
    wire_load_mode: str | None,
    output_dir: str,
    input_path: str,
    basename: str,
) -> dict[str, Any]:
    """Issue #1871: ``request.pdk.corners`` -- characterize the *same*
    loaded ``def``/``verilog`` geometry at every corner in ``corners``, one
    :func:`_run_corner_session` call per corner (each its own complete, fresh
    OpenSTA session -- this command has no checkpoint/``read_db`` mechanism
    of its own to share a loaded session across corners the way
    ``place_and_route.py``'s own post-route corner sweep does; see this
    module's own docstring "Multi-corner characterization" section for why
    that is an acceptable, explicitly-deferred first implementation rather
    than a blocking gap), returning the unified multi-corner response shape
    ``docs/cli/sta.md`` documents:

    - Every request-level field that cannot vary across corners (``def_path``/
      ``verilog_path``/``geometry_source``/``wire_load_model``/
      ``wire_load_mode``/``spef_path``, plus ``provenance.pdk``/``.input``)
      is hoisted to the **top level**, computed once.
    - ``corners`` is a list of per-corner entries, each carrying that
      corner's own name plus the exact same per-corner fields the
      single-corner (``request.pdk.corner``) response already has
      (``worst_slack_ns`` .. ``spef_annotation``), plus its own ``deck``
      provenance block (the one field that *does* vary per corner).

    **Verifies the shared-geometry invariant** this command exists to
    provide (docs/cli/sta.md's own "Why this exists" section): ``def``/
    ``verilog`` is hashed once before the corner loop and re-hashed once
    after it, raising :class:`PostRouteStaError` on a mismatch -- turning a
    hypothetical concurrent mutation of the input file mid-run (or a future
    refactor that stops holding this invariant) into a loud, attributable
    error instead of a `corners` array that silently characterizes more than
    one piece of geometry, the exact failure mode a caller's own hand-rolled
    N-subprocess loop could never itself detect.
    """
    input_hash_before = sha256_file(input_path)

    engine_version = _openroad_version()
    shared_pdk_info: dict[str, Any] | None = None
    corner_entries: list[dict[str, Any]] = []
    for corner_name in corners:
        corner_fields, resolution = _run_corner_session(
            corner_name=corner_name,
            cell_library=cell_library,
            pdk_variant=pdk_variant,
            pdk_root=pdk_root,
            has_def=has_def,
            def_path=def_path,
            verilog_path=verilog_path,
            hdl_toplevel=hdl_toplevel,
            clock_port=clock_port,
            clock_period_ns=clock_period_ns,
            input_delay_ns=input_delay_ns,
            output_delay_ns=output_delay_ns,
            spef_path=spef_path,
            wire_load_model=wire_load_model,
            wire_load_mode=wire_load_mode,
            output_dir=output_dir,
            script_tag=f"{basename}__{corner_name}",
        )
        if shared_pdk_info is None:
            shared_pdk_info = resolution["pdk_info"]
        deck_name = f"{cell_library}__{resolution['corner']}"
        corner_entries.append(
            {
                "corner": resolution["corner"],
                **corner_fields,
                "deck": _deck_block(deck_name, resolution["liberty_path"]),
            }
        )

    input_hash_after = sha256_file(input_path)
    if input_hash_after != input_hash_before:
        raise PostRouteStaError(
            "the geometry backing this multi-corner characterization "
            f"({input_path}) changed while characterizing corners "
            + ", ".join(corners)
            + " -- a klt sta corners response is only meaningful when every "
            "corner analyses the identical geometry, so this run cannot be "
            "reported"
        )

    provenance = build_provenance(
        deck_name=None,
        deck_path=None,
        pdk=shared_pdk_info,
        input_path=input_path,
        # Issue #2027: `input_path` is the DEF (a layout stream) when
        # the request supplied one, and the gate-level Verilog netlist
        # otherwise -- the two are not comparable artifacts, so the
        # role follows the same `has_def` branch `input_path` itself
        # does.
        input_role=INPUT_ROLE_LAYOUT if has_def else INPUT_ROLE_NETLIST,
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "engine": "openroad",
        "engine_version": engine_version,
        "hdl_toplevel": hdl_toplevel,
        "status": "ok",
        "def_path": def_path,
        "verilog_path": verilog_path,
        "geometry_source": geometry_source,
        "wire_load_model": wire_load_model,
        "wire_load_mode": wire_load_mode,
        "spef_path": spef_path,
        "provenance": provenance,
        # Additive field (issue #1871): one entry per requested
        # `request.pdk.corners` name, in the same order the request gave
        # them -- see this function's own docstring for the exact shape.
        "corners": corner_entries,
    }


def _spef_annotation_block(
    completed: _OpenRoadResult,
    spef_net_names: list[str] | None,
) -> dict[str, Any]:
    """Assemble the ``spef_annotation`` response block from one completed
    OpenROAD run's own output.

    Four independent pieces of evidence, three of which gate
    ``annotation_complete`` (issue #1624 -- the field previously rested on
    the *name-correlation* evidence alone, which is measured **before**
    ``read_spef`` and with a different name resolver than ``read_spef``
    itself uses, so it could report ``true`` for a SPEF ``read_spef`` then
    discarded wholesale, leaving every timing number bit-identical to the
    unannotated run):

    1. **Name correlation** (pre-``read_spef``, :func:`_spef_net_check_lines`)
       -- does the SPEF name the nets this design has. Gates.
    2. **Reader warnings** (:func:`_spef_reader_warnings`) -- OpenSTA's own
       ``STA-1650``/``STA-1648`` "net/instance not found" diagnostics, i.e.
       annotation it parsed and then threw away. Conclusive; gates.
    3. **Delay fingerprint** (:func:`_parse_delay_changed`) -- the same
       ``report_checks`` output before and after ``read_spef``. Identical
       output means the annotation changed nothing, whatever the cause.
       Gates (only when both fingerprints actually carry a path).
    4. **Post-``read_spef`` accounting** (:func:`_parse_parasitic_annotation`)
       -- ``report_parasitic_annotation``'s own count of drivers OpenSTA
       holds no parasitics for. ``unannotated_driver_count`` gates;
       ``partially_unannotated_driver_count`` is reported but deliberately
       does **not** gate: a complete, correctly-read SPEF routinely reports
       a non-zero partial count (load pins with no distinct RC node of their
       own), so gating on it would fail every honest run.
    """
    stdout = completed.stdout or ""
    stderr = completed.stderr or ""

    nets_check = _count_spef_nets_annotated(
        stdout,
        begin=_SPEF_NET_CHECK_BEGIN,
        end=_SPEF_NET_CHECK_END,
        pattern=_SPEF_NET_CHECK_RE,
    )
    nets_annotated, nets_total, design_nets_annotated, design_nets_total = (
        nets_check if nets_check is not None else (0, len(spef_net_names or []), 0, 0)
    )
    names_correlated = (
        design_nets_total > 0 and design_nets_annotated == design_nets_total
    )
    reader_warning_count, reader_warning_sample = _spef_reader_warnings(stdout, stderr)
    delay_changed = _parse_delay_changed(stdout)
    unannotated_drivers, partial_drivers = _parse_parasitic_annotation(stdout)

    reasons: list[str] = []
    design_nets_missing_sample: list[str] = []
    if not names_correlated:
        reasons.append(
            f"only {design_nets_annotated} of {design_nets_total} nets in "
            "the linked design are named by this SPEF"
        )
        design_nets_missing_sample = _parse_spef_missing_nets(stdout)
    if reader_warning_count:
        reasons.append(
            f"read_spef discarded annotation for {reader_warning_count} "
            "SPEF record(s) it could not resolve against the linked design "
            "(OpenSTA STA-1650/STA-1648 'not found' warnings)"
        )
    if delay_changed is False:
        reasons.append(
            "every timing path reported after read_spef is byte-identical to "
            "the same report taken before it, so this run's delays are the "
            "unannotated ones"
        )
    if unannotated_drivers:
        reasons.append(
            f"report_parasitic_annotation found {unannotated_drivers} driver(s) "
            "with no parasitics attached after read_spef"
        )

    annotation_complete = not reasons
    annotation_warning = None
    if reasons:
        annotation_warning = (
            "; ".join(reasons) + " -- the worst_slack_ns/etc. fields above are "
            "NOT a real-parasitics measurement to the extent annotation is "
            "missing."
        )

    return {
        "nets_annotated": nets_annotated,
        "nets_total": nets_total,
        "design_nets_annotated": design_nets_annotated,
        "design_nets_total": design_nets_total,
        "design_nets_missing_sample": design_nets_missing_sample,
        "reader_warning_count": reader_warning_count,
        "reader_warning_sample": reader_warning_sample,
        "unannotated_driver_count": unannotated_drivers,
        "partially_unannotated_driver_count": partial_drivers,
        "delay_changed": delay_changed,
        "annotation_complete": annotation_complete,
        "annotation_warning": annotation_warning,
    }


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


def _resolve_verilog(verilog_field: Any, request_dir: str) -> str:
    """Resolve ``request.verilog`` (issue #1825) -- the mutually-exclusive
    counterpart to :func:`_resolve_def` for a from-scratch, netlist-input
    request. Deliberately mirrors that function's own validation/resolution
    shape byte-for-byte (non-empty string, resolve relative to the request
    file's own directory, must exist and be readable) -- the two geometry
    sources differ in what they feed the OpenSTA session, not in how a
    caller-supplied path is resolved."""
    if not isinstance(verilog_field, str) or not verilog_field:
        raise PostRouteStaError("request.verilog must be a non-empty string")
    path = (
        verilog_field
        if os.path.isabs(verilog_field)
        else os.path.join(request_dir, verilog_field)
    )
    if not os.path.isfile(path):
        raise PostRouteStaError(f"verilog not found: {verilog_field}")
    try:
        with open(path, "rb"):
            pass
    except OSError as exc:
        raise PostRouteStaError(
            f"could not read verilog '{verilog_field}': {exc}"
        ) from exc
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


def _validate_constraints(
    constraints: Any,
) -> tuple[str, float, float | None, float | None]:
    """Unlike ``place_and_route.py``'s own ``_validate_constraints`` (where
    a clock is optional -- a ``target_stage: "floorplan"`` run has no
    meaningful clock yet), a standalone STA run has no meaning *without* a
    clock: there is no stage short of "timed" to fall back to. Both fields
    are therefore required here, not just required-together.

    Returns ``(clock_port, clock_period_ns, input_delay_ns,
    output_delay_ns)``. The last two (issue #1865) are the **I/O timing**
    constraints -- each independently optional, ``None`` when omitted (which
    reproduces this command's generated Tcl byte-for-byte). This command
    accepts a ``constraints`` block of its own (it can run standalone
    against an externally-produced DEF/Verilog, with no ``klt
    place-and-route`` request anywhere upstream), so the fields are
    validated and emitted here too, never inherited."""
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
    input_delay_ns = _validate_io_delay(constraints.get("input_delay_ns"), "input")
    output_delay_ns = _validate_io_delay(constraints.get("output_delay_ns"), "output")
    return clock_port, float(clock_period_ns), input_delay_ns, output_delay_ns


def _validate_io_delay(value: Any, which: str) -> float | None:
    """One of ``request.constraints.input_delay_ns``/``.output_delay_ns``
    (issue #1865): optional, and a non-negative number when given. ``0`` is
    a meaningful value ("valid exactly at the clock edge"), so this is
    ``>= 0``, not ``> 0``. Mirrors ``place_and_route.py``'s own
    ``_validate_io_delay`` exactly, modulo the exception class -- the same
    per-module-copy convention ``_clock_lines`` already follows."""
    if value is None:
        return None
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or value < 0
        or value != value  # NaN
        or value in (float("inf"), float("-inf"))
    ):
        raise PostRouteStaError(
            f"request.constraints.{which}_delay_ns must be a non-negative number"
        )
    return float(value)


def _validate_corners(value: Any) -> list[str] | None:
    """Optional ``request.pdk.corners`` (issue #1871) -- the additive,
    multi-corner alternative to the scalar ``request.pdk.corner``: a list of
    corner names to characterize the *same* loaded geometry against, in one
    request/response round trip, instead of a caller hand-rolling an
    external loop of N single-corner ``klt sta`` invocations (each of which
    re-reads the same LEF/DEF from scratch and can never itself assert that
    the N runs shared identical geometry).

    ``None`` (omitted, the default) preserves this command's original
    scalar-only behaviour exactly -- :func:`run_sta` falls back to
    ``request.pdk.corner`` (or the nominal corner when that is also
    omitted).

    Unlike ``place_and_route.py``'s ``request.pdk.sweep_corners`` (which
    *narrows* an already-enumerated shipped-corner set, so an explicit
    ``[]`` meaningfully means "sweep zero of them"), this field *is* the
    primary corner selection for a command whose only other option is a
    single scalar corner -- there is no broader set for ``[]`` to narrow.
    An empty list is therefore rejected outright as a request error, not
    treated as "characterize nothing".

    Raises :class:`PostRouteStaError` for anything other than a non-empty
    list of non-empty, mutually-distinct strings -- a duplicate corner name
    would silently run (and pay for) the identical OpenSTA session twice
    under two identical ``corners[]`` entries, which is never what a caller
    asking to characterize N *distinct* corners meant.
    """
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise PostRouteStaError(
            "request.pdk.corners must be a non-empty list of non-empty strings"
        )
    seen: set[str] = set()
    duplicates: list[str] = []
    for name in value:
        if name in seen:
            if name not in duplicates:
                duplicates.append(name)
        else:
            seen.add(name)
    if duplicates:
        raise PostRouteStaError(
            "request.pdk.corners must not repeat a corner name (duplicate(s): "
            + ", ".join(duplicates)
            + ")"
        )
    return value


def _validate_geometry_source(geometry_source: Any) -> str:
    """``request.geometry_source`` (issue #1826): declares whether ``def``
    is a fully-routed signoff geometry (``"routed"``, the default) or a
    pre-route placement-/CTS-stage estimate (``"placement_estimate"``) --
    see this module's own docstring "Pre-route DEFs" section. Purely a
    caller-supplied label this module cannot itself verify (a bare DEF file
    carries no stage provenance); echoed back in the response so a
    downstream consumer never mistakes a pre-route estimate for a routed
    signoff number without an explicit declaration in the request that put
    it there."""
    if geometry_source is None:
        return "routed"
    if geometry_source not in _GEOMETRY_SOURCES:
        raise PostRouteStaError(
            "request.geometry_source must be one of: " + ", ".join(_GEOMETRY_SOURCES)
        )
    return geometry_source


def _validate_wire_load_estimate(
    constraints: dict[str, Any], has_verilog: bool
) -> tuple[str | None, str | None]:
    """Validate ``constraints.wire_load_model``/``.wire_load_mode`` (issue
    #1825), returning ``(wire_load_model, wire_load_mode)`` -- both ``None``
    unless ``has_verilog`` (a ``def``-mode request's parasitics never come
    from a liberty wire-load model, so both fields are rejected outright
    rather than silently ignored there).

    ``constraints`` is already known to be a JSON object by the time this is
    called -- :func:`_validate_constraints` (always called first) raises
    otherwise.
    """
    wire_load_model = constraints.get("wire_load_model")
    wire_load_mode = constraints.get("wire_load_mode")
    if not has_verilog:
        if wire_load_model is not None or wire_load_mode is not None:
            raise PostRouteStaError(
                "constraints.wire_load_model/wire_load_mode are only valid "
                "when request.verilog is given -- a def-mode run's "
                "parasitics come from real (or caller-supplied spef) "
                "geometry, never a liberty wire-load model"
            )
        return None, None

    if wire_load_model is not None and (
        not isinstance(wire_load_model, str) or not wire_load_model
    ):
        raise PostRouteStaError(
            "constraints.wire_load_model must be a non-empty string when given"
        )
    if wire_load_mode is not None and wire_load_mode not in _WIRE_LOAD_MODES:
        raise PostRouteStaError(
            "constraints.wire_load_mode must be one of: " + ", ".join(_WIRE_LOAD_MODES)
        )
    if wire_load_mode is not None and wire_load_model is None:
        raise PostRouteStaError(
            "constraints.wire_load_mode requires constraints.wire_load_model"
        )
    return wire_load_model, wire_load_mode


def _resolve_liberty(
    cell_library: str,
    requested_corner: str | None,
    *,
    variant: str | None = None,
    root: str | None = None,
) -> tuple[str, str, dict[str, Any]]:
    """Resolve ``(liberty_path, corner, pdk_info)`` for ``cell_library``.
    Raises :class:`PostRouteStaError` (never
    :class:`~klayout_tools.pdk.PdkNotFoundError`).

    Thin wrapper around :func:`klayout_tools.pdk.resolve_liberty_for_cell_library`
    (issue #1652) -- that shared implementation (including the IHP
    single-underscore liberty-filename fallback, issue #1790) is what
    ``synthesize.py`` and ``place_and_route.py``'s own ``_resolve_liberty``
    wrappers call too, so all three modules stay in sync by construction;
    only the exception type raised on each failure differs per module.
    """
    return resolve_liberty_for_cell_library(
        cell_library,
        requested_corner,
        PostRouteStaError,
        variant=variant,
        root=root,
    )


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


def _io_delay_lines(
    clock_port: str,
    input_delay_ns: float | None,
    output_delay_ns: float | None,
) -> list[str]:
    """``set_input_delay``/``set_output_delay`` -- issue #1865's
    ``request.constraints.input_delay_ns``/``.output_delay_ns``.

    Byte-identical output to ``place_and_route.py``'s own
    ``_io_delay_lines`` (asserted by the test suite), and kept as a separate
    copy for the same reason ``_clock_lines`` above is: this command accepts
    and emits its ``constraints`` block independently, with no import
    dependency on ``place_and_route.py``. See that copy's docstring for why
    the non-clock input set is computed with a plain-Tcl ``lsearch`` rather
    than ``remove_from_collection``, and for what the sentinel-reporting
    failure looks like without these lines.

    With neither field set this emits nothing at all, reproducing this
    command's generated Tcl byte-for-byte as it was before issue #1865.
    """
    lines: list[str] = []
    if input_delay_ns is not None:
        lines += [
            f"set klt_clock_port [get_ports {clock_port}]",
            "set klt_non_clock_inputs "
            "[lsearch -inline -all -not -exact [all_inputs] $klt_clock_port]",
            f"set_input_delay {input_delay_ns} -clock {clock_port} "
            "$klt_non_clock_inputs",
        ]
    if output_delay_ns is not None:
        lines.append(
            f"set_output_delay {output_delay_ns} -clock {clock_port} [all_outputs]"
        )
    return lines


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


def _delay_fingerprint_lines(begin: str, end: str) -> list[str]:
    """A marker-delimited ``report_checks`` block, emitted once *before* and
    once *after* ``read_spef`` (issue #1624).

    The two blocks are compared byte for byte
    (:func:`_parse_delay_changed`): if annotating real parasitics left every
    reported path digit-identical, the run's delays are the *unannotated*
    ones no matter what the name-correlation check concluded. ``-digits 6``
    (rather than the default 2) makes the comparison sensitive enough that a
    real annotation always moves it; ``-path_delay min_max`` covers the
    hold-side path too, so a setup-only coincidence cannot mask the failure;
    and ``-unconstrained`` keeps the fingerprint non-empty (hence the check
    live) on a design whose paths OpenSTA considers unconstrained, where a
    bare ``report_checks`` reports only ``No paths found.``.

    Cost is one worst path per path group per direction -- negligible next to
    the ``read_lef``/``read_def``/``read_liberty`` load this session already
    does, and paid only when the request carries a ``spef``."""
    return [
        f'puts "{begin}"',
        "report_checks -path_delay min_max -digits 6 -unconstrained",
        f'puts "{end}"',
    ]


def _parasitic_annotation_lines() -> list[str]:
    """``report_parasitic_annotation``, marker-delimited -- OpenSTA's own
    post-``read_spef`` account of how many driver pins it holds no
    parasitics for (issue #1624).

    Unlike the pre-``read_spef`` name-correlation check, this measures the
    state ``read_spef`` actually left behind, using OpenSTA's own view of
    its own parasitics store. Wrapped in ``catch`` so an OpenSTA build
    without the command degrades the two derived fields to ``null`` rather
    than aborting the whole run (the command's output still reaches stdout;
    ``catch`` only swallows a Tcl-level error)."""
    return [
        f'puts "{_PARASITIC_ANNOTATION_BEGIN}"',
        "catch {report_parasitic_annotation} klt_parasitic_annotation_msg",
        f'puts "{_PARASITIC_ANNOTATION_END}"',
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
    input_delay_ns: float | None = None,
    output_delay_ns: float | None = None,
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

    When a ``spef`` is given, ``read_spef`` is wrapped in the annotation
    *evidence* scaffolding issue #1624 added: the pre-existing name-
    correlation check, a ``report_checks`` delay fingerprint on either side
    of the call, markers bracketing the call itself (so the SPEF reader's
    own warnings land in one delimited stdout region), and a closing
    ``report_parasitic_annotation``. See :func:`_spef_annotation_block` for
    what each piece proves.
    """
    lines = [
        f"read_lef {tech_lef}",
        f"read_lef {cell_lef}",
        f"read_def {def_path}",
        f"read_liberty {liberty_path}",
    ]
    lines += _clock_lines(clock_port, clock_period_ns)
    lines += _io_delay_lines(clock_port, input_delay_ns, output_delay_ns)
    if spef_path is not None:
        lines += _spef_net_check_lines(spef_net_names or [])
        lines += _delay_fingerprint_lines(_DELAY_PRE_BEGIN, _DELAY_PRE_END)
        lines.append(f'puts "{_SPEF_READ_BEGIN}"')
        lines.append(f"read_spef {spef_path}")
        lines.append(f'puts "{_SPEF_READ_END}"')
        lines += _delay_fingerprint_lines(_DELAY_POST_BEGIN, _DELAY_POST_END)
        lines += _parasitic_annotation_lines()
    lines += [
        "report_worst_slack_metric -setup",
        "report_worst_slack_metric -hold",
        "report_tns_metric -setup",
        "report_tns_metric -hold",
        "report_fmax_metric",
        "report_power_metric",
        "report_clock_skew_metric -setup",
    ]
    lines += _violation_count_lines()
    return lines


def _sta_netlist_script_lines(
    *,
    tech_lef: str,
    cell_lef: str,
    verilog_path: str,
    liberty_path: str,
    hdl_toplevel: str,
    clock_port: str,
    clock_period_ns: float,
    wire_load_model: str | None = None,
    wire_load_mode: str | None = None,
    input_delay_ns: float | None = None,
    output_delay_ns: float | None = None,
) -> list[str]:
    """Build the Tcl script for a from-scratch, netlist-input OpenSTA
    session (issue #1825): no DEF, no placement, no routing. Links a
    structural netlist directly (``read_verilog`` + ``link_design``) --
    the same ``read_liberty`` -> ``read_lef`` x2 -> ``read_verilog`` ->
    ``link_design`` -> ``create_clock`` order ``place_and_route.py``'s own
    ``"floorplan"``-stage load already runs (see that module's
    ``_stage_script_lines`` docstring for the ordering rationale), minus
    that stage's floorplan-init/macro-placement/PDN steps -- none of those
    have meaning without any placement geometry.

    ``wire_load_model``/``wire_load_mode`` (both optional, and only ever
    non-``None`` here -- :func:`_validate_wire_load_estimate` rejects them
    outright for a ``def``-mode request) drive OpenSTA's own
    ``set_wire_load_model``/``set_wire_load_mode``, this module's chosen
    parasitics-estimate mechanism for a geometry-free session (see this
    module's own docstring "From-scratch netlist input" section for why
    this -- not a flat per-net capacitance, and not either of
    ``place_and_route.py``'s ``estimate_parasitics`` modes -- was chosen).
    When both are omitted, no ``set_wire_load*`` command is issued at all:
    OpenSTA falls back to whatever ``default_wire_load``/
    ``default_wire_load_mode`` the resolved liberty itself declares (many
    open-PDK standard-cell libraries, including ``sky130_fd_sc_hd``, ship
    one), or to zero estimated wire parasitics if the liberty declares none
    -- either way, ``response.wire_load_model``/``wire_load_mode`` echo
    exactly what was (or was not) requested, never a guess at what OpenSTA
    silently defaulted to on its own.

    Reports the same timing/power/violation metrics
    :func:`_sta_script_lines` does, via the identical ``report_*``/
    ``puts``-marker sequence -- a caller diffing a ``def``-mode and
    ``verilog``-mode response for the same design sees the same field
    shapes throughout, differing only in ``geometry_source``/the estimate
    provenance fields.
    """
    lines = [
        f"read_liberty {liberty_path}",
        f"read_lef {tech_lef}",
        f"read_lef {cell_lef}",
        f"read_verilog {verilog_path}",
        f"link_design {hdl_toplevel}",
    ]
    lines += _clock_lines(clock_port, clock_period_ns)
    lines += _io_delay_lines(clock_port, input_delay_ns, output_delay_ns)
    if wire_load_mode is not None:
        lines.append(f"set_wire_load_mode {wire_load_mode}")
    if wire_load_model is not None:
        lines.append(f"set_wire_load_model -name {{{wire_load_model}}}")
    lines += [
        "report_worst_slack_metric -setup",
        "report_worst_slack_metric -hold",
        "report_tns_metric -setup",
        "report_tns_metric -hold",
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


def _engine_error_message(completed: _OpenRoadResult) -> str:
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


def _extract_block(text: str, begin: str, end: str) -> str | None:
    """The text between the ``begin``/``end`` markers, or ``None`` when
    either marker is absent (an OpenROAD build that never reached that part
    of the script, or a caller parsing a stream the markers aren't in)."""
    try:
        start_idx = text.index(begin) + len(begin)
        stop_idx = text.index(end, start_idx)
    except ValueError:
        return None
    return text[start_idx:stop_idx]


def _spef_reader_warnings(stdout: str, stderr: str) -> tuple[int, list[str]]:
    """``(count, capped_sample)`` of the diagnostics OpenSTA's *SPEF reader*
    emitted while parsing the caller-supplied SPEF -- the conclusive signal
    that ``read_spef`` parsed a record and then discarded it (issue #1624).

    Two matching strategies, because OpenROAD builds disagree about which
    stream the logger writes to:

    * **stdout** is bracketed by :data:`_SPEF_READ_BEGIN`/
      :data:`_SPEF_READ_END` around the ``read_spef`` call itself, and the
      SPEF reader is the only thing running inside that region -- so *any*
      ``[WARNING STA-...]`` there is a reader warning, including codes this
      module has never seen. (When the markers are absent -- an older
      script, a truncated run -- it falls back to the narrow code match.)
    * **stderr** cannot be interleaved with the Tcl ``puts`` markers, so it
      is matched narrowly against :data:`_SPEF_READER_WARNING_RE`
      (``STA-1650``/``STA-1648``) -- a liberty or clock warning on stderr
      must not be mistaken for a SPEF-annotation failure.
    """
    matched: list[str] = []
    region = _extract_block(stdout, _SPEF_READ_BEGIN, _SPEF_READ_END)
    if region is not None:
        matched += [
            line.strip() for line in region.splitlines() if _STA_WARNING_RE.search(line)
        ]
    else:
        matched += [
            line.strip()
            for line in stdout.splitlines()
            if _SPEF_READER_WARNING_RE.search(line)
        ]
    matched += [
        line.strip()
        for line in stderr.splitlines()
        if _SPEF_READER_WARNING_RE.search(line)
    ]
    return len(matched), matched[:_SPEF_READER_WARNING_SAMPLE_LIMIT]


def _parse_delay_changed(stdout: str) -> bool | None:
    """Did annotating the SPEF change any reported timing path?

    Compares the two :func:`_delay_fingerprint_lines` blocks (identical
    ``report_checks`` invocations run before and after ``read_spef``):

    * ``True`` -- the post-annotation report differs, i.e. the parasitics
      reached the delay calculator.
    * ``False`` -- byte-identical reports: whatever the name-correlation
      check concluded, these delays are the *unannotated* ones. This is the
      exact symptom issue #1624 reports.
    * ``None`` -- unknown, never ``False`` by default: a marker is missing
      (an OpenROAD build that failed before the second report), or the
      design has no timing path for ``report_checks`` to report on, in which
      case two empty/``No paths found`` blocks say nothing about annotation.
    """
    pre = _extract_block(stdout, _DELAY_PRE_BEGIN, _DELAY_PRE_END)
    post = _extract_block(stdout, _DELAY_POST_BEGIN, _DELAY_POST_END)
    if pre is None or post is None:
        return None
    pre_norm = _normalize_delay_block(pre)
    post_norm = _normalize_delay_block(post)
    if not pre_norm or not post_norm:
        return None
    if any(_NO_PATHS_FOUND in line.lower() for line in (pre_norm + post_norm)):
        return None
    return pre_norm != post_norm


def _normalize_delay_block(block: str) -> list[str]:
    """A ``report_checks`` block reduced to its non-blank, right-stripped
    lines -- so trailing-whitespace or blank-line churn between two
    otherwise-identical reports cannot be mistaken for a delay change."""
    return [line.rstrip() for line in block.splitlines() if line.strip()]


def _parse_parasitic_annotation(stdout: str) -> tuple[int | None, int | None]:
    """``(unannotated_drivers, partially_unannotated_drivers)`` parsed from
    ``report_parasitic_annotation``'s own two summary lines
    (``Found <n> unannotated drivers.`` / ``Found <n> partially unannotated
    drivers.``), or ``(None, None)`` when the block is absent or the command
    produced nothing this module recognises (e.g. an OpenSTA build without
    ``report_parasitic_annotation``, where the ``catch`` kept the run
    alive)."""
    block = _extract_block(
        stdout, _PARASITIC_ANNOTATION_BEGIN, _PARASITIC_ANNOTATION_END
    )
    if block is None:
        return (None, None)
    unannotated_match = _UNANNOTATED_DRIVERS_RE.search(block)
    partial_match = _PARTIAL_DRIVERS_RE.search(block)
    return (
        int(unannotated_match.group(1)) if unannotated_match else None,
        int(partial_match.group(1)) if partial_match else None,
    )

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
stage load -- ``read_liberty``, ``create_clock``, optionally
``set_propagated_clock`` + a latency evidence dump (issue #2739),
optionally ``read_spef``) and reports the same timing/power fields
``place-and-route``'s response already carries. It never places, routes,
or runs CTS -- there is no
``target_stage``, no netlist, no ``link_design``; the DEF handed in is the
one and only geometry analysed.

Deliberately duplicates one small helper already defined in
``place_and_route.py`` (``_clock_lines``) rather than importing it -- this
repo's own stated convention (see ``place_and_route.py``'s own helper
docstrings) is that each verb module stays self-contained. Every existing
cross-module import between verb modules in this repo is of a *public* name
(``run_place_and_route``, ``PlaceAndRouteError``, ``run_extract``, ...),
never a private one. ``run_openroad``, ``count_violations``,
``openroad_version``, ``write_script``, ``format_engine_error``,
``read_metrics``, and (issue #1703) the SPEF net-name-correlation Tcl
helpers ``_tcl_net_list``/``_count_spef_nets_annotated`` do not break that
convention: they were byte-identical (modulo exception class / label /
docstring length, or -- for ``_count_spef_nets_annotated`` -- module-local
marker constants) between this module and ``place_and_route.py`` with no
caller-visible behavioral difference worth preserving per-copy, so they
were consolidated into the shared ``_openroad_engine``/``_openroad_io``/
``_paths.py`` utility modules both import from (issues #1637/#1703/#8051) --
following the same precedent as ``_paths.py`` (issue #642). Those are
shared utility modules, not verb modules, so calling their helpers -- public
from ``_openroad_engine``/``_openroad_io`` since issue #8051 retired the
last grandfathered private imports, still underscore-prefixed from
``_paths.py`` -- does not violate the convention above. ``_resolve_liberty``
(this module's own thin wrapper, below) follows the same precedent one level
further: its body is a one-line call into
:func:`klayout_tools.pdk_cells.resolve_liberty_for_cell_library`, a *public* name
in the ``pdk_cells.py`` shared utility module (issue #1652) -- PDK-resolution
logic is the one part of the "self-contained" convention that must not
drift silently between verb modules, since a resolution bug in one copy
(issue #1790's missed ``post_route_sta.py`` fallback) is a live correctness
bug, not just duplicated code.

Propagated-clock timing is available through ``propagated_clock`` (issue #2739).
``fmax_mhz`` comes from OpenROAD's ``report_fmax_metric``. Its clock estimate
uses same-clock, same-edge, single-cycle paths and can overstate achievable
frequency when an opposite-edge path limits the period.

**Propagated-clock timing (issue #2739).** By default every analyzed corner
is timed with an **ideal** clock: ``create_clock`` alone, no
``set_propagated_clock`` -- the clock source's arrival is zero at every
clocked pin, so a real clock tree's insertion delay and skew (loaded from a
post-CTS DEF, or annotated from a SPEF) never reach the timing numbers.
``constraints.propagated_clock: true`` opts a ``def``-mode request into
propagated-clock timing: after the design and the created clock are loaded,
the script marks exactly that clock propagated
(``set_propagated_clock [get_clocks <clock_port>]`` -- the clock object
``_clock_lines`` itself created) and then, after any SPEF annotation and
before any timing/power metric is collected, dumps
``report_clock_latency`` plus a dedicated sink-count query
(:func:`_clock_sink_count_lines`, issue #7417) so the response carries
machine-readable evidence of what propagation actually reached: the number
of clock sink pins the clock network reaches
(``clock_propagation.clock_sinks``, from OpenSTA's own
``all_registers -clock_pins`` enumeration -- live-verified on the pinned
build to follow the clock through the CTS buffers), and the worst network
latency among the pins the latency report names. On a pinned-build session
a routed tree moves a boundary hold check from met to violated exactly as
the issue describes -- which is the point: ideal timing can call an input
hold check MET that the propagated analysis calls VIOLATED.

The option is **opt-in, and ``"ideal"`` remains the default and the
backward-compatible behavior byte-for-byte**, because a bare DEF carries no
metadata declaring it post-CTS: defaulting to propagated would silently
change every existing timing result, and propagating a pre-CTS DEF is
honest but near-zero-delay (the clock reaches the flops through plain
wires). The response always says which mode ran -- ``clock_mode``:
``"ideal"`` | ``"propagated"`` -- so a consumer never has to guess from
``clock_skew_ns`` (a measured metric that is nonzero under ideal timing
too, and therefore *not* evidence of propagation). The option is rejected
for ``verilog`` mode outright: a linked netlist has no placed clock tree to
propagate through. There is no engine query for the propagated flag itself
on the pinned build (verified: no ``get_property`` clock attribute and no
``is_propagated_clock`` Tcl command), so "propagation could not be
established" is reported as explicit machine-readable evidence rather than
an error: a DEF whose clock reaches no clock pin yields
``clock_propagation.clock_sinks: 0`` / ``max_network_latency_ns: null``
next to whatever timing the engine produced (which, for such a design, is
indistinguishable from ideal -- exactly the not-silently-claimed signal a
consumer needs).

**Constraint completeness (issue #2780).** ``timing_status`` only says
whether the slack fields are measurements; a design whose WNS is finite can
still leave endpoints unconstrained and registers unclocked. Every session
therefore ends with :func:`_constraint_completeness_lines` -- OpenSTA's
structured ``sta::check_timing_cmd`` query, one check per call -- and every
response/``corners[]`` entry carries the parsed ``constraint_completeness``
block (:func:`_parse_constraint_completeness`): counts, a bounded identity
sample with explicit truncation, and ``unavailable``/``unknown`` (never a
fabricated zero) when the query could not run.

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

Hard macros (issue #2635)
-------------------------

``request.macros`` is the shared hard-macro declaration -- one shape,
documented once in :mod:`klayout_tools.macros`, accepted identically by
``klt synthesize`` and (plus its own placement fields) by ``klt
place-and-route``. Each entry's ``lef`` is ``read_lef``'d alongside the
tech/cell LEF before the ``def``/``verilog`` load, which is what makes a
macro-bearing design analysable here at all: without it OpenROAD refuses
the load outright with ``[ERROR ORD-2013] instance <inst> LEF master
<cell> not found`` (live-reproduced, and live-confirmed fixed by the one
extra ``read_lef``, against OpenROAD 26Q3 on sky130A). Each entry's
optional ``lib`` -- a **per-corner** map -- is ``read_liberty``'d alongside
the standard-cell library for whichever corner is being analysed, so the
macro's real arcs participate in this verb's slack/``fmax``/power numbers
rather than being blackboxed; a ``lib`` map with no entry for the corner
being run is a named error, never a silent fallback to another corner's
timing, which matters most in a ``request.pdk.corners`` sweep. See
:func:`~klayout_tools._macros_blackbox.macro_liberty_paths`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess  # noqa: F401 -- tests patch post_route_sta.subprocess.run
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from . import _liberty_warnings, _sta_spef_inputs, _sta_wire_load, env_provenance
from . import _macros_blackbox as macro_blackbox
from . import _sta_clock_transition as _transition
from . import _sta_generated_clocks as _generated
from . import _sta_timing_derate as _derate
from . import macros as macro_spec
from ._jsonnum import is_finite_number, is_number
from ._openroad_engine import (
    OpenRoadResult,
    container_runtime_hint,
    count_violations,
    openroad_version,
    run_openroad,
    timing_status,
    timing_violation_report_lines,
    write_sdf_line,
)
from ._openroad_io import format_engine_error, read_metrics, write_script
from ._openroad_mount_hint import mount_namespace_hint
from ._paths import (
    _count_spef_nets_annotated,
    _load_request_json,
    _tcl_net_list,
    validate_request_shape,
)
from ._power_activity import power_activity_lines as _power_activity_lines
from ._power_activity import power_basis_block as _power_basis_block
from ._power_activity import validate_power_activity
from ._provenance import (
    INPUT_ROLE_LAYOUT,
    INPUT_ROLE_NETLIST,
    _deck_block,
    build_provenance,
    sha256_file,
)
from ._request_fields import check_known_fields
from ._sta_clock_transition import (
    validate_propagated_clock as _validate_propagated_clock,
)
from ._sta_clock_uncertainty import (
    clock_uncertainty_block as _clock_uncertainty_block,
)
from ._sta_clock_uncertainty import (
    clock_uncertainty_lines as _clock_uncertainty_lines,
)
from ._sta_clock_uncertainty import (
    validate_clock_uncertainty as _validate_clock_uncertainty,
)
from ._sta_io_delay_clock import IoDelayMap
from ._sta_io_delay_clock import (
    check_port_delay_key as _check_port_delay_key,
)
from ._sta_io_delay_clock import (
    entry_clock as _entry_clock,
)
from ._sta_io_delay_clock import (
    validate_port_delay_maps as _validate_delay_maps,
)
from ._sta_multicycle import (
    _false_path_lines,
    _validate_false_path_from,
)
from ._sta_multicycle import (
    multicycle_lines as _multicycle_lines,
)
from ._sta_multicycle import (
    validate_multicycle_paths as _validate_multicycle_paths,
)
from ._sta_period_maps import (
    check_period_maps,
    clock_period_for_corner,
    clock_targets,
    validate_period,
)
from ._sta_request_diag import (
    KNOWN_REQUEST_FIELDS,
    SPEF_NAME_MISMATCH_HINT,
    SPEF_NO_PIN_ATTACH_HINT,
    SPEF_SKIP_DRIVER_ONLY,
    validate_name_list,
)
from ._sta_request_diag import SPEF_MISSING_NETS_BEGIN as _SPEF_MISSING_NETS_BEGIN
from ._sta_request_diag import SPEF_MISSING_NETS_END as _SPEF_MISSING_NETS_END
from ._sta_request_diag import extract_block as _extract_block
from ._sta_request_diag import parse_spef_missing_nets as _parse_spef_missing_nets
from ._sta_sdc_check import attach_sdc_drift, resolve_sdc_check
from ._tcl import tcl_literal as _tcl_literal
from .pdk import lef_files
from .pdk_cells import resolve_liberty_for_cell_library
from .post_route_sta_clock_propagation import (  # noqa: F401 -- re-exported names
    _CLOCK_LATENCY_BEGIN,
    _CLOCK_LATENCY_END,
    _CLOCK_SECTION_RE,
    _CLOCK_SINK_COUNT_BEGIN,
    _CLOCK_SINK_COUNT_END,
    _CLOCK_SINK_COUNT_RE,
    _CLOCK_SINK_SAMPLE_LIMIT,
    _NETWORK_LATENCY_RE,
    _clock_latency_lines,
    _clock_latency_rows,
    _clock_sink_count_lines,
    _max_network_latency,
    _parse_clock_latency,
    _parse_clock_sink_count,
)
from .post_route_sta_electrical import (
    _ELECTRICAL_NET_PREFIX,
    _ELECTRICAL_OK,
    _ELECTRICAL_UNSUPPORTED,
    _MAX_CAP_BEGIN,
    _MAX_CAP_END,
    _MAX_SLEW_BEGIN,
    _MAX_SLEW_END,
    _MIN_PERIOD_BEGIN,
    _MIN_PERIOD_END,
    _MIN_PULSE_BEGIN,
    _MIN_PULSE_END,
    _parse_electrical_check,
    _parse_electrical_slack_check,
)
from .post_route_sta_errors import PostRouteStaError as PostRouteStaError
from .post_route_sta_instance_paths import (
    _instance_check_lines,
    _instance_path_lines,
    _instance_paths_corner_field,
    _validate_report_instances,
)
from .post_route_sta_multicycle import (
    check_multicycle_errors as _check_multicycle_errors,
)
from .post_route_sta_multicycle import (
    multicycle_session_fields as _multicycle_session_fields,
)
from .post_route_sta_path_groups import (  # noqa: F401 -- re-exported names
    _PORT_PATHS_BEGIN,
    _PORT_PATHS_END,
    _WORST_PATHS_BEGIN,
    _WORST_PATHS_END,
    _empty_path_record,
    _port_paths,
    _worst_path_record,
    _worst_paths,
    _worst_paths_by_group,
)
from .post_route_sta_stages import (
    _attach_worst_path_stages,
    _worst_path_lines,
    _worst_path_stage_lines,
)
from .post_route_sta_violating_endpoints import (
    _validate_max_violating_endpoints,
    _violating_endpoint_lines,
    _violating_endpoints_corner_field,
)

#: Bumped only on a non-additive (breaking) change to this command's own
#: JSON shape -- versioned independently of every other verb's own
#: ``SCHEMA_VERSION`` (docs/json-contract.md: "versioned per command, not
#: globally").
#:
#: ``2`` (issue #2879): ``def_path``/``verilog_path``/``spef_path`` are the
#: ``{path, scope}`` envelope (``env_provenance.repo_relative_path``, the
#: shape ``klt sim``/``klt pex`` adopted in issue #1261) instead of plain
#: absolute-path strings, and the SPEF reader-warning sample no longer
#: embeds the producing checkout's absolute input paths. A schema-``1``
#: response carried the absolute host path, which made committed STA
#: evidence verifiable only on the host that produced it.
SCHEMA_VERSION = 2

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
#: Liberty cell/pin identifier for ``constraints.driving_cell`` (issue #3742).
_LIBERTY_IDENT_RE = re.compile(r"[A-Za-z0-9_$.]+")
#: A ``*NAME_MAP`` entry (``*12 data\[0\]``) and a ``*<n>`` index reference.
_SPEF_NAME_MAP_ENTRY_RE = re.compile(r"^(\*\d+)\s+(\S+)", re.MULTILINE)
_SPEF_HEADER_DIVIDER_RE = re.compile(r"^\*DIVIDER\s+(\S)", re.MULTILINE)

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
#: Brackets the leaf output pins that sit on no net at all (issue #3662) --
#: the "floating, untimed" drivers (CTS dummy-load cells' outputs) that
#: ``report_parasitic_annotation`` counts as unannotated but that can never
#: carry parasitics. See :func:`_parasitic_annotation_lines` /
#: :func:`_parse_floating_drivers`.
_FLOATING_DRIVERS_BEGIN = "===KLT_STA_FLOATING_DRIVERS_BEGIN==="
_FLOATING_DRIVERS_END = "===KLT_STA_FLOATING_DRIVERS_END==="
_FLOATING_DRIVER_PREFIX = "KLT_FLOATING_DRIVER"
#: Brackets the leaf output pins whose net carries no other instance pin
#: (issue #3668) -- evidence only, never a gate: such a net may still reach a
#: port, so a driver on it is not "floating". See :func:`_parse_driver_nets`.
_DRIVER_NETS_BEGIN = "===KLT_STA_DRIVER_NETS_BEGIN==="
_DRIVER_NETS_END = "===KLT_STA_DRIVER_NETS_END==="
_DRIVER_NET_PREFIX = "KLT_DRIVER_NET"
#: Upper bound on how many driver pins each ``*_driver_sample`` field echoes
#: -- a diagnostic sample, same style as ``design_nets_missing_sample``.
_DRIVER_SAMPLE_LIMIT = 20
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
_SPEF_MISSING_NET_RE = re.compile(
    r"\[WARNING STA-1650\] (?:.*, )?net (.+) not found\.$"
)
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

#: Issue #2740: brackets the per-port I/O-delay port-existence/direction
#: check (:func:`_port_check_lines`). One ``<side>\t<port>\t<problem>`` row
#: per failing ``constraints.input_delays``/``output_delays`` key; an empty
#: block means every key named exactly one port of an allowed direction.
_PORT_CHECK_BEGIN = "===KLT_STA_PORT_CHECK_BEGIN==="
_PORT_CHECK_END = "===KLT_STA_PORT_CHECK_END==="
#: Exit status the generated script uses to stop a session whose per-port
#: map named a missing/ambiguous/wrong-direction port, *before* any timing is
#: reported -- so a typo can never silently constrain no ports.
_PORT_CHECK_EXIT_CODE = 3

#: Issue #2780: brackets the per-session constraint-completeness query
#: (:func:`_constraint_completeness_lines`). One ``CHECK\t<check>\t<total>``
#: row per check, followed by at most :data:`_COMPLETENESS_SAMPLE_LIMIT`
#: ``ITEM\t<check>\t<pin>`` rows naming that check's offending pins, or a
#: single ``ERROR\t<check>\t<message>`` row when the engine query itself
#: failed. Parsed by :func:`_parse_constraint_completeness`.
_COMPLETENESS_BEGIN = "===KLT_STA_CONSTRAINT_COMPLETENESS_BEGIN==="
_COMPLETENESS_END = "===KLT_STA_CONSTRAINT_COMPLETENESS_END==="
#: Upper bound on how many offending pin names each completeness check
#: echoes into its ``sample`` -- the ``count`` is always the engine's full
#: total, and ``truncated`` says when the sample is shorter than it.
_COMPLETENESS_SAMPLE_LIMIT = 20
#: ``check name -> sta::check_timing_cmd`` positional flags, in the engine's
#: own argument order (``no_input_delay no_output_delay reg_multiple_clks
#: reg_no_clks unconstrained_endpoints loops generated_clks``, verified on
#: OpenROAD ``26Q3-1510-g6cb3f2b704``). Exactly one flag is set per call so
#: every returned record belongs to that one check -- the engine's
#: human-readable message (the record's first element) is never parsed;
#: only the pin names after it are used.
_COMPLETENESS_CHECKS = (
    ("unconstrained_endpoints", "0 0 0 0 1 0 0"),
    ("unclocked_registers", "0 0 0 1 0 0 0"),
)

#: The direction strings OpenSTA's ``get_property <port> direction`` may
#: return that a per-port input/output delay may be applied to.
_INPUT_PORT_DIRECTIONS = ("input", "inout", "bidirect")
_OUTPUT_PORT_DIRECTIONS = ("output", "inout", "bidirect")
#: Issue #3042: ``constraints.report_ports`` accepts a port of any direction.
_ANY_PORT_DIRECTIONS = ("input", "output", "inout", "bidirect")


def load_request(request_path: str) -> dict[str, Any]:
    """Read the request as written and validate its shape for later hashing.

    The loader requires ``pdk`` and ``constraints``, accepts an optional schema,
    and rejects unknown fields, unreadable files and malformed JSON. After this
    check, :func:`run_sta` requires exactly one of ``def`` and ``verilog`` (#1825).
    """
    request = _load_request_json(request_path, PostRouteStaError)
    request = validate_request_shape(
        request,
        "request file",
        error_cls=PostRouteStaError,
        required_fields=("pdk", "constraints"),
    )
    check_known_fields(
        request, KNOWN_REQUEST_FIELDS, verb="sta", error_cls=PostRouteStaError
    )
    return request


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

    cell_library, requested_corner, requested_corners = _resolve_pdk_selection(request)

    # Issue #2635: the shared hard-macro declaration. Shape-validated here,
    # before any PDK resolution or engine invocation; the corner-dependent
    # half (which of a macro's per-corner liberties applies) is resolved
    # per corner inside `_run_corner_session`.
    macros = _validate_macros(request.get("macros"), request_dir, has_def=has_def)

    (
        clock_port,
        clock_period_ns,
        input_delay_ns,
        output_delay_ns,
    ) = _validate_constraints(request["constraints"])
    clock_extras = _validate_clock_extras(request["constraints"])
    check_period_maps(clock_period_ns, clock_extras, clock_port, requested_corners)
    # Issue #7399: an entry-level `clock` may name any declared clock -- the
    # primary plus every secondary `constraints.clocks` port.
    declared_clocks = {clock_port}
    declared_clocks.update(
        clock["port"] for clock in (clock_extras or {}).get("clocks", [])
    )
    input_delays, output_delays = _validate_port_delay_maps(
        request["constraints"], clock_port, declared_clocks
    )
    input_transition_ns, input_transitions = _validate_input_transitions(
        request["constraints"], clock_port
    )
    # Issue #7023: the eleven validated port/path constraint values below
    # travel together unchanged into every corner session -- one typed
    # record carries them through dispatch instead of eleven parallel
    # keyword arguments at each hop.
    constraints = _StaConstraints(
        input_delay_ns=input_delay_ns,
        output_delay_ns=output_delay_ns,
        input_delays=input_delays,
        output_delays=output_delays,
        input_transition_ns=input_transition_ns,
        input_transitions=input_transitions,
        boundary=_validate_boundary(
            request["constraints"], clock_port, input_transition_ns, input_transitions
        ),
        report_ports=_validate_report_ports(request["constraints"], clock_port),
        report_path_stages=_validate_report_path_stages(request["constraints"]),
        max_violating_endpoints=_validate_max_violating_endpoints(
            request["constraints"]
        ),
        false_path_from=_validate_false_path_from(request["constraints"], clock_port),
        report_instances=_validate_report_instances(request["constraints"]),
    )
    wire_load_model, wire_load_mode = _validate_wire_load_estimate(
        request["constraints"], has_verilog
    )
    clock_transition = _transition.validate_clock_transition(request["constraints"])
    propagated_clock = _validate_propagated_clock(request["constraints"], has_def)
    power_activity = _validate_power_activity(request["constraints"])
    clock_uncertainty = _validate_clock_uncertainty(request["constraints"])
    multicycle = _validate_multicycle_paths(request["constraints"])
    timing_derate = _derate.validate_timing_derate(request["constraints"])
    sdc_path = resolve_sdc_check(request.get("sdc_check"), request_dir)

    spef_map: dict[str, str] | None = None
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
        spef_field = request.get("spef")
        if isinstance(spef_field, dict):
            # Issue #3228: the map form (`{"min"|"nom"|"max": path}`) is the
            # interconnect axis of a liberty x interconnect matrix; there is
            # no single hoistable `spef_path` for it.
            spef_map = _sta_spef_inputs.resolve_spef_map(spef_field, request_dir)
            spef_path = None
        else:
            spef_path = _sta_spef_inputs.resolve_spef(spef_field, request_dir)
        geometry_source = _validate_geometry_source(request.get("geometry_source"))
    write_sdf = _validate_sdf(
        request.get("sdf"),
        has_verilog=has_verilog,
        has_spef=spef_path is not None,
        has_spef_map=spef_map is not None,
    )

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

    # Build the report's path envelopes once. Hashing and OpenROAD use the
    # original absolute paths, including when the report redacts them.
    report_paths = _ReportPaths.build(
        def_path=def_path,
        verilog_path=verilog_path,
        spef_path=spef_path,
        spef_map=spef_map,
        repo_root=env_provenance.find_repo_root(request_dir),
    )
    # Issue #7712: everything below that does not vary per corner travels
    # together as one record; only the per-corner/dispatch-varying values
    # (corner selection, clock period/extras, SPEF choice) stay explicit.
    run = _StaRunConfig(
        report_paths=report_paths,
        cell_library=cell_library,
        pdk_variant=pdk_variant,
        pdk_root=pdk_root,
        has_def=has_def,
        def_path=def_path,
        verilog_path=verilog_path,
        hdl_toplevel=hdl_toplevel,
        geometry_source=geometry_source,
        clock_port=clock_port,
        constraints=constraints,
        wire_load_model=wire_load_model,
        wire_load_mode=wire_load_mode,
        macros=macros,
        propagated_clock=propagated_clock,
        power_activity=power_activity,
        clock_uncertainty=clock_uncertainty,
        clock_transition=clock_transition,
        multicycle=multicycle,
        timing_derate=timing_derate,
        output_dir=output_dir,
        input_path=input_path,
        basename=basename,
        write_sdf=write_sdf,
    )
    with (
        report_paths.scrubbing_errors(),
        _sta_spef_inputs.stable_input_hashes(spef_path, spef_map) as spef_hashes,
    ):
        response = _dispatch_sta(
            requested_corner=requested_corner,
            requested_corners=requested_corners,
            run=run,
            clock_period_ns=clock_period_ns,
            spef_path=spef_path,
            spef_map=spef_map,
            clock_extras=clock_extras,
        )
        _sta_spef_inputs.attach_hashes(response, spef_hashes)
        if constraints.boundary and "clock_driving_cell" in constraints.boundary:
            response["clock_driving_cell"] = constraints.boundary["clock_driving_cell"]
        if multicycle:
            canonical = json.dumps(request, sort_keys=True, separators=(",", ":"))
            response["provenance"]["request_sha256"] = hashlib.sha256(
                canonical.encode()
            ).hexdigest()
        attach_sdc_drift(  # issue #5236: opt-in, informational only
            response,
            sdc_path,
            request_dir=request_dir,
            clocks=clock_targets(clock_port, clock_period_ns, clock_extras)["clocks"],
            clock_port=clock_port,
            input_delay_ns=constraints.input_delay_ns,
            output_delay_ns=constraints.output_delay_ns,
            input_delays=constraints.input_delays,
            output_delays=constraints.output_delays,
            clock_uncertainty=clock_uncertainty,
            multicycle=multicycle,
        )
        return response


class _ReportPaths:
    """Issue #2879: the persisted identity of ``klt sta``'s three input
    files, kept separate from the absolute engine paths they derive from.

    ``def_path``/``verilog_path``/``spef_path`` are each the ``{path,
    scope}`` envelope :func:`env_provenance.repo_relative_path` defines
    (``scope: "repo"`` with a repo-relative POSIX path, or ``{"path": null,
    "scope": "external"}`` for an input outside any repository -- never an
    absolute host path), or plain ``None`` when this request does not use
    that input at all (``def_path`` in ``verilog`` mode, ``verilog_path`` in
    ``def`` mode, ``spef_path`` with no ``request.spef``). ``None`` rather
    than ``scope: "absent"`` keeps the schema-1 "not applicable" value, and
    is what lets ``klt signoff`` fall through from ``def_path`` to
    ``verilog_path`` when choosing which artifact to re-hash.

    :meth:`scrub` is the bounded warning/error normaliser: it replaces only
    the literal resolved forms of these three files (``abspath`` and
    ``realpath``, longest first so one never half-rewrites the other) with
    their report identity -- the repo-relative path, or ``<outside repo>``.
    Any other absolute-looking text an engine diagnostic carries is left
    exactly as the engine wrote it.
    """

    def __init__(
        self,
        *,
        def_path: dict[str, Any] | None,
        verilog_path: dict[str, Any] | None,
        spef_path: dict[str, Any] | None,
        spef_paths: dict[str, dict[str, Any]] | None,
        replacements: list[tuple[str, str]],
        repo_root: str | None = None,
    ) -> None:
        self.def_path = def_path
        self.verilog_path = verilog_path
        self.spef_path = spef_path
        # Issue #3228: per-interconnect-corner envelopes of a map-form
        # `request.spef` (`None` otherwise); `spef_path` is `None` then.
        self.spef_paths = spef_paths
        self._replacements = replacements
        self._repo_root = repo_root

    def output_envelope(self, engine_path: str) -> dict[str, Any]:
        """The ``{path, scope}`` identity of a file this command *wrote*
        (issue #2886's per-corner SDF), resolved against the same repo root
        as the three inputs above -- never an absolute host path."""
        return env_provenance.repo_relative_path(engine_path, repo_root=self._repo_root)

    @classmethod
    def build(
        cls,
        *,
        def_path: str | None,
        verilog_path: str | None,
        spef_path: str | None,
        repo_root: str | None,
        spef_map: dict[str, str] | None = None,
    ) -> _ReportPaths:
        envelopes: dict[str, dict[str, Any] | None] = {}
        labels: dict[str, str] = {}
        spef_fields = [
            (f"spef_path.{key}", path) for key, path in (spef_map or {}).items()
        ]
        for field, engine_path in (
            ("def_path", def_path),
            ("verilog_path", verilog_path),
            ("spef_path", spef_path),
            *spef_fields,
        ):
            if engine_path is None:
                envelopes[field] = None
                continue
            envelope = env_provenance.repo_relative_path(
                engine_path, repo_root=repo_root
            )
            envelopes[field] = envelope
            label = (
                envelope["path"]
                if envelope.get("scope") == "repo"
                else env_provenance.render_path_field(envelope)
            )
            for variant in (engine_path, os.path.realpath(engine_path)):
                labels.setdefault(variant, label)
        replacements = sorted(
            labels.items(), key=lambda item: len(item[0]), reverse=True
        )
        spef_envelopes: dict[str, dict[str, Any]] = {}
        for key in spef_map or {}:
            # Map-form entries always carry a path, so never None.
            spef_envelope = envelopes[f"spef_path.{key}"]
            assert spef_envelope is not None
            spef_envelopes[key] = spef_envelope
        return cls(
            def_path=envelopes["def_path"],
            verilog_path=envelopes["verilog_path"],
            spef_path=envelopes["spef_path"],
            spef_paths=spef_envelopes or None,
            replacements=replacements,
            repo_root=repo_root,
        )

    def scrub(self, text: str) -> str:
        for engine_path, label in self._replacements:
            text = text.replace(engine_path, label)
        return text

    @contextmanager
    def scrubbing_errors(self) -> Iterator[None]:
        """Re-raise a :class:`PostRouteStaError` whose message quotes one of
        the three resolved input paths (``[ERROR ODB-...] ... /abs/checkout/
        flows/gcd.def``) with that path rewritten to the input's report
        identity, exactly like the warning sample. The message is otherwise
        untouched, and an error that quotes none of them propagates as-is."""
        try:
            yield
        except PostRouteStaError as exc:
            scrubbed = self.scrub(str(exc))
            if scrubbed == str(exc):
                raise
            raise PostRouteStaError(scrubbed) from exc


@dataclass(frozen=True)
class _StaConstraints:
    """Issue #7023: the twelve validated port/path constraint values that
    travel together unchanged from :func:`run_sta` into every corner
    session -- the scalar and per-port input/output delays, the scalar and
    per-port input transitions, the boundary settings, the report ports,
    the path-stage flag, the violating-endpoint cap, the false-path
    sources, and the report instances.

    Built once after request validation and carried as one value inside
    :class:`_StaRunConfig` through :func:`_dispatch_sta`,
    :func:`_run_multi_corner`, and :func:`_run_corner_session`; the named
    fields are read where the Tcl script and the response are assembled.
    Deliberately excluded: the clock-period resolution and the SPEF choice,
    both of which are resolved per corner and stay explicit arguments.
    """

    input_delay_ns: float | None
    output_delay_ns: float | None
    input_delays: IoDelayMap | None
    output_delays: IoDelayMap | None
    input_transition_ns: float | None
    input_transitions: dict[str, float] | None
    boundary: dict[str, Any] | None
    report_ports: list[str] | None
    report_path_stages: bool
    max_violating_endpoints: int | None
    false_path_from: list[str] | None
    report_instances: list[str] | None


@dataclass(frozen=True)
class _StaRunConfig:
    """Issue #7712: every request field that is identical across the corner
    sessions of one :func:`run_sta` call, resolved and validated once and
    carried as this one value through :func:`_dispatch_sta`,
    :func:`_run_multi_corner`, and :func:`_run_corner_session` -- so a new
    run-invariant request option is one field here instead of a parameter
    on every hop.

    Every ``*_path`` is an absolute engine path; ``report_paths`` carries
    what the response persists. Deliberately excluded, because each varies
    per corner or per dispatch branch and stays an explicit argument: the
    requested corner(s), the clock period and clock extras (period maps are
    per corner), the SPEF choice (``spef_path`` / ``spef_map``), and the
    per-session ``script_tag`` / ``sdf_stem``. ``basename`` and
    ``write_sdf`` are the inputs those two names derive from.
    """

    report_paths: _ReportPaths
    cell_library: str
    pdk_variant: str | None
    pdk_root: str | None
    has_def: bool
    def_path: str | None
    verilog_path: str | None
    hdl_toplevel: str | None
    geometry_source: str
    clock_port: str
    constraints: _StaConstraints
    wire_load_model: str | None
    wire_load_mode: str | None
    macros: list[dict[str, Any]]
    propagated_clock: bool
    power_activity: dict[str, float] | None
    clock_uncertainty: dict[str, float | None] | None
    clock_transition: float | None
    multicycle: list[dict[str, Any]] | None
    timing_derate: dict[str, float | None] | None
    output_dir: str
    input_path: str
    basename: str
    write_sdf: bool


def _response_header(
    run: _StaRunConfig,
    *,
    engine_version: str | None,
    clock_period_ns: float | dict[str, float],
    clock_extras: dict[str, Any] | None,
) -> dict[str, Any]:
    """The eighteen top-level response keys shared, in this order and with
    these values, by the single-corner response (:func:`_dispatch_sta`) and
    the ``corners[]`` response (:func:`_run_multi_corner`) -- every
    request-level field that cannot vary across corners, hoisted to the top
    level. Both callers splice it in with ``**`` ahead of their own keys, so
    the emitted key order is part of this function's contract.
    """
    report_paths = run.report_paths
    return {
        "schema_version": SCHEMA_VERSION,
        "engine": "openroad",
        "engine_version": engine_version,
        "hdl_toplevel": run.hdl_toplevel,
        "status": "ok",
        # Issue #2879 (schema 2): `{path, scope}` envelopes, never the
        # absolute engine path -- see `_ReportPaths`.
        "def_path": report_paths.def_path,
        # Additive field (issue #1825): `null` unless `request.verilog` was
        # given -- the mutually-exclusive counterpart to `def_path` above.
        "verilog_path": report_paths.verilog_path,
        # Additive field (issue #1826): echo of `request.geometry_source`, never `null`
        # (see the module docstring "Pre-route DEFs"); `"routed"` by default,
        # `"netlist_estimate"` whenever `request.verilog` is given (issue #1825).
        "geometry_source": run.geometry_source,
        # Additive fields (issue #1825): the wire-load estimate knob used,
        # `null`/`null` unless `request.verilog` was given (a `def`-mode run's
        # parasitics never come from a liberty wire-load model).
        "wire_load_model": run.wire_load_model,
        "wire_load_mode": run.wire_load_mode,
        # Additive field (issue #2635): echo of `request.macros`, resolved to
        # absolute paths in `klt synthesize`'s own shape; `[]` when none declared.
        "macros": macro_spec.macros_response(run.macros),
        # Additive field (issue #5707): cells linked as untimed black boxes
        # (always present; their delay/power is excluded from the numbers).
        "blackboxed_cells": macro_blackbox.blackboxed_cells_response(run.macros),
        # Additive field (issue #2739): which clock mode every analyzed
        # corner was timed with -- "ideal" (the default, this command's
        # pre-#2739 behaviour) or "propagated". Present on both response
        # shapes; never infer it from clock_skew_ns, which is a measured
        # metric that is non-zero under ideal timing too. Every per-corner
        # session runs the same request-level `constraints.propagated_clock`,
        # so it is hoisted; each `corners[]` entry carries that session's own
        # `clock_propagation` evidence block (the network latencies it
        # reports *do* vary per corner).
        "clock_mode": "propagated" if run.propagated_clock else "ideal",
        "clock_targets": clock_targets(run.clock_port, clock_period_ns, clock_extras),
        "clock_uncertainty": _clock_uncertainty_block(run.clock_uncertainty),
        "clock_transition": _transition.transition_block(run.clock_transition),
        "timing_derate": _derate.timing_derate_block(run.timing_derate),
        "spef_path": report_paths.spef_path,
    }


def _dispatch_sta(
    *,
    requested_corner: str | None,
    requested_corners: list[str] | None,
    run: _StaRunConfig,
    clock_period_ns: float | dict[str, float],
    spef_path: str | None,
    spef_map: dict[str, str] | None,
    clock_extras: dict[str, Any] | None,
) -> dict[str, Any]:
    """Run the single- or multi-corner analysis :func:`run_sta` resolved,
    and assemble its response. Every ``*_path`` field of ``run`` is an
    absolute engine path; ``run.report_paths`` carries what the response
    persists."""
    # Issue #1871: `pdk.corners` (a list) is the multi-corner alternative to
    # the scalar `pdk.corner` above -- both share every request field
    # resolved up to this point (`def`/`verilog` are loaded fresh in *every*
    # corner's own OpenSTA session, never modified in between), and this is
    # the one branch point where their responses actually diverge.
    # Issue #3228: a map-form `spef` always takes the `corners[]` shape, over
    # the one scalar (or nominal) liberty corner when no list was given.
    if requested_corners is not None or spef_map is not None:
        return _run_multi_corner(
            requested_corners if requested_corners is not None else [requested_corner],
            run=run,
            clock_period_ns=clock_period_ns,
            spef_path=spef_path,
            spef_map=spef_map,
            clock_extras=clock_extras,
        )

    assert not isinstance(clock_period_ns, dict)  # maps need pdk.corners
    corner_fields, resolution = _run_corner_session(
        run=run,
        corner_name=requested_corner,
        clock_period_ns=clock_period_ns,
        spef_path=spef_path,
        clock_extras=clock_extras,
        script_tag=run.basename,
        sdf_stem=run.basename if run.write_sdf else None,
    )

    engine_version = openroad_version()
    deck_name = f"{run.cell_library}__{resolution['corner']}"
    provenance = build_provenance(
        deck_name=deck_name,
        deck_path=resolution["liberty_path"],
        deck_path_envelope=run.report_paths.output_envelope(resolution["liberty_path"]),
        pdk=resolution["pdk_info"],
        input_path=run.input_path,
        # Issue #2027: `input_path` is the DEF (a layout stream) when
        # the request supplied one, and the gate-level Verilog netlist
        # otherwise -- the two are not comparable artifacts, so the
        # role follows the same `has_def` branch `input_path` itself
        # does.
        input_role=INPUT_ROLE_LAYOUT if run.has_def else INPUT_ROLE_NETLIST,
    )

    response: dict[str, Any] = {
        **_response_header(
            run,
            engine_version=engine_version,
            clock_period_ns=clock_period_ns,
            clock_extras=clock_extras,
        ),
        "worst_slack_ns": corner_fields["worst_slack_ns"],
        "total_negative_slack_ns": corner_fields["total_negative_slack_ns"],
        "worst_hold_slack_ns": corner_fields["worst_hold_slack_ns"],
        "total_negative_hold_slack_ns": corner_fields["total_negative_hold_slack_ns"],
        "fmax_mhz": corner_fields["fmax_mhz"],
        # A positive OpenSTA sentinel can represent an unconstrained path.
        "timing_status": corner_fields["timing_status"],
        # Measured slack can coexist with unclocked registers or unconstrained pins.
        "constraint_completeness": corner_fields["constraint_completeness"],
        "liberty_warnings": corner_fields["liberty_warnings"],
        "setup_violation_count": corner_fields["setup_violation_count"],
        "hold_violation_count": corner_fields["hold_violation_count"],
        "worst_setup_path": corner_fields["worst_setup_path"],
        "worst_hold_path": corner_fields["worst_hold_path"],
        # Additive field (issue #5424): the unreduced per-path-group rows
        # behind the two records above -- how a caller reads a data-setup
        # WNS when an async recovery check determines `worst_slack_ns`.
        "worst_paths_by_group": corner_fields["worst_paths_by_group"],
        "clock_skew_ns": corner_fields["clock_skew_ns"],
        "estimated_power_mw": corner_fields["estimated_power_mw"],
        "power_basis": corner_fields["power_basis"],
        "provenance": provenance,
    }

    # These fields are present only when the corner session produced them.
    for field in (
        "wire_load",
        "port_paths",
        "instance_paths",
        "generated_clocks",
        "violating_endpoints",
    ):
        if field in corner_fields:
            response[field] = corner_fields[field]
    # Echo the exclusions only when requested (#2968).
    if run.constraints.false_path_from:
        response["false_path_from"] = list(run.constraints.false_path_from)
    if run.multicycle:  # additive (issue #6921): only when entries named
        for field in ("multicycle_paths", "multicycle_engine_sessions"):
            response[field] = corner_fields[field]
    # Required corner fields can carry null when a requested measurement is unavailable.
    for key in (
        "spef_annotation",
        "clock_propagation",
        "sdf",
        "electrical_checks",
        "engine_log",
    ):
        response[key] = corner_fields[key]

    return response


def _run_corner_session(
    *,
    run: _StaRunConfig,
    corner_name: str | None,
    clock_period_ns: float,
    spef_path: str | None,
    clock_extras: dict[str, Any] | None,
    script_tag: str,
    sdf_stem: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run one complete, fresh OpenSTA session (``read_lef`` x2, ``read_def``
    or ``read_verilog``/``link_design``, ``read_liberty``, ``create_clock``,
    optionally ``read_spef``) at ``corner_name`` (the nominal corner when
    ``None``), exactly like :func:`run_sta`'s single-corner path always has.

    ``run`` (issue #7712) carries every corner-invariant request field; the
    unqualified names below (``propagated_clock``, ``macros``,
    ``report_paths``, ...) are its fields. Only what varies per session --
    the corner, its clock period/extras, its SPEF, and the script/SDF names
    -- is passed explicitly.

    ``propagated_clock`` (issue #2739) marks the request's created clock
    propagated in this session (:func:`_sta_script_lines` emits
    ``set_propagated_clock`` for a ``def``-mode request; it is always ``False``
    for ``verilog`` mode -- :func:`_validate_propagated_clock` rejects the
    option there) and adds the parsed ``clock_propagation`` evidence block to
    ``corner_fields``; ``False`` leaves the emitted Tcl and every derived field
    byte-identical to the pre-#2739 session, with ``clock_propagation: None``.

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
    - ``resolution`` -- ``{"corner": str, "liberty_path": str, "pdk_info": dict}``,
      everything the caller needs to build its own ``provenance``/``deck`` block(s);
      kept separate from ``corner_fields`` because the multi-corner caller hoists
      ``pdk_info`` to the top-level response once (shared across every corner)
      while nesting only ``deck`` per corner (see :func:`_run_multi_corner`).

    ``macros`` (issue #2635) adds one ``read_lef`` per declared hard macro
    -- which is what makes a macro-bearing ``def``/``verilog`` load at all
    instead of failing ``[ERROR ORD-2013] instance <inst> LEF master <cell>
    not found`` -- plus one ``read_liberty`` per macro that declared a
    liberty for *this* corner (:func:`macro_blackbox.macro_liberty_paths`).
    ``[]`` (the default for pre-#2635 requests) emits neither, script byte-identical.
    ``sdf_stem`` (issue #2886) -- when given (only ever for a ``def``-mode
    request with a ``spef``; :func:`_validate_sdf` enforces both) -- makes this
    session write ``<output_dir>/<sdf_stem>.<corner>.sdf`` right after ``read_spef``
    and adds its ``{path, scope, content_hash}`` identity to ``corner_fields["sdf"]``
    (``report_paths`` supplies the ``{path, scope}`` resolution). Any previous file
    at that path is removed first, so a missing or empty file after the run is
    this session's own failure, raised, never reported. ``None`` leaves the
    emitted Tcl byte-identical to the pre-#2886 session, with ``sdf: None``.

    Raises :class:`PostRouteStaError` for an unresolvable
    ``cell_library``/``corner_name``/LEF, a macro liberty with no entry for
    this corner, or an OpenROAD engine failure -- otherwise identical to
    this command's pre-#1871 single-corner behaviour."""
    constraints = run.constraints
    has_def = run.has_def
    report_paths = run.report_paths
    macros = run.macros
    output_dir = run.output_dir
    liberty_path, corner, pdk_info = _resolve_liberty(
        run.cell_library, corner_name, variant=run.pdk_variant, root=run.pdk_root
    )
    tech_lef, cell_lef = _resolve_lef(run.cell_library, pdk_info)
    macro_lefs = [macro["lef"] for macro in macros if macro["lef"] is not None]
    macro_lefs.extend(macro_blackbox.write_stub_lefs(macros, output_dir, script_tag))
    macro_liberty_paths = macro_blackbox.macro_liberty_paths(
        macros, corner, error_cls=PostRouteStaError
    )
    decks = (liberty_path, *macro_liberty_paths)
    scrub = report_paths.scrub

    script_path = os.path.join(output_dir, f"sta_{script_tag}.tcl")
    metrics_path = os.path.join(output_dir, f"{script_tag}_metrics.json")
    mcp_lines = _multicycle_lines(run.multicycle)
    sdf_path: str | None = None
    if sdf_stem is not None:
        sdf_path = os.path.join(output_dir, f"{sdf_stem}.{corner}.sdf")
        try:
            os.remove(sdf_path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise PostRouteStaError(
                f"could not remove stale sdf '{sdf_path}': {exc}"
            ) from exc

    if has_def:
        assert run.def_path is not None
        spef_net_names = _spef_net_names(spef_path) if spef_path is not None else None
        design: _DefStaInput | _NetlistStaInput = _DefStaInput(
            def_path=run.def_path,
            spef_path=spef_path,
            spef_net_names=spef_net_names,
            propagated_clock=run.propagated_clock,
            sdf_path=sdf_path,
        )
    else:
        spef_net_names = None
        assert run.verilog_path is not None and run.hdl_toplevel is not None
        design = _NetlistStaInput(
            verilog_path=run.verilog_path,
            hdl_toplevel=run.hdl_toplevel,
            wire_load_model=run.wire_load_model,
            wire_load_mode=run.wire_load_mode,
        )
    lines = _build_sta_session_lines(
        design,
        tech_lef=tech_lef,
        cell_lef=cell_lef,
        liberty_path=liberty_path,
        clock_port=run.clock_port,
        clock_period_ns=clock_period_ns,
        input_delay_ns=constraints.input_delay_ns,
        output_delay_ns=constraints.output_delay_ns,
        macro_lefs=macro_lefs,
        macro_liberty_paths=macro_liberty_paths,
        input_delays=constraints.input_delays,
        output_delays=constraints.output_delays,
        input_transition_ns=constraints.input_transition_ns,
        input_transitions=constraints.input_transitions,
        boundary=constraints.boundary,
        report_ports=constraints.report_ports,
        report_path_stages=constraints.report_path_stages,
        max_violating_endpoints=constraints.max_violating_endpoints,
        false_path_from=constraints.false_path_from,
        report_instances=constraints.report_instances,
        clock_extras=clock_extras,
        power_activity=run.power_activity,
        clock_uncertainty=run.clock_uncertainty,
        clock_transition=run.clock_transition,
        multicycle_lines=mcp_lines,
        timing_derate=run.timing_derate,
    )
    write_script(script_path, lines, label="sta", error_cls=PostRouteStaError)

    completed = run_openroad(
        script_path,
        metrics_path,
        error_cls=PostRouteStaError,
        repo_root=report_paths._repo_root,
    )
    with completed.diagnostics(PostRouteStaError):
        # Issue #2740: a rejected per-port map key stops the session before
        # any timing is reported -- surface that as the actionable request
        # error it is, ahead of the generic engine-failure message.
        port_errors = _parse_port_check_errors(completed.stdout or "")
        if port_errors:
            raise PostRouteStaError(
                _generated.link_error(port_errors)
                or _port_check_error_message(port_errors)
            )
        _check_multicycle_errors(completed.stdout or "", run.multicycle)
        if completed.returncode != 0:
            raise PostRouteStaError(
                _engine_error_message(completed, pdk_info, verilog_mode=not has_def)
            )

        metrics = read_metrics(metrics_path, label="sta", error_cls=PostRouteStaError)
    setup_violation_count = count_violations(
        completed.stdout, _SETUP_VIOLATIONS_BEGIN, _SETUP_VIOLATIONS_END
    )
    hold_violation_count = count_violations(
        completed.stdout, _HOLD_VIOLATIONS_BEGIN, _HOLD_VIOLATIONS_END
    )

    worst_slack = metrics.get("timing__setup__ws")
    tns = metrics.get("timing__setup__tns")
    worst_hold_slack = metrics.get("timing__hold__ws")
    hold_tns = metrics.get("timing__hold__tns")
    fmax_hz = metrics.get("timing__fmax")
    power_w = metrics.get("power__total")
    clock_skew = metrics.get("clock__skew__setup")
    worst_setup_path, worst_hold_path = _worst_paths(
        completed.stdout or "", worst_slack, worst_hold_slack
    )
    stdout = completed.stdout or ""
    worst_setup_path, worst_hold_path = _attach_worst_path_stages(  # issue #5783
        stdout,
        worst_setup_path,
        worst_hold_path,
        enabled=constraints.report_path_stages,
    )

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
        "timing_status": timing_status((worst_slack, worst_hold_slack)),
        # Additive field (issue #2780): whether every endpoint is constrained
        # and every register is clocked in this session -- independent of
        # `timing_status`, which only says whether the slack numbers are
        # measurements. See `_parse_constraint_completeness`.
        "constraint_completeness": _parse_constraint_completeness(
            completed.stdout or ""
        ),
        # Additive field (issue #7411): see `_liberty_warnings`.
        "liberty_warnings": _liberty_warnings.parse(completed, report_paths, decks),
        "setup_violation_count": setup_violation_count,
        "hold_violation_count": hold_violation_count,
        # Additive fields (issue #2740): identity of the worst setup-side /
        # hold-side path end this session found -- see `_worst_path_record`.
        "worst_setup_path": worst_setup_path,
        "worst_hold_path": worst_hold_path,
        # Additive field (issue #5424): every per-path-group row behind
        # those two records, unreduced -- the per-check-family slack the
        # single records cannot carry. See `_worst_paths_by_group`.
        "worst_paths_by_group": _worst_paths_by_group(completed.stdout or ""),
        "clock_skew_ns": round(clock_skew, 5) if clock_skew is not None else None,
        "estimated_power_mw": (
            round(power_w * 1000, 4) if power_w is not None else None
        ),
        # Additive field (issue #3744): the assumptions behind that figure.
        "power_basis": _power_basis_block(run.power_activity, spef_path is not None),
    }
    corner_fields.update(
        _sta_wire_load.corner_fields(
            stdout, run.wire_load_model, run.wire_load_mode, has_def=has_def
        )
    )
    # Optional timing-path details are included only when requested.
    if constraints.report_ports:
        corner_fields["port_paths"] = _port_paths(stdout, constraints.report_ports)
    corner_fields.update(
        _instance_paths_corner_field(stdout, constraints.report_instances)
    )
    corner_fields.update(
        _violating_endpoints_corner_field(
            stdout,
            constraints.max_violating_endpoints,
            setup_violation_count,
            hold_violation_count,
        )
    )
    if spef_path is not None:
        spef_annotation = _spef_annotation_block(completed, spef_net_names)
        # Issue #2879: an OpenSTA reader warning quoting the SPEF (or DEF)
        # by its resolved absolute path is persisted with that input's
        # report identity instead -- bounded to the three known input
        # paths, so every other diagnostic byte is kept verbatim.
        spef_annotation["reader_warning_sample"] = [
            scrub(line) for line in spef_annotation["reader_warning_sample"]
        ]
        corner_fields["spef_annotation"] = spef_annotation
    else:
        corner_fields["spef_annotation"] = None
    # Additive field (issue #3005): max-transition / max-capacitance checks
    # against this session's parasitics; `null` in netlist mode (no routed
    # geometry to check).
    corner_fields["electrical_checks"] = (
        _electrical_checks_block(completed.stdout or "", spef_path is not None)
        if has_def
        else None
    )
    # Issue #2739: the propagated-clock evidence block -- what propagation
    # actually reached -- parsed from this session's `report_clock_latency`
    # dump; `null` unless the request asked for propagated-clock timing.
    if run.propagated_clock and has_def:
        corner_fields["clock_propagation"] = _parse_clock_latency(
            completed.stdout or "", run.clock_port
        )
    else:
        corner_fields["clock_propagation"] = None
    corner_fields["sdf"] = (
        _sdf_record(sdf_path, report_paths, completed=completed, scrub=scrub)
        if sdf_path is not None
        else None
    )
    # Issue #6921: per-entry with/without measurement (other entries
    # stay applied in each "without" baseline).
    corner_fields.update(
        _multicycle_session_fields(
            completed.stdout or "",
            run.multicycle,
            lines,
            output_dir,
            script_tag,
            has_def,
        )
    )

    resolution = {"corner": corner, "liberty_path": liberty_path, "pdk_info": pdk_info}
    corner_fields.update(_generated.response(clock_extras, stdout))
    return corner_fields, resolution


def _run_multi_corner(
    corners: Sequence[str | None],
    *,
    run: _StaRunConfig,
    clock_period_ns: float | dict[str, float],
    spef_path: str | None,
    spef_map: dict[str, str] | None,
    clock_extras: dict[str, Any] | None,
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

    Issue #3228: a map-form ``request.spef`` (``spef_map``, already in
    canonical min/nom/max order) adds an interconnect axis -- one session per
    liberty corner (request order) x supplied interconnect key. Each entry
    then also carries ``interconnect_corner`` and its own ``spef_path``, and
    the top-level ``spef_path`` is ``null``.
    """
    report_paths = run.report_paths
    input_hash_before = sha256_file(run.input_path)

    engine_version = openroad_version()
    shared_pdk_info: dict[str, Any] | None = None
    corner_entries: list[dict[str, Any]] = []
    spef_axis: list[tuple[str | None, str | None]] = (
        list(spef_map.items()) if spef_map is not None else [(None, spef_path)]
    )
    for corner_name, (interconnect, pair_spef) in (
        (c, s) for c in corners for s in spef_axis
    ):
        script_tag = f"{run.basename}__{corner_name or 'nominal'}"
        if interconnect is not None:
            script_tag += f"__{interconnect}"
        corner_period, corner_extras, corner_targets = clock_period_for_corner(
            corner_name, run.clock_port, clock_period_ns, clock_extras
        )
        corner_fields, resolution = _run_corner_session(
            run=run,
            corner_name=corner_name,
            clock_period_ns=corner_period,
            spef_path=pair_spef,
            clock_extras=corner_extras,
            script_tag=script_tag,
            sdf_stem=run.basename if run.write_sdf else None,
        )
        if shared_pdk_info is None:
            shared_pdk_info = resolution["pdk_info"]
        deck_name = f"{run.cell_library}__{resolution['corner']}"
        axis_fields: dict[str, Any] = {}
        if interconnect is not None:
            assert report_paths.spef_paths is not None
            axis_fields = {
                "interconnect_corner": interconnect,
                "spef_path": report_paths.spef_paths[interconnect],
            }
        corner_entries.append(
            {
                "corner": resolution["corner"],
                **axis_fields,
                **corner_fields,
                **corner_targets,
                "deck": _deck_block(
                    deck_name,
                    resolution["liberty_path"],
                    path_envelope=report_paths.output_envelope(
                        resolution["liberty_path"]
                    ),
                ),
            }
        )

    input_hash_after = sha256_file(run.input_path)
    if input_hash_after != input_hash_before:
        raise PostRouteStaError(
            "the geometry backing this multi-corner characterization "
            f"({run.input_path}) changed while characterizing corners "
            + ", ".join(c or "nominal" for c in corners)
            + " -- a klt sta corners response is only meaningful when every "
            "corner analyses the identical geometry, so this run cannot be "
            "reported"
        )

    provenance = build_provenance(
        deck_name=None,
        deck_path=None,
        pdk=shared_pdk_info,
        input_path=run.input_path,
        # Issue #2027: `input_path` is the DEF (a layout stream) when
        # the request supplied one, and the gate-level Verilog netlist
        # otherwise -- the two are not comparable artifacts, so the
        # role follows the same `has_def` branch `input_path` itself
        # does.
        input_role=INPUT_ROLE_LAYOUT if run.has_def else INPUT_ROLE_NETLIST,
    )

    response: dict[str, Any] = {
        # Every request-level field that cannot vary across corners is hoisted
        # to the top level -- the same header the single-corner response opens
        # with (see `_response_header`).
        **_response_header(
            run,
            engine_version=engine_version,
            clock_period_ns=clock_period_ns,
            clock_extras=clock_extras,
        ),
        "provenance": provenance,
        # Additive field (issue #1871): one entry per requested
        # `request.pdk.corners` name, in the same order the request gave
        # them -- see this function's own docstring for the exact shape.
        "corners": corner_entries,
    }
    # Additive field (issue #2968): corner-invariant request echo, present only
    # when the request named `constraints.false_path_from`.
    if run.constraints.false_path_from:
        response["false_path_from"] = list(run.constraints.false_path_from)
    return response


def _spef_annotation_block(
    completed: OpenRoadResult,
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
       holds no parasitics for. ``unannotated_driver_count`` gates, minus the
       floating no-net drivers (``floating_driver_count``, issue #3662);
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
    reader_warnings = _spef_reader_warnings(stdout, stderr)
    delay_changed = _parse_delay_changed(stdout)
    unannotated_drivers, partial_drivers = _parse_parasitic_annotation(stdout)
    unannotated_pins, partial_pins = _parse_parasitic_driver_pins(stdout)
    floating_pins = _parse_floating_drivers(stdout)
    # Issue #3662: a driver pin on no net can never carry parasitics and is on
    # no timed path, so it is split out of the gating count -- but only when
    # the pin listing is trustworthy (it accounts for every reported driver);
    # otherwise the raw OpenSTA count keeps gating, as before.
    floating_unannotated: list[str] | None = None
    if (
        unannotated_drivers is not None
        and floating_pins is not None
        and unannotated_pins is not None
        and len(unannotated_pins) == unannotated_drivers
    ):
        floating_set = {_pin_key(p) for p in floating_pins}
        floating_unannotated = [
            p for p in unannotated_pins if _pin_key(p) in floating_set
        ]
    gating_unannotated = unannotated_drivers
    if unannotated_drivers is not None and floating_unannotated is not None:
        gating_unannotated = unannotated_drivers - len(floating_unannotated)
    driver_detail = (
        _driver_detail(
            _timed_sample(unannotated_pins, floating_unannotated),
            _parse_driver_nets(stdout),
        )
        if floating_unannotated is not None
        else None
    )

    reasons: list[str] = []
    design_nets_missing_sample: list[str] = []
    if not names_correlated:
        reasons.append(
            f"only {design_nets_annotated} of {design_nets_total} nets in "
            f"the linked design are named by this SPEF{SPEF_NAME_MISMATCH_HINT}"
        )
        design_nets_missing_sample = _parse_spef_missing_nets(stdout)
    if reader_warnings:
        discarded = {
            m[1] for line in reader_warnings if (m := _SPEF_MISSING_NET_RE.search(line))
        }
        reasons.append(
            f"read_spef discarded annotation for {len(reader_warnings)} "
            f"SPEF record(s). STA-1650 identified {len(discarded)} distinct "
            "unresolved net name(s). "
            "design_nets_annotated counts name matches before read_spef and does "
            "not establish that the reader accepted their parasitics"
        )
    if delay_changed is False:
        reasons.append(
            "every timing path reported after read_spef is byte-identical to "
            "the same report taken before it, so this run's delays are the "
            f"unannotated ones{SPEF_NO_PIN_ATTACH_HINT}"
        )
    if gating_unannotated:
        reason = (
            f"report_parasitic_annotation found {gating_unannotated} driver(s) "
            "with no parasitics attached after read_spef"
        )
        if driver_detail:
            first = driver_detail[0]
            reason += f" (first: {first['pin']}, {first['net_status']})"
        reasons.append(reason)

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
        "reader_warning_count": len(reader_warnings),
        "reader_warning_sample": reader_warnings[:_SPEF_READER_WARNING_SAMPLE_LIMIT],
        "unannotated_driver_count": unannotated_drivers,
        "partially_unannotated_driver_count": partial_drivers,
        "floating_driver_count": (
            len(floating_unannotated) if floating_unannotated is not None else None
        ),
        "floating_driver_sample": (
            floating_unannotated[:_DRIVER_SAMPLE_LIMIT]
            if floating_unannotated is not None
            else None
        ),
        "unannotated_driver_sample": (
            _timed_sample(unannotated_pins, floating_unannotated)
        ),
        "unannotated_driver_detail": driver_detail,
        "partially_unannotated_driver_sample": (
            partial_pins[:_DRIVER_SAMPLE_LIMIT] if partial_pins is not None else None
        ),
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


def _validate_constraints(
    constraints: Any,
) -> tuple[str, float | dict[str, float], float | None, float | None]:
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
    clocks = _validate_clocks_list(constraints)
    if clocks:
        clock_port = clocks[0]["port"]
        clock_period_ns = clocks[0]["period_ns"]
    else:
        clock_port = constraints.get("clock_port")
        clock_period_ns = constraints.get("clock_period_ns")
    if not (isinstance(clock_port, str) and clock_port):
        if constraints.get("generated_clocks"):
            raise PostRouteStaError(
                "constraints.generated_clocks[0] requires a declared source clock"
            )
        raise PostRouteStaError("request.constraints.clock_port is required")
    if isinstance(clock_period_ns, dict):
        clock_period_ns = validate_period(  # values reach create_clock -period
            clock_period_ns, "request.constraints", "clock_period_ns"
        )
    elif not is_number(clock_period_ns) or clock_period_ns <= 0:
        raise PostRouteStaError(
            "request.constraints.clock_period_ns must be a positive number"
        )
    input_delay_ns = _validate_io_delay(constraints.get("input_delay_ns"), "input")
    output_delay_ns = _validate_io_delay(constraints.get("output_delay_ns"), "output")
    if not isinstance(clock_period_ns, dict):
        clock_period_ns = float(clock_period_ns)
    return clock_port, clock_period_ns, input_delay_ns, output_delay_ns


def _validate_clocks_list(constraints: dict[str, Any]) -> list[dict[str, Any]]:
    """``constraints.clocks`` (issue #4241) as ``[{port, period_ns}]``, empty
    when omitted/``null``. The multi-clock alternative to the
    ``clock_port``/``clock_period_ns`` shorthand -- the two are mutually
    exclusive, and the first entry is the primary clock (it keeps every role
    ``clock_port`` has: the I/O-delay ``-clock``, ``report_ports`` /
    ``false_path_from`` anchors and the input-delay exclusion). Mirrors
    ``synthesize.py``'s ``constraints.clocks`` (issue #2883) rules."""
    raw = constraints.get("clocks")
    if raw is None:
        return []
    if constraints.get("clock_port") is not None or (
        constraints.get("clock_period_ns") is not None
    ):
        raise PostRouteStaError(
            "request.constraints.clocks cannot be combined with "
            "clock_port/clock_period_ns -- give either the single-clock "
            "shorthand or the clocks list"
        )
    if not isinstance(raw, list) or not raw:
        raise PostRouteStaError(
            "request.constraints.clocks must be a non-empty array of "
            "{port, period_ns} objects"
        )
    clocks: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        clock = _validate_clock_entry(entry, f"request.constraints.clocks[{index}]")
        if clock["port"] in seen:
            raise PostRouteStaError(
                f"request.constraints.clocks[{index}].port "
                f"'{clock['port']}' is listed more than once"
            )
        seen.add(clock["port"])
        clocks.append(clock)
    return clocks


def _validate_clock_entry(entry: Any, where: str) -> dict[str, Any]:
    """One ``constraints.clocks`` entry as ``{port, period_ns}`` (issue #4241)."""
    if not isinstance(entry, dict):
        raise PostRouteStaError(f"{where} must be a {{port, period_ns}} object")
    unknown = sorted(set(entry) - {"port", "period_ns"})
    if unknown:
        raise PostRouteStaError(
            f"{where} has unknown key(s): {', '.join(map(str, unknown))} "
            "(allowed: port, period_ns)"
        )
    port = entry.get("port")
    if not isinstance(port, str) or not port.strip():
        raise PostRouteStaError(f"{where}.port must be a non-empty string")
    return {"port": port, "period_ns": validate_period(entry.get("period_ns"), where)}


def _validate_clock_extras(constraints: dict[str, Any]) -> dict[str, Any] | None:
    """Return None for one base clock to preserve its original Tcl and JSON (#4241)."""
    clocks = _validate_clocks_list(constraints)
    declared = (
        {c["port"] for c in clocks} if clocks else {constraints.get("clock_port")}
    )
    generated = _generated.validate(constraints, declared)
    groups_raw = constraints.get("clock_groups")
    groups = (
        _validate_clock_groups(groups_raw, declared) if groups_raw is not None else []
    )
    if len(clocks) <= 1 and not groups and not generated:
        return None
    extras = {"clocks": clocks[1:], "groups": groups}
    if generated:
        extras["generated_clocks"] = generated
    return extras


def _validate_clock_groups(groups_raw: Any, declared: set[Any]) -> list[list[str]]:
    """``constraints.clock_groups`` (issue #4241) as de-duplicated port lists,
    each naming only ``declared`` clocks; at least two groups are required."""
    if not isinstance(groups_raw, list) or not groups_raw:
        raise PostRouteStaError(
            "request.constraints.clock_groups must be a non-empty array "
            "of non-empty port-name arrays"
        )
    groups: list[list[str]] = []
    for index, group in enumerate(groups_raw):
        where = f"request.constraints.clock_groups[{index}]"
        if not isinstance(group, list) or not group:
            raise PostRouteStaError(f"{where} must be a non-empty array")
        ports: list[str] = []
        for port in group:
            if not isinstance(port, str) or not port:
                raise PostRouteStaError(f"{where} entries must be port names")
            if port not in declared:
                raise PostRouteStaError(
                    f"{where} names '{port}', which is not a declared clock"
                )
            if port not in ports:
                ports.append(port)
        groups.append(ports)
    if len(groups) < 2:
        raise PostRouteStaError(
            "request.constraints.clock_groups needs at least two groups "
            "for an asynchronous relationship"
        )
    return groups


def _validate_io_delay(value: Any, which: str) -> float | None:
    """One of ``request.constraints.input_delay_ns``/``.output_delay_ns``
    (issue #1865): optional, and a non-negative number when given. ``0`` is
    a meaningful value ("valid exactly at the clock edge"), so this is
    ``>= 0``, not ``> 0``. Mirrors ``place_and_route.py``'s own
    ``_validate_io_delay`` exactly, modulo the exception class -- the same
    per-module-copy convention ``_clock_lines`` already follows."""
    if value is None:
        return None
    if not is_finite_number(value) or value < 0:
        raise PostRouteStaError(
            f"request.constraints.{which}_delay_ns must be a non-negative number"
        )
    return float(value)


def _validate_input_transitions(
    constraints: dict[str, Any], clock_port: str
) -> tuple[float | None, dict[str, float] | None]:
    """``constraints.input_transition_ns`` / ``.input_transitions`` (issue
    #3227): the default input slew (ns) for non-clock inputs, and a literal
    port/bus-bit -> slew map overriding it. Returns ``(input_transition_ns,
    input_transitions)``, each ``None`` when omitted (the map also when
    empty), which leaves the generated Tcl byte-identical. Values must be
    finite, non-boolean numbers ``>= 0``; keys follow the same literal-port
    rules as the I/O delay maps (clock port rejected here; existence and
    direction are checked in the engine session by :func:`_port_check_lines`).
    """
    scalar = _validate_transition_value(
        constraints.get("input_transition_ns"),
        "request.constraints.input_transition_ns",
    )
    field = "request.constraints.input_transitions"
    value = constraints.get("input_transitions")
    if value is None:
        return scalar, None
    if not isinstance(value, dict):
        raise PostRouteStaError(
            f"{field} must be a JSON object mapping port names to numbers"
        )
    parsed: dict[str, float] = {}
    for port, entry in value.items():
        _check_port_delay_key(field, "input", port, clock_port)
        if entry is None:
            raise PostRouteStaError(
                f"{field}[{port!r}] must be a finite non-negative number"
            )
        validated = _validate_transition_value(entry, f"{field}[{port!r}]")
        assert validated is not None  # entry is not None, checked above
        parsed[port] = validated
    return scalar, parsed or None


def _validate_driving_cell(value: Any, field: str) -> dict[str, str]:
    """One ``{"cell": ..., "pin": ..., "input_pin": ...}`` driving-cell entry
    (``input_pin`` optional). Names are literal liberty identifiers -- never
    Tcl, never a pattern."""
    if not isinstance(value, dict):
        raise PostRouteStaError(
            f'{field} must be a JSON object {{"cell": ..., "pin": ...}} '
            '(optional "input_pin")'
        )
    unknown = sorted(set(value) - {"cell", "pin", "input_pin"})
    if unknown:
        raise PostRouteStaError(
            f"{field} has unknown key(s) {unknown}; allowed: cell, pin, input_pin"
        )
    parsed: dict[str, str] = {}
    for key in ("cell", "pin", "input_pin"):
        item = value.get(key)
        if item is None and key != "input_pin":
            raise PostRouteStaError(f"{field}.{key} is required")
        if item is None:
            continue
        if (
            not isinstance(item, str)
            or not item
            or not _LIBERTY_IDENT_RE.fullmatch(item)
        ):
            raise PostRouteStaError(
                f"{field}.{key} must be a literal liberty name matching [A-Za-z0-9_$.]+"
            )
        parsed[key] = item
    return parsed


def _validate_boundary(
    constraints: dict[str, Any],
    clock_port: str,
    input_transition_ns: float | None,
    input_transitions: dict[str, float] | None,
) -> dict[str, Any] | None:
    """Validate the optional driving cells and output loads for a timing boundary.

    The data driver and input transitions must target disjoint ports. The
    primary clock driver is independent of those data-port constraints.
    Port existence and direction are checked in the engine session.
    """
    prefix = "request.constraints."
    boundary = _validate_driving_cells(constraints, clock_port, prefix)
    if boundary:
        _check_driving_cell_conflicts(
            boundary, input_transition_ns, input_transitions, prefix
        )
    boundary.update(_validate_output_loads(constraints, clock_port, prefix))
    return boundary or None


def _validate_driving_cells(
    constraints: dict[str, Any], clock_port: str, prefix: str
) -> dict[str, Any]:
    """The ``driving_cell`` / ``driving_cells`` half of
    :func:`_validate_boundary` (only the supplied, non-empty keys)."""
    boundary: dict[str, Any] = {}
    for key in ("driving_cell", "clock_driving_cell"):
        if constraints.get(key) is not None:
            boundary[key] = _validate_driving_cell(constraints[key], f"{prefix}{key}")
    field = f"{prefix}driving_cells"
    cells = constraints.get("driving_cells")
    if cells is not None:
        if not isinstance(cells, dict):
            raise PostRouteStaError(
                f"{field} must be a JSON object mapping port names to driving cells"
            )
        parsed_cells: dict[str, dict[str, str]] = {}
        for port, entry in cells.items():
            _check_port_delay_key(field, "input", port, clock_port)
            parsed_cells[port] = _validate_driving_cell(entry, f"{field}[{port!r}]")
        if parsed_cells:
            boundary["driving_cells"] = parsed_cells
    return boundary


def _check_driving_cell_conflicts(
    boundary: dict[str, Any],
    input_transition_ns: float | None,
    input_transitions: dict[str, float] | None,
    prefix: str,
) -> None:
    """Reject a driving cell combined with an input transition on the same
    port (or with the scalar ``input_transition_ns``)."""
    if "driving_cell" not in boundary and "driving_cells" not in boundary:
        return
    if input_transition_ns is not None:
        raise PostRouteStaError(
            f"{prefix}input_transition_ns cannot be combined with "
            "driving_cell/driving_cells -- a driving cell sets the input "
            "slew itself; give per-port input_transitions for the rest"
        )
    overlap = sorted(
        set(boundary.get("driving_cells", {})) & set(input_transitions or {})
    )
    if overlap:
        raise PostRouteStaError(
            f"{prefix}driving_cells and {prefix}input_transitions both "
            f"name port(s) {overlap} -- a port takes one or the other"
        )


def _validate_output_loads(
    constraints: dict[str, Any], clock_port: str, prefix: str
) -> dict[str, Any]:
    """The ``output_load_pf`` / ``output_loads`` half of
    :func:`_validate_boundary` (only the supplied, non-empty keys)."""
    boundary: dict[str, Any] = {}
    scalar = _validate_load_value(
        constraints.get("output_load_pf"), f"{prefix}output_load_pf"
    )
    if scalar is not None:
        boundary["output_load_pf"] = scalar
    field = f"{prefix}output_loads"
    loads = constraints.get("output_loads")
    if loads is not None:
        if not isinstance(loads, dict):
            raise PostRouteStaError(
                f"{field} must be a JSON object mapping port names to numbers"
            )
        parsed_loads: dict[str, float] = {}
        for port, entry in loads.items():
            _check_port_delay_key(field, "output", port, clock_port)
            validated = _validate_load_value(entry, f"{field}[{port!r}]")
            if validated is None:
                raise PostRouteStaError(
                    f"{field}[{port!r}] must be a finite non-negative number"
                )
            parsed_loads[port] = validated
        if parsed_loads:
            boundary["output_loads"] = parsed_loads
    return boundary


def _validate_load_value(value: Any, field: str) -> float | None:
    """One output load (pF): ``None`` when omitted, else a finite,
    non-boolean number ``>= 0``."""
    if value is None:
        return None
    if not is_finite_number(value) or value < 0:
        raise PostRouteStaError(f"{field} must be a finite non-negative number")
    return float(value)


def _validate_transition_value(value: Any, field: str) -> float | None:
    """One input-transition value: ``None`` when omitted, else a finite,
    non-boolean number ``>= 0`` (``0`` is an ideal edge)."""
    if value is None:
        return None
    if not is_finite_number(value) or value < 0:
        raise PostRouteStaError(f"{field} must be a finite non-negative number")
    return float(value)


def _validate_port_delay_maps(
    constraints: dict[str, Any],
    clock_port: str,
    declared_clocks: set[str] | None = None,
) -> tuple[IoDelayMap | None, IoDelayMap | None]:
    """``constraints.input_delays``/``.output_delays`` (issue #2740):
    optional per-port, per-bound I/O delay maps -- validated by
    :mod:`._sta_io_delay_clock`, which owns the entry rules (bounds, key
    form, and issue #7399's optional per-entry ``clock`` naming a declared
    clock; the primary clock when omitted, so a ``clock``-free request
    parses identically)."""
    return _validate_delay_maps(constraints, clock_port, declared_clocks)


def _validate_report_ports(
    constraints: dict[str, Any], clock_port: str
) -> list[str] | None:
    """``constraints.report_ports`` (issue #3042): optional list of unique
    literal port names (one scalar port or one bus bit, e.g. ``data[3]`` --
    the same key rules as the :func:`_validate_port_delay_maps` maps) whose
    own worst setup/hold path is reported as ``port_paths``. Returns the
    list, or ``None`` when the field is omitted *or* empty (nothing is
    emitted, so the generated Tcl is byte-identical to a request without
    it). Existence and direction are verified inside the engine session
    (:func:`_port_check_lines`); the clock port is rejected here."""
    return validate_name_list(
        constraints,
        "report_ports",
        clock_port=clock_port,
        clock_reason="it has no data path of its own",
    )


def _validate_report_path_stages(constraints: dict[str, Any]) -> bool:
    """``constraints.report_path_stages`` (issue #5783): optional boolean;
    ``False`` when omitted, so an unrequested session is byte-identical."""
    field = "request.constraints.report_path_stages"
    value = constraints.get("report_path_stages")
    if value is None:
        return False
    if not isinstance(value, bool):
        raise PostRouteStaError(f"{field} must be a boolean")
    return value


def _resolve_pdk_selection(
    request: dict[str, Any],
) -> tuple[str, str | None, list[str] | None]:
    """Resolve the library and validate the two alternative corner selectors."""
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
    requested_corners = _validate_corners(pdk_spec.get("corners"))
    if requested_corner is not None and requested_corners is not None:
        raise PostRouteStaError(
            "request.pdk.corner and request.pdk.corners are mutually "
            "exclusive -- give at most one: a single scalar corner for the "
            "existing single-corner response shape, or a list for "
            "multi-corner characterization (see docs/cli/sta.md)"
        )
    return cell_library, requested_corner, requested_corners


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


def _validate_power_activity(constraints: dict[str, Any]) -> dict[str, float] | None:
    """``klt sta`` binding of :func:`validate_power_activity` (issue #3744)."""
    return validate_power_activity(constraints, PostRouteStaError)


def _validate_sdf(
    value: Any, *, has_verilog: bool, has_spef: bool, has_spef_map: bool = False
) -> bool:
    """Validate the optional top-level ``request.sdf`` boolean (issue
    #2886), returning ``False`` when omitted or ``null``.

    ``true`` writes one post-route SDF per analyzed corner. Rejected (exit
    1) rather than silently ignored when:

    - the value is not a JSON boolean;
    - the request is ``verilog`` mode -- a linked netlist has no routed
      geometry for the SDF's interconnect delays to reflect;
    - the request has no ``spef`` -- mirroring ``klt place-and-route``'s
      ``post_route_sdf`` requiring ``post_route_spef``: an SDF written
      without real parasitics would carry no routed interconnect delay
      while looking exactly like a post-route measurement to every
      simulation that annotates it;
    - the ``spef`` is the map form (issue #3228) -- one SDF per liberty
      corner would be ambiguous across the interconnect axis.
    """
    if value is None:
        return False
    if not isinstance(value, bool):
        raise PostRouteStaError(
            "request.sdf must be a boolean (true writes one post-route SDF "
            "per corner; omit or false writes none)"
        )
    if not value:
        return False
    if has_verilog:
        raise PostRouteStaError(
            "request.sdf is only valid in def mode -- a verilog-mode request "
            "links a netlist with no routed geometry for an SDF to reflect"
        )
    if has_spef_map:
        raise PostRouteStaError(
            "request.sdf is not supported together with the map form of "
            "request.spef -- an SDF is written per liberty corner, which "
            "is ambiguous across the min/nom/max interconnect axis; give "
            "a single spef path"
        )
    if not has_spef:
        raise PostRouteStaError(
            "request.sdf requires request.spef -- an SDF written without the "
            "routed parasitics would not be a post-route delay file"
        )
    return True


def _sdf_record(
    sdf_path: str,
    report_paths: _ReportPaths | None,
    *,
    completed: OpenRoadResult,
    scrub: Callable[[str], str],
) -> dict[str, Any]:
    """The ``sdf`` response record (issue #2886) for one corner session's
    ``write_sdf`` output: ``{path, scope, content_hash}``, the ``content_hash``
    being the ``sha256:``-prefixed digest of the exact bytes on disk.

    A missing or empty file is a :class:`PostRouteStaError` -- a reported
    path a downstream simulator cannot open degrades there to a *silent*
    zero-delay run (see docs/cli/place-and-route.md "SDF export")."""
    try:
        size = os.path.getsize(sdf_path)
    except OSError:
        size = None
    if not size:
        detail = scrub(_engine_error_message(completed))
        raise PostRouteStaError(
            f"write_sdf produced {'no' if size is None else 'an empty'} file "
            f"'{os.path.basename(sdf_path)}': {detail}"
        )
    envelope = (
        report_paths.output_envelope(sdf_path)
        if report_paths is not None
        else env_provenance.repo_relative_path(sdf_path)
    )
    return {**envelope, "content_hash": f"sha256:{sha256_file(sdf_path)}"}


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


def _validate_macros(
    macros: Any, request_dir: str, *, has_def: bool = False
) -> list[dict[str, Any]]:
    """Validate the optional ``request.macros`` array (issue #2635) -- the
    same shared hard-macro declaration ``klt synthesize`` accepts, field for
    field, documented once in :mod:`klayout_tools.macros`. Returns ``[]``
    when the field is omitted, so every request written before this field
    existed analyses exactly as it did. A thin wrapper rather than a second
    validator: only the exception class differs per verb. Issue #5707: a
    LEF-less entry is a verilog-mode-only untimed black box (see
    :mod:`klayout_tools._macros_blackbox`); ``def`` mode rejects it.
    """
    if has_def:
        macro_blackbox.reject_lefless_in_def(macros, error_cls=PostRouteStaError)
    return macro_spec.validate_macros(
        macros, request_dir, error_cls=PostRouteStaError, allow_blackbox=not has_def
    )


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

    Thin wrapper around :func:`klayout_tools.pdk_cells.resolve_liberty_for_cell_library`
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


# Pins the session's command units so every unit-less ``_ns`` / ``_pF``
# number the generated Tcl emits (clock period, I/O delays, design-rule
# limits) and every ``report_*_metric`` it reads back are in ns / pF
# whatever ``time_unit`` / ``capacitive_load_unit`` the Liberty declares
# (issue #4089: a ``1ps`` Liberty otherwise read ``-period 1.0`` as 1 ps).
_UNIT_PIN_LINE = "set_cmd_units -time ns -capacitance pF"


def _clock_lines(clock_port: str, clock_period_ns: float) -> list[str]:
    return [
        _UNIT_PIN_LINE,
        f"create_clock -name {_tcl_literal(clock_port)} "
        f"-period {clock_period_ns} [get_ports {_tcl_literal(clock_port)}]",
    ]


def _propagated_clock_lines(clock_port: str) -> list[str]:
    """Quote the clock name so Tcl preserves literal bus brackets and spaces (#5708)."""
    return [f"set_propagated_clock [get_clocks {_tcl_literal(clock_port)}]"]


def _extra_clock_lines(clock_extras: dict[str, Any] | None) -> list[str]:
    """Declare secondary clocks and asynchronous groups before derived clocks."""
    if not clock_extras:
        return []
    lines = [
        f"create_clock -name {_tcl_literal(c['port'])} -period {c['period_ns']} "
        f"[get_ports {_tcl_literal(c['port'])}]"
        for c in clock_extras["clocks"]
    ]
    groups = clock_extras["groups"]
    if groups:
        lines.append(
            "set_clock_groups -asynchronous "
            + " ".join(f"-group [get_clocks {{{' '.join(g)}}}]" for g in groups)
        )
    return lines + _generated.constraint_lines(
        clock_extras.get("generated_clocks", []),
        _PORT_CHECK_BEGIN,
        _PORT_CHECK_END,
        _PORT_CHECK_EXIT_CODE,
    )


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
            f"set klt_clock_port [get_ports {_tcl_literal(clock_port)}]",
            "set klt_non_clock_inputs "
            "[lsearch -inline -all -not -exact [all_inputs] $klt_clock_port]",
            f"set_input_delay {input_delay_ns} "
            f"-clock {_tcl_literal(clock_port)} $klt_non_clock_inputs",
        ]
    if output_delay_ns is not None:
        lines.append(
            f"set_output_delay {output_delay_ns} "
            f"-clock {_tcl_literal(clock_port)} [all_outputs]"
        )
    return lines


def _request_io_delay_lines(
    clock_port: str,
    input_delay_ns: float | None,
    output_delay_ns: float | None,
    input_delays: IoDelayMap | None,
    output_delays: IoDelayMap | None,
    input_transition_ns: float | None = None,
    input_transitions: dict[str, float] | None = None,
    report_ports: list[str] | None = None,
    false_path_from: list[str] | None = None,
    report_instances: list[str] | None = None,
    boundary: dict[str, Any] | None = None,
) -> list[str]:
    """All I/O-timing Tcl for one session. With no per-port map, no
    ``input_transitions``, no ``report_ports`` and no ``false_path_from`` this
    is exactly :func:`_io_delay_lines` (the scalar-only contract,
    byte-for-byte the same Tcl as before issue #2740); otherwise
    :func:`_port_check_lines` (once, covering the delay maps, the issue #3227
    ``input_transitions`` map, the issue #3042 ``report_ports`` names and the
    issue #2968 ``false_path_from`` names) followed by the delay Tcl --
    :func:`_port_io_delay_lines` when a delay map is present, else
    :func:`_io_delay_lines` -- then :func:`_input_transition_lines` (nothing
    when no transition field was given) and, last, the
    :func:`_false_path_lines` exceptions."""
    if input_delays or output_delays:
        delay_lines = _port_io_delay_lines(
            clock_port, input_delay_ns, output_delay_ns, input_delays, output_delays
        )
    else:
        delay_lines = _io_delay_lines(clock_port, input_delay_ns, output_delay_ns)
    if (
        input_delays
        or output_delays
        or input_transitions
        or report_ports
        or false_path_from
        or report_instances
        or boundary
    ):
        delay_lines = (
            _port_check_lines(
                input_delays,
                output_delays,
                input_transitions,
                report_ports,
                false_path_from,
                report_instances,
                boundary,
            )
            + delay_lines
        )
    return (
        delay_lines
        + _input_transition_lines(clock_port, input_transition_ns, input_transitions)
        + _boundary_lines(clock_port, boundary, input_transitions)
        + _false_path_lines(false_path_from)
    )


def _port_check_lines(
    input_delays: IoDelayMap | None,
    output_delays: IoDelayMap | None,
    input_transitions: dict[str, float] | None = None,
    report_ports: list[str] | None = None,
    false_path_from: list[str] | None = None,
    report_instances: list[str] | None = None,
    boundary: dict[str, Any] | None = None,
) -> list[str]:
    """Verify every per-port map key against the loaded design before any
    delay is applied (issue #2740): each must match **exactly one** port
    whose full name equals the key literally (so a wildcard, a whole bus
    name, or a near-miss is rejected rather than expanded), with a direction
    the map allows. Failures are printed between
    :data:`_PORT_CHECK_BEGIN`/:data:`_PORT_CHECK_END` and the session exits
    with :data:`_PORT_CHECK_EXIT_CODE`, so a bad key is an actionable error
    -- never a request that silently constrains nothing. The
    ``input_transitions`` map (issue #3227) is checked in the same block
    under its own ``input_transition`` side tag, ``report_ports``
    (issue #3042) on a ``report`` side that accepts a port of any
    direction, and ``false_path_from`` (issue #2968) on a ``false_path``
    side that accepts an input or inout port."""
    entries: list[str] = []
    for side, mapping, allowed in (
        ("input", input_delays, _INPUT_PORT_DIRECTIONS),
        ("output", output_delays, _OUTPUT_PORT_DIRECTIONS),
        ("input_transition", input_transitions, _INPUT_PORT_DIRECTIONS),
        ("driving_cell", (boundary or {}).get("driving_cells"), _INPUT_PORT_DIRECTIONS),
        ("output_load", (boundary or {}).get("output_loads"), _OUTPUT_PORT_DIRECTIONS),
        ("report", dict.fromkeys(report_ports or ()), _ANY_PORT_DIRECTIONS),
        (
            "false_path",
            dict.fromkeys(false_path_from or ()),
            _INPUT_PORT_DIRECTIONS,
        ),
    ):
        for port in mapping or {}:
            entries.append(f"{side} {_tcl_literal(port)} {{{' '.join(allowed)}}}")
    lines = [
        # `get_ports` re-parses its argument as a Tcl list of glob patterns,
        # so `{p`/`"p` raise list errors and `sp ace` splits in two (issue
        # #3050, reproduced live). Enumerate the bit-level ports once and
        # key them by exact full name; every later lookup is a dict key
        # (never re-parsed), and `_port_io_delay_lines` reuses these objects.
        "set klt_ports_by_name [dict create]",
        "foreach klt_port [concat [all_inputs] [all_outputs]] {",
        "    dict set klt_ports_by_name [get_full_name $klt_port] $klt_port",
        "}",
        "proc klt_port_problem {klt_name klt_allowed} {",
        "    global klt_ports_by_name",
        "    if {![dict exists $klt_ports_by_name $klt_name]} { return not_found }",
        "    set klt_port [dict get $klt_ports_by_name $klt_name]",
        "    set klt_dir [get_property $klt_port direction]",
        "    if {[lsearch -exact $klt_allowed $klt_dir] < 0} {",
        '        return "direction_$klt_dir"',
        "    }",
        '    return ""',
        "}",
        "set klt_port_errors {}",
        "foreach {klt_side klt_name klt_allowed} [list " + " ".join(entries) + "] {",
        "    set klt_problem [klt_port_problem $klt_name $klt_allowed]",
        '    if {$klt_problem ne ""} {',
        "        lappend klt_port_errors [list $klt_side $klt_name $klt_problem]",
        "    }",
        "}",
    ]
    # Issue #5754: the `constraints.report_instances` names are checked
    # against their own instance enumeration (never the port dict above),
    # appending to the same error list so one rejected name stops the session
    # before any timing is reported. No lines -- and no enumeration -- when
    # the field is absent, so this block stays byte-identical. The
    # `klt_instances_by_name` dict it builds is reused by the instance-path
    # query later in the script.
    lines += _instance_check_lines(report_instances)
    lines += [
        f'puts "{_PORT_CHECK_BEGIN}"',
        "foreach klt_err $klt_port_errors {",
        '    puts "[lindex $klt_err 0]\\t[lindex $klt_err 1]\\t[lindex $klt_err 2]"',
        "}",
        f'puts "{_PORT_CHECK_END}"',
        "if {[llength $klt_port_errors] > 0} {",
        f"    exit {_PORT_CHECK_EXIT_CODE}",
        "}",
    ]
    return lines


def _port_io_delay_lines(
    clock_port: str,
    input_delay_ns: float | None,
    output_delay_ns: float | None,
    input_delays: IoDelayMap | None,
    output_delays: IoDelayMap | None,
) -> list[str]:
    """Per-port ``set_input_delay``/``set_output_delay`` (issue #2740).

    Precedence: a scalar (``input_delay_ns``/``output_delay_ns``) is the
    default for every port **absent** from the corresponding map -- the
    default is applied to that filtered set only, so it is never written to
    a mapped port and later partially overwritten. Each mapped port then gets
    exactly the bounds its entry gives (``-min``/``-max``), and nothing for a
    bound it omits. The clock port is excluded from the input default
    exactly as in :func:`_io_delay_lines`. ``all_inputs``/``all_outputs``
    enumerate bit-level ports (verified live), so a mapped bus bit
    (``b[0]``) is excluded from the default individually while its sibling
    bits keep it. Mapped ports are fetched from ``$klt_ports_by_name``
    (built by :func:`_port_check_lines`, always emitted first) -- never
    re-looked-up by name through ``get_ports``.

    Issue #7399: an entry naming a declared ``clock`` is emitted
    ``-clock`` that clock instead of the primary (the scalar default and
    every clock-less entry keep the primary).
    """
    lines: list[str] = []
    sides = (
        ("input", "set_input_delay", input_delay_ns, input_delays, "[all_inputs]"),
        ("output", "set_output_delay", output_delay_ns, output_delays, "[all_outputs]"),
    )
    for side, command, scalar, mapping, all_ports in sides:
        mapping = mapping or {}
        if scalar is not None:
            mapped = " ".join(_tcl_literal(port) for port in mapping)
            if side == "input":
                lines += [
                    f"set klt_clock_port [get_ports {_tcl_literal(clock_port)}]",
                    "set klt_input_candidates "
                    "[lsearch -inline -all -not -exact [all_inputs] $klt_clock_port]",
                ]
                candidates = "$klt_input_candidates"
            else:
                candidates = all_ports
            lines += [
                f"set klt_{side}_mapped [list {mapped}]",
                f"set klt_{side}_default_ports {{}}",
                f"foreach klt_port {candidates} {{",
                f"    if {{[lsearch -exact $klt_{side}_mapped "
                "[get_full_name $klt_port]] < 0} {",
                f"        lappend klt_{side}_default_ports $klt_port",
                "    }",
                "}",
                f"if {{[llength $klt_{side}_default_ports] > 0}} {{",
                f"    {command} {scalar} "
                f"-clock {_tcl_literal(clock_port)} $klt_{side}_default_ports",
                "}",
            ]
        for port, bounds in mapping.items():
            # Issue #7399: an entry that names a declared `clock` is emitted
            # relative to that clock (e.g. a strobe), not the primary.
            port_clock = _entry_clock(bounds, clock_port)
            for bound, flag in (("min_ns", "-min"), ("max_ns", "-max")):
                if bound in bounds:
                    lines.append(
                        f"{command} {flag} {bounds[bound]!r} "
                        f"-clock {_tcl_literal(port_clock)} "
                        f"[dict get $klt_ports_by_name {_tcl_literal(port)}]"
                    )
    return lines


def _input_transition_lines(
    clock_port: str,
    input_transition_ns: float | None,
    input_transitions: dict[str, float] | None,
) -> list[str]:
    """``set_input_transition`` for ``constraints.input_transition_ns`` /
    ``.input_transitions`` (issue #3227). The scalar is the default for every
    non-clock input **absent** from the map (bus bits are excluded
    individually, as in :func:`_port_io_delay_lines`); each mapped port then
    gets exactly its own value. Omitted fields emit nothing. The scalar
    compares with ``is not None``: ``0`` is a valid transition."""
    mapping = input_transitions or {}
    lines: list[str] = []
    if input_transition_ns is not None:
        mapped = " ".join(_tcl_literal(port) for port in mapping)
        lines += [
            f"set klt_transition_clock_port [get_ports {_tcl_literal(clock_port)}]",
            "set klt_transition_candidates "
            "[lsearch -inline -all -not -exact [all_inputs] "
            "$klt_transition_clock_port]",
            f"set klt_transition_mapped [list {mapped}]",
            "set klt_transition_default_ports {}",
            "foreach klt_port $klt_transition_candidates {",
            "    if {[lsearch -exact $klt_transition_mapped "
            "[get_full_name $klt_port]] < 0} {",
            "        lappend klt_transition_default_ports $klt_port",
            "    }",
            "}",
            "if {[llength $klt_transition_default_ports] > 0} {",
            f"    set_input_transition {input_transition_ns!r} "
            "$klt_transition_default_ports",
            "}",
        ]
    for port, value in mapping.items():
        lines.append(f"set_input_transition {value!r} [get_ports {_tcl_literal(port)}]")
    return lines


def _boundary_lines(
    clock_port: str,
    boundary: dict[str, Any] | None,
    input_transitions: dict[str, float] | None,
) -> list[str]:
    """``set_driving_cell`` / ``set_load`` for the issue #3742 boundary
    constraints. The default driving cell goes to every non-clock input absent
    from ``driving_cells`` and ``input_transitions``; the default load to every
    output absent from ``output_loads``. Mapped ports are fetched from
    ``$klt_ports_by_name`` (built by :func:`_port_check_lines`). Nothing when
    ``boundary`` is ``None``."""
    if not boundary:
        return []
    lines: list[str] = []
    cells = boundary.get("driving_cells") or {}

    def drive_args(cell: dict[str, str]) -> str:
        args = (
            f"-lib_cell {_tcl_literal(cell['cell'])} -pin {_tcl_literal(cell['pin'])}"
        )
        if "input_pin" in cell:
            args += f" -from_pin {_tcl_literal(cell['input_pin'])}"
        return args

    clock_cell = boundary.get("clock_driving_cell")
    if clock_cell is not None:
        lines.append(
            f"set_driving_cell {drive_args(clock_cell)} "
            f"[get_ports {_tcl_literal(clock_port)}]"
        )
    default_cell = boundary.get("driving_cell")
    if default_cell is not None:
        mapped = " ".join(
            _tcl_literal(port) for port in [*cells, *(input_transitions or {})]
        )
        lines += [
            f"set klt_drive_clock_port [get_ports {_tcl_literal(clock_port)}]",
            "set klt_drive_candidates "
            "[lsearch -inline -all -not -exact [all_inputs] $klt_drive_clock_port]",
            f"set klt_drive_mapped [list {mapped}]",
            "set klt_drive_default_ports {}",
            "foreach klt_port $klt_drive_candidates {",
            "    if {[lsearch -exact $klt_drive_mapped "
            "[get_full_name $klt_port]] < 0} {",
            "        lappend klt_drive_default_ports $klt_port",
            "    }",
            "}",
            "if {[llength $klt_drive_default_ports] > 0} {",
            f"    set_driving_cell {drive_args(default_cell)} $klt_drive_default_ports",
            "}",
        ]
    for port, cell in cells.items():
        lines.append(
            f"set_driving_cell {drive_args(cell)} "
            f"[dict get $klt_ports_by_name {_tcl_literal(port)}]"
        )
    loads = boundary.get("output_loads") or {}
    scalar = boundary.get("output_load_pf")
    if scalar is not None:
        mapped = " ".join(_tcl_literal(port) for port in loads)
        lines += [
            f"set klt_load_mapped [list {mapped}]",
            "set klt_load_default_ports {}",
            "foreach klt_port [all_outputs] {",
            "    if {[lsearch -exact $klt_load_mapped "
            "[get_full_name $klt_port]] < 0} {",
            "        lappend klt_load_default_ports $klt_port",
            "    }",
            "}",
            "if {[llength $klt_load_default_ports] > 0} {",
            f"    set_load {scalar!r} $klt_load_default_ports",
            "}",
        ]
    for port, value in loads.items():
        lines.append(
            f"set_load {value!r} [dict get $klt_ports_by_name {_tcl_literal(port)}]"
        )
    return lines


def _port_path_lines(report_ports: list[str] | None) -> list[str]:
    """Per-port worst setup (``max``) and hold (``min``) path ends,
    marker-delimited (issue #3042); no lines at all without
    ``report_ports``, so an unrequested session is byte-identical.

    For each port: its direction, then ``find_timing_paths -from <port>``
    when the port can launch a path (``input``/``inout``/``bidirect``) and
    ``-to <port>`` when it can capture one (``output``/``inout``/
    ``bidirect``), each with ``-group_path_count 1 -endpoint_path_count 1``
    -- so output is bounded by (ports x path groups x 2), never by design
    size. Every port is ``catch``-wrapped on its own: a failed query yields
    an ``ERROR`` row for that port (reported as ``status: "unavailable"``),
    never a fabricated path. Port names were verified by
    :func:`_port_check_lines` before this runs."""
    if not report_ports:
        return []
    ports = " ".join(_tcl_literal(port) for port in report_ports)
    query = "find_timing_paths {flag} $klt_rp_port -path_delay $klt_rp_side "
    query += "-group_path_count 1 -endpoint_path_count 1"
    row = (
        'puts "PATH\\t$klt_rp_name\\t$klt_rp_side\\t[$klt_pe check_role]\\t'
        "[$klt_pe slack]\\t[get_full_name [get_property $klt_pe startpoint]]\\t"
        '[get_full_name [get_property $klt_pe endpoint]]"'
    )
    lines = [
        f'puts "{_PORT_PATHS_BEGIN}"',
        f"foreach klt_rp_name [list {ports}] {{",
        "    if {[catch {",
        "        set klt_rp_port [get_ports $klt_rp_name]",
        "        set klt_rp_dir [get_property $klt_rp_port direction]",
        '        puts "DIR\\t$klt_rp_name\\t$klt_rp_dir"',
        "        foreach klt_rp_side {max min} {",
    ]
    for flag, excluded in (("-from", "output"), ("-to", "input")):
        lines += [
            f'            if {{$klt_rp_dir ne "{excluded}"}} {{',
            f"                foreach klt_pe [{query.format(flag=flag)}] {{",
            f"                    {row}",
            "                }",
            "            }",
        ]
    lines += [
        "        }",
        "    } klt_rp_error]} {",
        '        puts "ERROR\\t$klt_rp_name\\t'
        '[string map [list \\n { } \\t { }] $klt_rp_error]"',
        "    }",
        "}",
        f'puts "{_PORT_PATHS_END}"',
    ]
    return lines


def _constraint_completeness_lines() -> list[str]:
    """Per-session constraint-completeness query, marker-delimited (issue
    #2780).

    Calls OpenSTA's own ``sta::check_timing_cmd`` -- the structured engine
    query behind ``check_setup`` -- once per check in
    :data:`_COMPLETENESS_CHECKS`, with exactly that one check enabled. The
    command returns a list of records, each ``{message pin pin ...}``; this
    script discards the message and emits the pin names, so identities come
    from the engine's own object list, never from scraping a warning line.
    ``count`` is the full number of pins the engine returned; only the
    sample is capped at :data:`_COMPLETENESS_SAMPLE_LIMIT`.

    Each check is ``catch``-wrapped on its own: an engine whose
    ``check_timing_cmd`` is missing or takes a different argument list
    yields an ``ERROR`` row for that check (reported as ``status:
    "unavailable"``), never a fabricated zero. Tabs/newlines inside a pin
    name or an error message are flattened to spaces so every row stays one
    tab-separated line."""
    checks = " ".join(f"{name} {{{flags}}}" for name, flags in _COMPLETENESS_CHECKS)
    last = _COMPLETENESS_SAMPLE_LIMIT - 1
    return [
        f'puts "{_COMPLETENESS_BEGIN}"',
        f"foreach {{klt_cc_check klt_cc_flags}} {{{checks}}} {{",
        "    if {[catch {",
        "        set klt_cc_pins {}",
        "        foreach klt_cc_record [sta::check_timing_cmd {*}$klt_cc_flags] {",
        "            lappend klt_cc_pins {*}[lrange $klt_cc_record 1 end]",
        "        }",
        '        puts "CHECK\\t$klt_cc_check\\t[llength $klt_cc_pins]"',
        f"        foreach klt_cc_pin [lrange $klt_cc_pins 0 {last}] {{",
        '            puts "ITEM\\t$klt_cc_check\\t'
        '[string map [list \\n { } \\t { }] $klt_cc_pin]"',
        "        }",
        "    } klt_cc_error]} {",
        '        puts "ERROR\\t$klt_cc_check\\t'
        '[string map [list \\n { } \\t { }] $klt_cc_error]"',
        "    }",
        "}",
        f'puts "{_COMPLETENESS_END}"',
    ]


def _spef_net_check_lines(net_names: list[str]) -> list[str]:
    """Correlate SPEF names with the linked design before reading parasitics.

    ``net_names`` uses the unescaped design spelling returned by
    :func:`_spef_net_names` and OpenSTA's ``get_full_name`` (issue #1422).
    The SPEF side counts every listed name found in the design. The design
    side skips empty or driver-only internal nets and samples missing names."""
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
        *SPEF_SKIP_DRIVER_ONLY,
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
        # `-report_unannotated` lists the offending pins (issue #3662); a build
        # without the flag falls back to the counts-only form.
        "if {[catch {report_parasitic_annotation -report_unannotated} "
        "klt_parasitic_annotation_msg]} {",
        "  catch {report_parasitic_annotation} klt_parasitic_annotation_msg",
        "}",
        f'puts "{_PARASITIC_ANNOTATION_END}"',
        f'puts "{_FLOATING_DRIVERS_BEGIN}"',
        # Floating = no net, or a driver-only dangling net (no other pin, no
        # port): either way the DEF/SPEF has nothing to annotate (issue #3669).
        "catch {",
        "  foreach klt_pin [get_pins -hierarchical *] {",
        '    if {[get_property $klt_pin direction] ne "output"} {continue}',
        "    set klt_nets [get_nets -quiet -of_objects $klt_pin]",
        "    set klt_floating [expr {[llength $klt_nets] == 0}]",
        "    if {!$klt_floating} {",
        "      set klt_net [lindex $klt_nets 0]",
        "      set klt_floating [expr {"
        "[llength [get_pins -quiet -of_objects $klt_net]] <= 1 "
        "&& [llength [get_ports -quiet -of_objects $klt_net]] == 0}]",
        "    }",
        "    if {$klt_floating} {",
        f'      puts "{_FLOATING_DRIVER_PREFIX} [get_full_name $klt_pin]"',
        "    }",
        "  }",
        "} klt_floating_drivers_msg",
        f'puts "{_FLOATING_DRIVERS_END}"',
        f'puts "{_DRIVER_NETS_BEGIN}"',
        # A pin on no net yields a one-element list holding a NULL net in
        # OpenSTA 26Q3 (measured, #3668): `get_full_name` on it raises and,
        # uncaught, aborted the loop and dropped every later pin's evidence.
        # Catch per pin; such a pin is already listed as floating above.
        "foreach klt_pin [get_pins -hierarchical *] {",
        "  catch {",
        '    if {[get_property $klt_pin direction] eq "output"} {',
        "      set klt_nets [get_nets -quiet -of_objects $klt_pin]",
        "      if {[llength $klt_nets] == 1} {",
        "        set klt_n [llength [get_pins -quiet -of_objects $klt_nets]]",
        "        if {$klt_n <= 1} {",
        f'          puts "{_DRIVER_NET_PREFIX} [get_full_name $klt_pin] $klt_n '
        '[get_full_name $klt_nets]"',
        "        }",
        "      }",
        "    }",
        "  } klt_driver_nets_msg",
        "}",
        f'puts "{_DRIVER_NETS_END}"',
    ]


def _violation_count_lines() -> list[str]:
    """Marker-delimited setup/hold violator reports for this module's
    standalone ``klt sta`` session (single corner and each
    ``corners[]`` entry -- every per-corner session calls this).

    Delegates the check-family policy to
    :func:`~klayout_tools._openroad_engine.timing_violation_report_lines`
    (issue #2741): the setup side counts data setup + recovery + clock-gating
    setup and the hold side data hold + removal + clock-gating hold -- the
    same populations ``report_worst_slack_metric``/``report_tns_metric``
    measure -- so ``setup_violation_count``/``hold_violation_count`` agree
    with the WNS/TNS fields next to them. Unit: violating endpoints (one
    ``-format end`` row per endpoint per path group), not enumerated paths.
    """
    return timing_violation_report_lines(
        setup_begin=_SETUP_VIOLATIONS_BEGIN,
        setup_end=_SETUP_VIOLATIONS_END,
        hold_begin=_HOLD_VIOLATIONS_BEGIN,
        hold_end=_HOLD_VIOLATIONS_END,
    )


def _electrical_check_lines() -> list[str]:
    """Marker-delimited max-transition / max-capacitance / min-pulse-width /
    min-period violator reports (issues #3005, #5241), run in the same
    session that already did ``read_spef`` so the numbers are against the
    extracted parasitics when a ``spef`` was given.

    Units are pinned with ``set_cmd_units`` (``ns`` / ``pF``) so the report
    is in the units the response states regardless of the Liberty's own.
    Each report runs through ``sta::redirect_string_begin``/``_end`` inside a
    ``catch``: a clean design prints nothing from ``report_check_types``, so
    the explicit ``KLT_ELECTRICAL_OK`` sentinel is what distinguishes
    "checked, zero violations" from a command that never ran
    (``KLT_ELECTRICAL_UNSUPPORTED`` when the ``catch`` fired, after ending the
    redirect so later output is not swallowed; a missing
    block is ``not_run``). For every violating pin a ``KLT_ELECTRICAL_NET``
    row maps it to its net (ports are their own net) so distinct nets can be
    counted."""
    lines = ["set_cmd_units -time ns -capacitance pF"]
    for flag, begin, end in (
        ("-max_slew", _MAX_SLEW_BEGIN, _MAX_SLEW_END),
        ("-max_capacitance", _MAX_CAP_BEGIN, _MAX_CAP_END),
        ("-min_pulse_width", _MIN_PULSE_BEGIN, _MIN_PULSE_END),
        ("-min_period", _MIN_PERIOD_BEGIN, _MIN_PERIOD_END),
    ):
        lines += [
            f'puts "{begin}"',
            "set klt_elec_rep {}",
            "if {[catch {",
            "  sta::redirect_string_begin",
            f"  report_check_types {flag} -violators -digits 5",
            "  set klt_elec_rep [sta::redirect_string_end]",
            "} klt_elec_msg]} {",
            # A throw after redirect_string_begin leaves the redirect active
            # (verified on OpenROAD 26Q3), swallowing every later puts.
            "  catch {sta::redirect_string_end}",
            f'  puts "{_ELECTRICAL_UNSUPPORTED}"',
            "} else {",
            "  puts $klt_elec_rep",
            '  foreach klt_elec_line [split $klt_elec_rep "\\n"] {',
            '    if {[string match "*(VIOLATED)*" $klt_elec_line]} {',
            "      set klt_elec_pin [lindex $klt_elec_line 0]",
            "      set klt_elec_obj [get_pins -quiet $klt_elec_pin]",
            "      if {[llength $klt_elec_obj]} {",
            "        set klt_elec_net [get_nets -quiet -of_objects $klt_elec_obj]",
            "        set klt_elec_net_name [expr {[llength $klt_elec_net] ? "
            "[get_full_name $klt_elec_net] : $klt_elec_pin}]",
            "      } else {",
            "        set klt_elec_net_name $klt_elec_pin",
            "      }",
            f'      puts "{_ELECTRICAL_NET_PREFIX} $klt_elec_pin $klt_elec_net_name"',
            "    }",
            "  }",
            f'  puts "{_ELECTRICAL_OK}"',
            "}",
            f'puts "{end}"',
        ]
    return lines


@dataclass(frozen=True, kw_only=True)
class _DefStaInput:
    """The routed DEF supplies all placement geometry for a fresh session."""

    def_path: str
    spef_path: str | None
    spef_net_names: list[str] | None
    propagated_clock: bool
    sdf_path: str | None

    def load_lines(self, lefs: list[str], liberties: list[str]) -> list[str]:
        if self.sdf_path is not None and self.spef_path is None:
            raise ValueError("sdf_path requires spef_path")
        # DEF component masters must already exist, including all macro LEFs.
        return [*lefs, f"read_def {_tcl_literal(self.def_path)}", *liberties]

    def parasitic_lines(self, clock_port: str) -> list[str]:
        """Bracket SPEF reader warnings separately from SDF writer diagnostics.

        Clock latency and sink counts are queried after annotation so that the
        evidence uses the same parasitics as the timing and power reports.
        """
        lines: list[str] = []
        if self.spef_path is not None:
            lines += _spef_net_check_lines(self.spef_net_names or [])
            lines += _delay_fingerprint_lines(_DELAY_PRE_BEGIN, _DELAY_PRE_END)
            lines.append(f'puts "{_SPEF_READ_BEGIN}"')
            lines.append(f"read_spef {_tcl_literal(self.spef_path)}")  # issue #5708
            lines.append(f'puts "{_SPEF_READ_END}"')
            if self.sdf_path is not None:
                lines.append(write_sdf_line(self.sdf_path))
            lines += _delay_fingerprint_lines(_DELAY_POST_BEGIN, _DELAY_POST_END)
            lines += _parasitic_annotation_lines()
        if self.propagated_clock:
            lines += _clock_latency_lines()
            lines += _clock_sink_count_lines(clock_port)
        return lines


@dataclass(frozen=True, kw_only=True)
class _NetlistStaInput:
    """Link the structural netlist after loading its cell and macro libraries."""

    verilog_path: str
    hdl_toplevel: str
    wire_load_model: str | None
    wire_load_mode: str | None

    def load_lines(self, lefs: list[str], liberties: list[str]) -> list[str]:
        return [
            *liberties,
            *lefs,
            f"read_verilog {_tcl_literal(self.verilog_path)}",
            f"link_design {_tcl_literal(self.hdl_toplevel)}",
        ]

    def parasitic_lines(self, _clock_port: str) -> list[str]:
        """Apply the wire-load estimate and record the engine build."""
        lines: list[str] = []
        if self.wire_load_mode is not None:
            lines.append(f"set_wire_load_mode {self.wire_load_mode}")
        if self.wire_load_model is not None:
            lines.append(f"set_wire_load_model -name {{{self.wire_load_model}}}")
        lines.append(_sta_wire_load.SESSION_MARKER_TCL)
        return lines


def _sta_script_lines(
    *,
    def_path: str,
    spef_path: str | None = None,
    spef_net_names: list[str] | None = None,
    propagated_clock: bool = False,
    sdf_path: str | None = None,
    **session: Any,
) -> list[str]:
    """Build one from-scratch timing session on a routed DEF."""
    return _build_sta_session_lines(
        _DefStaInput(
            def_path=def_path,
            spef_path=spef_path,
            spef_net_names=spef_net_names,
            propagated_clock=propagated_clock,
            sdf_path=sdf_path,
        ),
        **session,
    )


def _sta_netlist_script_lines(
    *,
    verilog_path: str,
    hdl_toplevel: str,
    wire_load_model: str | None = None,
    wire_load_mode: str | None = None,
    **session: Any,
) -> list[str]:
    """Build one from-scratch timing session on a linked Verilog netlist."""
    return _build_sta_session_lines(
        _NetlistStaInput(
            verilog_path=verilog_path,
            hdl_toplevel=hdl_toplevel,
            wire_load_model=wire_load_model,
            wire_load_mode=wire_load_mode,
        ),
        **session,
    )


def _build_sta_session_lines(
    design: _DefStaInput | _NetlistStaInput,
    /,
    *,
    tech_lef: str,
    cell_lef: str,
    liberty_path: str,
    clock_port: str,
    clock_period_ns: float,
    input_delay_ns: float | None = None,
    output_delay_ns: float | None = None,
    macro_lefs: list[str] | None = None,
    macro_liberty_paths: list[str] | None = None,
    input_delays: IoDelayMap | None = None,
    output_delays: IoDelayMap | None = None,
    input_transition_ns: float | None = None,
    input_transitions: dict[str, float] | None = None,
    boundary: dict[str, Any] | None = None,
    report_ports: list[str] | None = None,
    report_path_stages: bool = False,
    max_violating_endpoints: int | None = None,
    false_path_from: list[str] | None = None,
    report_instances: list[str] | None = None,
    clock_extras: dict[str, Any] | None = None,
    power_activity: dict[str, float] | None = None,
    clock_uncertainty: dict[str, float | None] | None = None,
    clock_transition: float | None = None,
    multicycle_lines: list[str] | None = None,
    timing_derate: dict[str, float | None] | None = None,
) -> list[str]:
    """Assemble constraints, parasitics and reports in their engine order."""
    lefs = [
        f"read_lef {_tcl_literal(path)}"
        for path in [tech_lef, cell_lef, *(macro_lefs or [])]
    ]
    liberties = [
        f"read_liberty {_tcl_literal(path)}"
        for path in [liberty_path, *(macro_liberty_paths or [])]
    ]
    lines = design.load_lines(lefs, liberties)
    lines += _clock_lines(clock_port, clock_period_ns)
    lines += _transition.transition_lines(clock_port, clock_transition)
    lines += _extra_clock_lines(clock_extras)
    # Propagate each clock before applying the I/O delays or reading SPEF.
    if isinstance(design, _DefStaInput) and design.propagated_clock:
        lines += _propagated_clock_lines(clock_port)
        if clock_extras:
            for name in [e["port"] for e in clock_extras["clocks"]] + [
                g["name"] for g in clock_extras.get("generated_clocks", [])
            ]:
                lines += _propagated_clock_lines(name)
    lines += _clock_uncertainty_lines(clock_port, clock_uncertainty)
    lines += _derate.timing_derate_lines(timing_derate)
    lines += _request_io_delay_lines(
        clock_port,
        input_delay_ns,
        output_delay_ns,
        input_delays,
        output_delays,
        input_transition_ns,
        input_transitions,
        report_ports,
        false_path_from,
        report_instances,
        boundary,
    )
    # Issue #6921: after the I/O delays/false paths (the port check
    # keeps its stop-the-session precedence), before every report.
    lines += multicycle_lines or []
    lines += design.parasitic_lines(clock_port)
    lines += [
        "report_worst_slack_metric -setup",
        "report_worst_slack_metric -hold",
        "report_tns_metric -setup",
        "report_tns_metric -hold",
        "report_fmax_metric",
    ]
    lines += _power_activity_lines(power_activity)
    lines += [
        "report_power_metric",
        "report_clock_skew_metric -setup",
    ]
    lines += _violation_count_lines()
    lines += _worst_path_lines()
    lines += _worst_path_stage_lines(report_path_stages)  # issue #5783
    lines += _violating_endpoint_lines(max_violating_endpoints)  # issue #7418
    lines += _port_path_lines(report_ports)
    lines += _instance_path_lines(report_instances)
    # Before the completeness query, which must stay the script's last
    # statements (issue #2780): it can take the session down.
    if isinstance(design, _DefStaInput):
        lines += _electrical_check_lines()
    lines += _generated.evidence_lines(clock_extras)
    lines += _constraint_completeness_lines()
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
    name_map = _spef_name_map(text)
    divider_match = _SPEF_HEADER_DIVIDER_RE.search(text)
    divider = divider_match.group(1) if divider_match else "/"
    names: set[str] = set()
    for raw in _SPEF_D_NET_RE.findall(text):
        # A name-mapped SPEF (OpenROAD's ``write_spef``) declares nets as
        # ``*D_NET *12 <cap>``; resolve the index through ``*NAME_MAP``
        # (issue #3742). An unmapped ``*<n>`` is kept verbatim.
        name = _unescape_spef_name(name_map.get(raw, raw))
        if divider != "/":
            # OpenSTA addresses hierarchy with ``/``; translate a header
            # ``*DIVIDER`` of another character to it.
            name = name.replace(divider, "/")
        names.add(name)
    return sorted(names)


def _spef_name_map(text: str) -> dict[str, str]:
    """``{"*12": "data\\[0\\]", ...}`` from the ``*NAME_MAP`` section (the
    still-escaped spelling; the caller unescapes), ``{}`` when the SPEF has
    none. The section is the run of ``*<n> <name>`` lines after the
    ``*NAME_MAP`` header; it ends at the first other ``*`` section keyword."""
    names: dict[str, str] = {}
    in_map = False
    for line in text.splitlines():
        stripped = line.strip()
        if not in_map:
            in_map = stripped == "*NAME_MAP"
            continue
        match = _SPEF_NAME_MAP_ENTRY_RE.match(stripped)
        if match:
            names[match.group(1)] = match.group(2)
        elif stripped.startswith("*"):
            break
    return names


# --------------------------------------------------------------------------- #
# OpenROAD subprocess invocation + output parsing
# --------------------------------------------------------------------------- #


def _engine_error_message(
    completed: OpenRoadResult,
    pdk_info: dict[str, Any] | None = None,
    *,
    verilog_mode: bool = False,
) -> str:
    """Build an actionable error message from a failed OpenROAD run.

    This verb's hint list wrapped around the shared diagnostic scraper
    :func:`~klayout_tools._openroad_io.format_engine_error`: a bracketed
    OpenROAD error is preferred over a bare Tcl error, as place-and-route
    does, and a container runtime or mount hint is appended when the output
    supports it. The mount hint uses the resolved PDK root when the unreadable
    path is a PDK asset. The raw diagnostic remains available in either
    case."""
    return format_engine_error(
        completed,
        context="sta run",
        hints=(
            container_runtime_hint(completed),
            mount_namespace_hint(completed, pdk_info),
            # Issue #2635: `ORD-2013` names the instance and the LEF master
            # but nothing about which request field would have supplied it --
            # the one failure mode a caller with a hard macro and no
            # `request.macros` entry hits first. Appended as a further `--`
            # clause, the same recognized-hint structure the hints above use.
            macro_spec.missing_lef_master_hint(
                (completed.stdout or "") + "\n" + (completed.stderr or ""),
                blackbox_form=verilog_mode,
            ),
        ),
    )


def _spef_reader_warnings(stdout: str, stderr: str) -> list[str]:
    """All diagnostics from the bracketed read_spef call, before sample capping.

    When the stdout markers are missing, accept only known reader codes.
    Stderr has no markers, so it always needs the narrower filter.
    """
    region = _extract_block(stdout, _SPEF_READ_BEGIN, _SPEF_READ_END)
    stdout_pattern = _STA_WARNING_RE if region is not None else _SPEF_READER_WARNING_RE
    output = region if region is not None else stdout
    return [
        line.strip() for line in output.splitlines() if stdout_pattern.search(line)
    ] + [
        line.strip()
        for line in stderr.splitlines()
        if _SPEF_READER_WARNING_RE.search(line)
    ]


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


def _parse_parasitic_driver_pins(
    stdout: str,
) -> tuple[list[str] | None, list[str] | None]:
    """``(unannotated_pins, partially_unannotated_pins)`` listed by
    ``report_parasitic_annotation -report_unannotated`` (issue #3662): the
    pin-name lines that follow each ``Found <n> ... drivers.`` summary line.
    ``None`` for a list whose summary line is absent; an empty list when the
    summary is present with no pins listed (an OpenSTA build without the
    flag -- the caller then sees a length mismatch against the count and
    degrades)."""
    block = _extract_block(
        stdout, _PARASITIC_ANNOTATION_BEGIN, _PARASITIC_ANNOTATION_END
    )
    if block is None:
        return (None, None)
    unannotated: list[str] | None = None
    partial: list[str] | None = None
    current: list[str] | None = None
    for raw in block.splitlines():
        line = raw.strip()
        if not line:
            continue
        if _PARTIAL_DRIVERS_RE.search(line):
            partial = current = []
        elif _UNANNOTATED_DRIVERS_RE.search(line):
            unannotated = current = []
        elif current is not None and " " not in line:
            current.append(line)
    return (unannotated, partial)


def _parse_floating_drivers(stdout: str) -> list[str] | None:
    """Output pins that sit on no net, from the
    :data:`_FLOATING_DRIVERS_BEGIN` block, or ``None`` when the block is
    absent (an older script / a run that never reached it)."""
    block = _extract_block(stdout, _FLOATING_DRIVERS_BEGIN, _FLOATING_DRIVERS_END)
    if block is None:
        return None
    prefix = _FLOATING_DRIVER_PREFIX + " "
    return [
        line.strip()[len(prefix) :]
        for line in block.splitlines()
        if line.strip().startswith(prefix)
    ]


def _pin_key(pin: str) -> str:
    """Spelling-insensitive pin key: ``report_parasitic_annotation`` may print
    escaped bus brackets (``a\\[0\\]/Z``) where ``get_full_name`` does not."""
    return pin.replace("\\", "")


def _parse_driver_nets(stdout: str) -> dict[str, tuple[int, str]] | None:
    """``{pin_key: (net_pin_count, net_name)}`` for output pins on a net with
    at most one instance pin (issue #3668), or ``None`` when the block is
    absent."""
    block = _extract_block(stdout, _DRIVER_NETS_BEGIN, _DRIVER_NETS_END)
    if block is None:
        return None
    prefix = _DRIVER_NET_PREFIX + " "
    rows: dict[str, tuple[int, str]] = {}
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith(prefix):
            continue
        parts = line[len(prefix) :].split(" ", 2)
        if len(parts) == 3 and parts[1].isdigit():
            rows[_pin_key(parts[0])] = (int(parts[1]), parts[2])
    return rows


def _timed_sample(
    unannotated_pins: list[str] | None, floating: list[str] | None
) -> list[str] | None:
    """Capped sample of the unannotated drivers that are *not* floating -- the
    ones that gate. ``None`` when the pin listing is unavailable."""
    if unannotated_pins is None:
        return None
    skip = {_pin_key(p) for p in floating or []}
    return [p for p in unannotated_pins if _pin_key(p) not in skip][
        :_DRIVER_SAMPLE_LIMIT
    ]


def _driver_detail(
    sample: list[str] | None, driver_nets: dict[str, tuple[int, str]] | None
) -> list[dict[str, Any]] | None:
    """Per-pin account of the gating unannotated drivers: which pin, and what
    its net looks like (``single_pin_net`` = no other instance pin on it, so
    an unconnected or port-only net; otherwise ``net_with_other_pins``).
    ``None`` when the pin listing is unavailable."""
    if sample is None:
        return None
    nets = driver_nets or {}
    detail: list[dict[str, Any]] = []
    for pin in sample:
        found = nets.get(_pin_key(pin))
        detail.append(
            {
                "pin": pin,
                "net_status": "single_pin_net" if found else "net_with_other_pins",
                "net": found[1] if found else None,
            }
        )
    return detail


def _electrical_checks_block(stdout: str, has_spef: bool) -> dict[str, Any]:
    """The per-corner ``electrical_checks`` response block (issue #3005).
    ``parasitics_source`` says whether the numbers are against the
    caller-supplied extracted SPEF or LEF/DEF-only wire models."""
    return {
        "parasitics_source": "spef" if has_spef else "none",
        "max_transition": _parse_electrical_check(
            stdout, _MAX_SLEW_BEGIN, _MAX_SLEW_END, "ns"
        ),
        "max_capacitance": _parse_electrical_check(
            stdout, _MAX_CAP_BEGIN, _MAX_CAP_END, "pF"
        ),
        "min_pulse_width": _parse_electrical_slack_check(
            stdout, _MIN_PULSE_BEGIN, _MIN_PULSE_END
        ),
        "min_period": _parse_electrical_slack_check(
            stdout, _MIN_PERIOD_BEGIN, _MIN_PERIOD_END
        ),
    }


# --------------------------------------------------------------------------- #
# Per-port I/O-delay check + worst-path identity parsing (issue #2740)
# --------------------------------------------------------------------------- #


def _parse_port_check_errors(stdout: str) -> list[tuple[str, str, str]]:
    """``(side, port, problem)`` rows from :func:`_port_check_lines`' block,
    or ``[]`` when the block is absent or empty."""
    block = _extract_block(stdout, _PORT_CHECK_BEGIN, _PORT_CHECK_END)
    if block is None:
        return []
    rows: list[tuple[str, str, str]] = []
    for line in block.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            rows.append((parts[0], parts[1], parts[2]))
    return rows


#: The noun the ``not_found`` message names, per check side (issue #5754
#: added the instance side; every other side checks a port).
_CHECK_NOT_FOUND_NOUNS: dict[str, str] = {"report_instance": "instance"}

#: The request field each named check side maps to in error messages --
#: the ``report``/``report_instance`` sides name their own constraint key,
#: not a ``*_delays`` map (issue #3042, #5754).
_CHECK_FIELD_NAMES: dict[str, str] = {
    "report": "report_ports",
    "report_instance": "report_instances",
}

_PORT_CHECK_PREFIXES: dict[str | None, str] = {
    "report": "constraints.report_ports rejected",
    "report_instance": "constraints.report_instances rejected",
    "false_path": "constraints.false_path_from rejected",
    "input_transition": "constraints.input_transitions rejected",
    "driving_cell": "constraints.driving_cells rejected",
    "output_load": "constraints.output_loads rejected",
}


def _port_check_error_message(rows: list[tuple[str, str, str]]) -> str:
    details = []
    for side, port, problem in rows:
        if side == "input_transition":
            field = f"constraints.input_transitions[{port!r}]"
            side = "input"
        elif side == "driving_cell":
            field = f"constraints.driving_cells[{port!r}]"
            side = "input"
        elif side == "output_load":
            field = f"constraints.output_loads[{port!r}]"
            side = "output"
        elif side in _CHECK_FIELD_NAMES:
            field = f"constraints.{_CHECK_FIELD_NAMES[side]}[{port!r}]"
        elif side == "false_path":
            field = f"constraints.false_path_from[{port!r}]"
        else:
            field = f"constraints.{side}_delays[{port!r}]"
        if problem == "not_found":
            noun = _CHECK_NOT_FOUND_NOUNS.get(side, "port")
            details.append(f"{field}: no {noun} with exactly this name in the design")
        elif problem.startswith("matches_"):
            count = problem[len("matches_") :].split("_", 1)[0]
            details.append(
                f"{field}: matches {count} ports -- name a single port or "
                "an individual bus bit (e.g. 'data[3]'); keys are literal, "
                "never wildcards or whole buses"
            )
        elif problem.startswith("direction_"):
            direction = problem[len("direction_") :]
            details.append(
                f"{field}: port direction is '{direction}', not an "
                f"{'input' if side == 'false_path' else side} "
                "-- move it to the other map or drop it"
            )
        else:
            details.append(f"{field}: {problem}")
    sides = {side for side, _, _ in rows}
    only_side = next(iter(sides)) if len(sides) == 1 else None
    prefix = _PORT_CHECK_PREFIXES.get(
        only_side, "per-port I/O delay constraint(s) rejected"
    )
    return f"{prefix}: " + "; ".join(details)


def _unavailable_completeness_check(reason: str) -> dict[str, Any]:
    return {
        "status": "unavailable",
        "count": None,
        "sample": [],
        "truncated": None,
        "reason": reason,
    }


def _parse_constraint_completeness(stdout: str) -> dict[str, Any]:
    """The ``constraint_completeness`` block for one session (issue #2780),
    parsed from :func:`_constraint_completeness_lines`' marker-delimited
    output.

    Each check in :data:`_COMPLETENESS_CHECKS` becomes
    ``{status, count, sample, truncated, reason}``:

    - ``"ok"`` -- the engine query ran: ``count`` is the engine's full
      total (``0`` only when the engine returned no offending pin),
      ``sample`` the first :data:`_COMPLETENESS_SAMPLE_LIMIT` pin names in
      engine order, ``truncated`` whether ``sample`` is shorter than
      ``count``.
    - ``"unavailable"`` -- the block is absent, the engine query failed
      (``ERROR`` row), or the rows are malformed (missing/duplicate
      ``CHECK`` row, non-integer count, an ``ITEM`` count that disagrees
      with the total and the sample cap). ``count``/``truncated`` are
      ``null`` and ``reason`` says why -- a check that did not run never
      reports ``0``.

    Lines that are not ``CHECK``/``ITEM``/``ERROR`` rows (stray engine
    warnings interleaved in stdout) are ignored; they can never become an
    identity.

    The roll-up ``status`` is ``"incomplete"`` when any check that ran
    reported at least one offending pin, ``"complete"`` when every check ran
    and reported none, and ``"unknown"`` otherwise (no check found a gap
    but at least one could not run)."""
    names = [name for name, _flags in _COMPLETENESS_CHECKS]
    block = _extract_block(stdout, _COMPLETENESS_BEGIN, _COMPLETENESS_END)
    checks: dict[str, dict[str, Any]]
    if block is None:
        reason = "constraint-completeness records absent from engine output"
        checks = {name: _unavailable_completeness_check(reason) for name in names}
    else:
        checks = {name: _parse_completeness_check(block, name) for name in names}

    statuses = [check["status"] for check in checks.values()]
    if any(check["status"] == "ok" and check["count"] > 0 for check in checks.values()):
        overall = "incomplete"
    elif all(status == "ok" for status in statuses):
        overall = "complete"
    else:
        overall = "unknown"
    return {
        "status": overall,
        **checks,
        "sample_limit": _COMPLETENESS_SAMPLE_LIMIT,
    }


def _completeness_rows(
    block: str, name: str
) -> tuple[list[str], list[str], str | None]:
    """``(totals, items, error)`` -- the ``CHECK`` totals and ``ITEM`` pin
    names the completeness block carries for check ``name``, or a non-null
    ``error`` reason on an engine ``ERROR`` row or a malformed row. Rows of
    any other kind or check are ignored."""
    totals: list[str] = []
    items: list[str] = []
    for line in block.splitlines():
        parts = line.split("\t")
        if len(parts) < 2 or parts[1] != name:
            continue
        if parts[0] == "ERROR":
            message = "\t".join(parts[2:]).strip()
            return totals, items, f"engine {name} query failed: {message}"
        if parts[0] not in ("CHECK", "ITEM"):
            continue
        if len(parts) != 3 or not parts[2]:
            return totals, items, f"malformed {name} record: {line!r}"
        (totals if parts[0] == "CHECK" else items).append(parts[2])
    return totals, items, None


def _parse_completeness_check(block: str, name: str) -> dict[str, Any]:
    """One check's record from the completeness block -- see
    :func:`_parse_constraint_completeness`."""
    totals, items, error = _completeness_rows(block, name)
    if error is not None:
        return _unavailable_completeness_check(error)
    if not totals:
        return _unavailable_completeness_check(f"no {name} total in engine output")
    if len(totals) > 1:
        return _unavailable_completeness_check(
            f"duplicate {name} totals in engine output: {totals!r}"
        )
    total_text = totals[0]
    if re.fullmatch(r"[0-9]+", total_text) is None:
        return _unavailable_completeness_check(
            f"malformed {name} total: {total_text!r}"
        )
    count = int(total_text)
    if len(items) != min(count, _COMPLETENESS_SAMPLE_LIMIT):
        return _unavailable_completeness_check(
            f"{name} sample has {len(items)} pin(s) for a total of {count} "
            f"(sample limit {_COMPLETENESS_SAMPLE_LIMIT})"
        )
    return {
        "status": "ok",
        "count": count,
        "sample": items,
        "truncated": count > len(items),
        "reason": None,
    }

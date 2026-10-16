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

Scope deliberately excluded from the first version and landed since: a
``propagated_clock`` request option (issue #2739, below). Still excluded
(not required for this issue): a bisected (rather than
``report_fmax_metric``'s ``1/(T-WNS)`` extrapolated) ``fmax_mhz``.

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
``report_clock_latency`` so the response carries machine-readable evidence
of what propagation actually reached: the clock pins the clock network
reaches (``clock_propagation.clock_sinks``) and the worst network latency
among them. On a pinned-build session a routed tree moves a boundary hold
check from met to violated exactly as the issue describes -- which is the
point: ideal timing can call an input hold check MET that the propagated
analysis calls VIOLATED.

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
"""

from __future__ import annotations

import json
import os
import re
import subprocess  # noqa: F401 -- tests patch post_route_sta.subprocess.run
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import Any

from . import env_provenance
from ._openroad_engine import (
    _container_runtime_hint,
    _count_violations,
    _openroad_version,
    _OpenRoadResult,
    _run_openroad,
    _timing_status,
    _timing_violation_report_lines,
    write_sdf_line,
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
#: Marker pairs and sentinels for the per-corner ``electrical_checks`` block
#: (issue #3005): one ``report_check_types -violators`` region per limit class.
#: See :func:`_electrical_check_lines` / :func:`_parse_electrical_check`.
_MAX_SLEW_BEGIN = "===KLT_STA_MAX_SLEW_BEGIN==="
_MAX_SLEW_END = "===KLT_STA_MAX_SLEW_END==="
_MAX_CAP_BEGIN = "===KLT_STA_MAX_CAP_BEGIN==="
_MAX_CAP_END = "===KLT_STA_MAX_CAP_END==="
_ELECTRICAL_OK = "KLT_ELECTRICAL_OK"
_ELECTRICAL_UNSUPPORTED = "KLT_ELECTRICAL_UNSUPPORTED"
_ELECTRICAL_NET_PREFIX = "KLT_ELECTRICAL_NET"
#: One violator row of ``report_check_types``: ``<pin> <limit> <value> <slack>
#: (VIOLATED)``. Anchored from the right so a pin name is taken verbatim.
_ELECTRICAL_ROW_RE = re.compile(
    r"^(?P<pin>\S.*?)\s+(?P<limit>-?[\d.]+(?:e[-+]?\d+)?)\s+"
    r"(?P<value>-?[\d.]+(?:e[-+]?\d+)?)\s+(?P<slack>-?[\d.]+(?:e[-+]?\d+)?)"
    r"\s+\(VIOLATED\)\s*$",
    re.IGNORECASE,
)
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

#: Issue #2740: brackets the per-path-group worst setup/hold path records
#: (:func:`_worst_path_lines`), one
#: ``PATH\t<max|min>\t<check_role>\t<slack_s>\t<startpoint>\t<endpoint>`` row
#: per path group per side, or a single ``ERROR\t<message>`` row when the
#: engine's path introspection itself failed.
_WORST_PATHS_BEGIN = "===KLT_STA_WORST_PATHS_BEGIN==="
_WORST_PATHS_END = "===KLT_STA_WORST_PATHS_END==="

#: Issue #3042: brackets the per-port worst-path records
#: (:func:`_port_path_lines`) a ``constraints.report_ports`` request emits.
#: Per requested port: one ``DIR\t<port>\t<direction>`` row, then
#: ``PATH\t<port>\t<max|min>\t<check_role>\t<slack_s>\t<startpoint>\t<endpoint>``
#: rows (an ``inout`` port contributes its ``-from`` and its ``-to`` paths),
#: or a single ``ERROR\t<port>\t<message>`` row when that port's query failed.
_PORT_PATHS_BEGIN = "===KLT_STA_PORT_PATHS_BEGIN==="
_PORT_PATHS_END = "===KLT_STA_PORT_PATHS_END==="

#: Issue #2739: brackets the ``report_clock_latency -digits 6`` dump a
#: ``propagated_clock`` request emits after ``set_propagated_clock`` (and,
#: when present, after ``read_spef``) -- the machine-readable evidence of
#: what propagation actually reached. Per clock: a ``Clock <name>`` header
#: line, then one (min-column) and one continuation (max-column)
#: ``network latency <pin>`` row per clock sink per transition pair. A clock
#: whose network reaches no clock pin prints the header and nothing else --
#: the explicit "propagation could not be established" signal
#: :func:`_parse_clock_latency` turns into ``clock_sinks: 0``.
_CLOCK_LATENCY_BEGIN = "===KLT_STA_CLOCK_LATENCY_BEGIN==="
_CLOCK_LATENCY_END = "===KLT_STA_CLOCK_LATENCY_END==="
#: ``report_clock_latency``'s per-clock section header (``Clock clk``).
_CLOCK_SECTION_RE = re.compile(r"^Clock (\S+)$")
#: One ``network latency <pin>`` row: the min-column value on the row
#: OpenSTA prints for the sink, and the max-column value on the continuation
#: line it indents under the same pin -- one regex captures both.
_NETWORK_LATENCY_RE = re.compile(r"^\s*([-+0-9.eE]+)\s+network latency (.+?)\s*$")
#: Upper bound on how many clock-sink pin names the parsed evidence echoes
#: into ``clock_propagation.clock_sinks_sample`` -- a diagnostic sample, not
#: an exhaustive list (a real CTS tree reaches thousands of clock pins).
_CLOCK_SINK_SAMPLE_LIMIT = 20

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

#: OpenSTA ``PathEnd::checkRole()`` names -> this command's ``check_type``
#: values. Verified live (OpenROAD ``openroad/orfs:latest`` image, 2026-10-10)
#: for ``setup``, ``hold``, ``recovery``, ``removal``, ``clock gating
#: setup``, ``clock gating hold`` and ``output setup``; ``output hold``,
#: ``latch setup`` and ``latch hold`` are the remaining OpenSTA
#: ``TimingRole`` names a path end can carry. Any role not listed here is
#: reported as ``check_type: "unknown"`` (with the engine's own spelling kept
#: in ``engine_check_role``) -- never guessed into a setup/hold bucket.
_CHECK_ROLE_TYPES = {
    "setup": "setup",
    "hold": "hold",
    "recovery": "recovery",
    "removal": "removal",
    "clock gating setup": "clock_gating_setup",
    "clock gating hold": "clock_gating_hold",
    "output setup": "output_setup",
    "output hold": "output_hold",
    "latch setup": "latch_setup",
    "latch hold": "latch_hold",
}

#: Largest |path slack - aggregate WNS| (ns) still treated as "the same
#: number": the aggregate comes from ``report_worst_slack_metric`` (printed
#: to 6 decimals in the engine's time unit) while the path slack is the
#: engine's own full-precision value in seconds.
_SLACK_CONSISTENCY_TOL_NS = 1e-4

#: The direction strings OpenSTA's ``get_property <port> direction`` may
#: return that a per-port input/output delay may be applied to.
_INPUT_PORT_DIRECTIONS = ("input", "inout", "bidirect")
_OUTPUT_PORT_DIRECTIONS = ("output", "inout", "bidirect")
#: Issue #3042: ``constraints.report_ports`` accepts a port of any direction.
_ANY_PORT_DIRECTIONS = ("input", "output", "inout", "bidirect")


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
    input_delays, output_delays = _validate_port_delay_maps(
        request["constraints"], clock_port
    )
    input_transition_ns, input_transitions = _validate_input_transitions(
        request["constraints"], clock_port
    )
    report_ports = _validate_report_ports(request["constraints"], clock_port)
    false_path_from = _validate_false_path_from(request["constraints"], clock_port)
    wire_load_model, wire_load_mode = _validate_wire_load_estimate(
        request["constraints"], has_verilog
    )
    propagated_clock = _validate_propagated_clock(request["constraints"], has_def)

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
            spef_map = _resolve_spef_map(spef_field, request_dir)
            spef_path = None
        else:
            spef_path = _resolve_spef(spef_field, request_dir)
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

    # Issue #2879: the absolute `def_path`/`verilog_path`/`spef_path` above
    # are *engine* paths -- what `read_def`/`read_verilog`/`read_spef` and
    # the hashing below consume. What the response persists is derived from
    # them exactly once, here, and never fed back to the engine.
    report_paths = _ReportPaths.build(
        def_path=def_path,
        verilog_path=verilog_path,
        spef_path=spef_path,
        spef_map=spef_map,
        repo_root=env_provenance.find_repo_root(request_dir),
    )
    with report_paths.scrubbing_errors():
        return _dispatch_sta(
            requested_corner=requested_corner,
            requested_corners=requested_corners,
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
            clock_period_ns=clock_period_ns,
            input_delay_ns=input_delay_ns,
            output_delay_ns=output_delay_ns,
            input_delays=input_delays,
            output_delays=output_delays,
            input_transition_ns=input_transition_ns,
            input_transitions=input_transitions,
            report_ports=report_ports,
            false_path_from=false_path_from,
            spef_path=spef_path,
            spef_map=spef_map,
            wire_load_model=wire_load_model,
            wire_load_mode=wire_load_mode,
            propagated_clock=propagated_clock,
            write_sdf=write_sdf,
            output_dir=output_dir,
            input_path=input_path,
            basename=basename,
        )


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


def _identity(text: str) -> str:
    return text


def _dispatch_sta(
    *,
    requested_corner: str | None,
    requested_corners: list[str] | None,
    report_paths: _ReportPaths,
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
    input_delays: dict[str, dict[str, float]] | None,
    output_delays: dict[str, dict[str, float]] | None,
    input_transition_ns: float | None,
    input_transitions: dict[str, float] | None,
    report_ports: list[str] | None,
    false_path_from: list[str] | None,
    spef_path: str | None,
    spef_map: dict[str, str] | None,
    wire_load_model: str | None,
    wire_load_mode: str | None,
    propagated_clock: bool,
    output_dir: str,
    input_path: str,
    basename: str,
    write_sdf: bool = False,
) -> dict[str, Any]:
    """Run the single- or multi-corner analysis :func:`run_sta` resolved,
    and assemble its response. Every ``*_path`` argument is an absolute
    engine path; ``report_paths`` carries what the response persists."""
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
            input_delays=input_delays,
            output_delays=output_delays,
            input_transition_ns=input_transition_ns,
            input_transitions=input_transitions,
            report_ports=report_ports,
            false_path_from=false_path_from,
            spef_path=spef_path,
            spef_map=spef_map,
            wire_load_model=wire_load_model,
            wire_load_mode=wire_load_mode,
            propagated_clock=propagated_clock,
            write_sdf=write_sdf,
            output_dir=output_dir,
            input_path=input_path,
            basename=basename,
            report_paths=report_paths,
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
        input_delays=input_delays,
        output_delays=output_delays,
        input_transition_ns=input_transition_ns,
        input_transitions=input_transitions,
        report_ports=report_ports,
        false_path_from=false_path_from,
        spef_path=spef_path,
        wire_load_model=wire_load_model,
        wire_load_mode=wire_load_mode,
        propagated_clock=propagated_clock,
        output_dir=output_dir,
        script_tag=basename,
        scrub=report_paths.scrub,
        sdf_stem=basename if write_sdf else None,
        report_paths=report_paths,
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
        # Issue #2879 (schema 2): `{path, scope}` envelopes, never the
        # absolute engine path -- see `_ReportPaths`.
        "def_path": report_paths.def_path,
        # Additive field (issue #1825): `null` unless `request.verilog` was
        # given -- the mutually-exclusive counterpart to `def_path` above.
        "verilog_path": report_paths.verilog_path,
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
        # Additive field (issue #2739): which clock mode every analyzed
        # corner was timed with -- "ideal" (the default, this command's
        # pre-#2739 behaviour) or "propagated". Present on both response
        # shapes; never infer it from clock_skew_ns, which is a measured
        # metric that is non-zero under ideal timing too.
        "clock_mode": "propagated" if propagated_clock else "ideal",
        "spef_path": report_paths.spef_path,
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
        # Additive field (issue #2780): constraint completeness -- unconstrained
        # endpoints and unclocked registers, with counts and identities. A
        # finite WNS (`timing_status: "constrained"`) only says *some* path
        # was timed; this says whether any endpoint/register was left out.
        "constraint_completeness": corner_fields["constraint_completeness"],
        "setup_violation_count": corner_fields["setup_violation_count"],
        "hold_violation_count": corner_fields["hold_violation_count"],
        "worst_setup_path": corner_fields["worst_setup_path"],
        "worst_hold_path": corner_fields["worst_hold_path"],
        "clock_skew_ns": corner_fields["clock_skew_ns"],
        "estimated_power_mw": corner_fields["estimated_power_mw"],
        "provenance": provenance,
    }

    if "port_paths" in corner_fields:
        response["port_paths"] = corner_fields["port_paths"]
    # Additive field (issue #2968): the applied exclusion list, present only
    # when the request named `constraints.false_path_from`.
    if false_path_from:
        response["false_path_from"] = list(false_path_from)
    response["spef_annotation"] = corner_fields["spef_annotation"]
    # Additive field (issue #2739): the propagated-clock evidence block --
    # what propagation actually reached -- `null` unless the request asked
    # for propagated-clock timing (see `_parse_clock_latency`).
    response["clock_propagation"] = corner_fields["clock_propagation"]
    # Additive field (issue #2886): the post-route SDF this corner's session
    # wrote -- `{path, scope, content_hash}`, `null` unless `request.sdf`.
    response["sdf"] = corner_fields["sdf"]
    # Additive field (issue #3005): see `_electrical_checks_block`.
    response["electrical_checks"] = corner_fields["electrical_checks"]
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
    input_delays: dict[str, dict[str, float]] | None,
    output_delays: dict[str, dict[str, float]] | None,
    input_transition_ns: float | None,
    input_transitions: dict[str, float] | None,
    report_ports: list[str] | None,
    false_path_from: list[str] | None,
    spef_path: str | None,
    wire_load_model: str | None,
    wire_load_mode: str | None,
    propagated_clock: bool,
    output_dir: str,
    script_tag: str,
    scrub: Callable[[str], str] = _identity,
    sdf_stem: str | None = None,
    report_paths: _ReportPaths | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run one complete, fresh OpenSTA session (``read_lef`` x2, ``read_def``
    or ``read_verilog``/``link_design``, ``read_liberty``, ``create_clock``,
    optionally ``read_spef``) at ``corner_name`` (the nominal corner when
    ``None``), exactly like :func:`run_sta`'s single-corner path always has.

    ``propagated_clock`` (issue #2739) marks the request's created clock
    propagated in this session (:func:`_sta_script_lines` emits
    ``set_propagated_clock`` for a ``def``-mode request; it is always
    ``False`` for ``verilog`` mode -- :func:`_validate_propagated_clock`
    rejects the option there) and adds the parsed ``clock_propagation``
    evidence block to ``corner_fields``; ``False`` leaves the emitted Tcl
    and every derived field byte-identical to the pre-#2739 session, with
    ``clock_propagation: None``.

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

    ``sdf_stem`` (issue #2886) -- when given (only ever for a ``def``-mode
    request with a ``spef``; :func:`_validate_sdf` enforces both) -- makes
    this session write ``<output_dir>/<sdf_stem>.<corner>.sdf`` right after
    ``read_spef`` and adds its ``{path, scope, content_hash}`` identity to
    ``corner_fields["sdf"]`` (``report_paths`` supplies the ``{path,
    scope}`` resolution). Any previous file at that path is removed first,
    so a missing or empty file after the run is this session's own failure
    and is raised, never reported. ``None`` leaves the emitted Tcl
    byte-identical to the pre-#2886 session, with ``sdf: None``.

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
            input_delays=input_delays,
            output_delays=output_delays,
            input_transition_ns=input_transition_ns,
            input_transitions=input_transitions,
            report_ports=report_ports,
            false_path_from=false_path_from,
            propagated_clock=propagated_clock,
            sdf_path=sdf_path,
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
            input_delays=input_delays,
            output_delays=output_delays,
            input_transition_ns=input_transition_ns,
            input_transitions=input_transitions,
            report_ports=report_ports,
            false_path_from=false_path_from,
        )
    _write_script(script_path, lines)

    completed = _run_openroad(script_path, metrics_path, error_cls=PostRouteStaError)
    with completed.diagnostics(PostRouteStaError):
        # Issue #2740: a rejected per-port map key stops the session before
        # any timing is reported -- surface that as the actionable request
        # error it is, ahead of the generic engine-failure message.
        port_errors = _parse_port_check_errors(completed.stdout or "")
        if port_errors:
            raise PostRouteStaError(_port_check_error_message(port_errors))
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
    worst_setup_path, worst_hold_path = _worst_paths(
        completed.stdout or "", worst_slack, worst_hold_slack
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
        "timing_status": _timing_status((worst_slack, worst_hold_slack)),
        # Additive field (issue #2780): whether every endpoint is constrained
        # and every register is clocked in this session -- independent of
        # `timing_status`, which only says whether the slack numbers are
        # measurements. See `_parse_constraint_completeness`.
        "constraint_completeness": _parse_constraint_completeness(
            completed.stdout or ""
        ),
        "setup_violation_count": setup_violation_count,
        "hold_violation_count": hold_violation_count,
        # Additive fields (issue #2740): identity of the worst setup-side /
        # hold-side path end this session found -- see `_worst_path_record`.
        "worst_setup_path": worst_setup_path,
        "worst_hold_path": worst_hold_path,
        "clock_skew_ns": round(clock_skew, 5) if clock_skew is not None else None,
        "estimated_power_mw": (
            round(power_w * 1000, 4) if power_w is not None else None
        ),
    }
    # Additive field (issue #3042): only present when the request named
    # `constraints.report_ports`, so an unrequested response is unchanged.
    if report_ports:
        corner_fields["port_paths"] = _port_paths(completed.stdout or "", report_ports)
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
    # Issue #2739: the propagated-clock evidence block -- what propagation
    # actually reached -- parsed from this session's `report_clock_latency`
    # dump; `null` unless the request asked for propagated-clock timing.
    # Additive field (issue #3005): max-transition / max-capacitance checks
    # against this session's parasitics; `null` in netlist mode (no routed
    # geometry to check).
    corner_fields["electrical_checks"] = (
        _electrical_checks_block(completed.stdout or "", spef_path is not None)
        if has_def
        else None
    )
    if propagated_clock and has_def:
        corner_fields["clock_propagation"] = _parse_clock_latency(
            completed.stdout or "", clock_port
        )
    else:
        corner_fields["clock_propagation"] = None
    corner_fields["sdf"] = (
        _sdf_record(sdf_path, report_paths, completed=completed, scrub=scrub)
        if sdf_path is not None
        else None
    )

    resolution = {"corner": corner, "liberty_path": liberty_path, "pdk_info": pdk_info}
    return corner_fields, resolution


def _run_multi_corner(
    corners: Sequence[str | None],
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
    input_delays: dict[str, dict[str, float]] | None,
    output_delays: dict[str, dict[str, float]] | None,
    input_transition_ns: float | None,
    input_transitions: dict[str, float] | None,
    report_ports: list[str] | None,
    false_path_from: list[str] | None,
    spef_path: str | None,
    spef_map: dict[str, str] | None,
    wire_load_model: str | None,
    wire_load_mode: str | None,
    propagated_clock: bool,
    output_dir: str,
    input_path: str,
    basename: str,
    report_paths: _ReportPaths,
    write_sdf: bool = False,
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
    input_hash_before = sha256_file(input_path)

    engine_version = _openroad_version()
    shared_pdk_info: dict[str, Any] | None = None
    corner_entries: list[dict[str, Any]] = []
    spef_axis: list[tuple[str | None, str | None]] = (
        list(spef_map.items()) if spef_map is not None else [(None, spef_path)]
    )
    for corner_name, (interconnect, pair_spef) in (
        (c, s) for c in corners for s in spef_axis
    ):
        script_tag = f"{basename}__{corner_name or 'nominal'}"
        if interconnect is not None:
            script_tag += f"__{interconnect}"
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
            input_delays=input_delays,
            output_delays=output_delays,
            input_transition_ns=input_transition_ns,
            input_transitions=input_transitions,
            report_ports=report_ports,
            false_path_from=false_path_from,
            spef_path=pair_spef,
            wire_load_model=wire_load_model,
            wire_load_mode=wire_load_mode,
            propagated_clock=propagated_clock,
            output_dir=output_dir,
            script_tag=script_tag,
            scrub=report_paths.scrub,
            sdf_stem=basename if write_sdf else None,
            report_paths=report_paths,
        )
        if shared_pdk_info is None:
            shared_pdk_info = resolution["pdk_info"]
        deck_name = f"{cell_library}__{resolution['corner']}"
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
                "deck": _deck_block(deck_name, resolution["liberty_path"]),
            }
        )

    input_hash_after = sha256_file(input_path)
    if input_hash_after != input_hash_before:
        raise PostRouteStaError(
            "the geometry backing this multi-corner characterization "
            f"({input_path}) changed while characterizing corners "
            + ", ".join(c or "nominal" for c in corners)
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

    response: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "engine": "openroad",
        "engine_version": engine_version,
        "hdl_toplevel": hdl_toplevel,
        "status": "ok",
        "def_path": report_paths.def_path,
        "verilog_path": report_paths.verilog_path,
        "geometry_source": geometry_source,
        "wire_load_model": wire_load_model,
        "wire_load_mode": wire_load_mode,
        # Additive field (issue #2739): one clock mode for the whole
        # characterization -- every per-corner session runs the same
        # request-level `constraints.propagated_clock`, so this is hoisted
        # like the other corner-invariant request echoes. Each `corners[]`
        # entry carries that session's own `clock_propagation` evidence
        # block (the network latencies it reports *do* vary per corner).
        "clock_mode": "propagated" if propagated_clock else "ideal",
        "spef_path": report_paths.spef_path,
        "provenance": provenance,
        # Additive field (issue #1871): one entry per requested
        # `request.pdk.corners` name, in the same order the request gave
        # them -- see this function's own docstring for the exact shape.
        "corners": corner_entries,
    }
    # Additive field (issue #2968): corner-invariant request echo, present only
    # when the request named `constraints.false_path_from`.
    if false_path_from:
        response["false_path_from"] = list(false_path_from)
    return response


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
    reader_warning_count, reader_warning_sample = _spef_reader_warnings(stdout, stderr)
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
        "reader_warning_count": reader_warning_count,
        "reader_warning_sample": reader_warning_sample,
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


_INTERCONNECT_CORNERS = ("min", "nom", "max")


def _resolve_spef_map(spef_field: dict[str, Any], request_dir: str) -> dict[str, str]:
    """Issue #3228: validate map-form ``request.spef`` and return the engine
    paths keyed by interconnect corner in canonical min/nom/max order."""
    if not spef_field:
        raise PostRouteStaError(
            "request.spef must be a non-empty object when given as a map"
        )
    unknown = [key for key in spef_field if key not in _INTERCONNECT_CORNERS]
    if unknown:
        raise PostRouteStaError(
            f"request.spef has unknown interconnect corner '{unknown[0]}' "
            f"-- allowed keys are {', '.join(_INTERCONNECT_CORNERS)}"
        )
    resolved: dict[str, str] = {}
    for key in _INTERCONNECT_CORNERS:
        if key not in spef_field:
            continue
        value = spef_field[key]
        if not isinstance(value, str) or not value:
            raise PostRouteStaError(
                f"request.spef.{key} must be a non-empty string when given"
            )
        path = value if os.path.isabs(value) else os.path.join(request_dir, value)
        if not os.path.isfile(path):
            raise PostRouteStaError(f"spef not found for request.spef.{key}: {value}")
        resolved[key] = os.path.abspath(path)
    return resolved


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


def _validate_transition_value(value: Any, field: str) -> float | None:
    """One input-transition value: ``None`` when omitted, else a finite,
    non-boolean number ``>= 0`` (``0`` is an ideal edge)."""
    if value is None:
        return None
    if not _is_finite_number(value) or value < 0:
        raise PostRouteStaError(f"{field} must be a finite non-negative number")
    return float(value)


def _is_finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value == value  # NaN
        and value not in (float("inf"), float("-inf"))
    )


def _validate_port_delay_maps(
    constraints: dict[str, Any], clock_port: str
) -> tuple[dict[str, dict[str, float]] | None, dict[str, dict[str, float]] | None]:
    """``constraints.input_delays``/``.output_delays`` (issue #2740):
    optional per-port, per-bound I/O delay maps. Returns ``(input_delays,
    output_delays)``, each ``None`` when the field is omitted *or* empty
    (an empty map constrains nothing, so it reproduces the scalar-only
    generated Tcl byte-for-byte).

    Each map is ``{"<literal port name>": {"min_ns": n, "max_ns": n}}``
    with both bounds independently optional but at least one required. A
    supplied bound is never copied to the other side: an entry with only
    ``min_ns`` constrains only the min (hold-side) arrival of that port, and
    its max side stays unconstrained -- the scalar default does **not**
    fill it in, because an explicit entry replaces the scalar default for
    that port entirely.

    Unlike the legacy scalars (non-negative), per-port bounds may be
    negative (a boundary reference point can legitimately sit after the
    launching edge); they must still be finite, non-boolean numbers, and
    ``min_ns <= max_ns`` when both are given.

    Keys are literal port (or single bus-bit) names -- never Tcl, never a
    wildcard pattern. Existence and direction can only be checked against
    the loaded design, so they are verified inside the engine session
    (:func:`_port_check_lines`); the clock port is rejected here, since
    ``constraints.clock_port`` already names it.
    """
    return (
        _validate_port_delay_map(constraints, "input", clock_port),
        _validate_port_delay_map(constraints, "output", clock_port),
    )


def _validate_port_delay_map(
    constraints: dict[str, Any], which: str, clock_port: str
) -> dict[str, dict[str, float]] | None:
    """Validate one of ``constraints.{input,output}_delays`` (see
    :func:`_validate_port_delay_maps`)."""
    field = f"request.constraints.{which}_delays"
    value = constraints.get(f"{which}_delays")
    if value is None:
        return None
    if not isinstance(value, dict):
        raise PostRouteStaError(
            f"{field} must be a JSON object mapping port names to "
            '{"min_ns": ..., "max_ns": ...} entries'
        )
    parsed: dict[str, dict[str, float]] = {}
    for port, entry in value.items():
        _check_port_delay_key(field, which, port, clock_port)
        parsed[port] = _parse_port_delay_entry(field, port, entry)
    return parsed or None


def _check_port_delay_key(field: str, which: str, port: str, clock_port: str) -> None:
    """Reject a malformed port-name key of an I/O delay map."""
    if not port or port != port.strip():
        raise PostRouteStaError(
            f"{field} keys must be non-empty port names without "
            f"leading/trailing whitespace (got {port!r})"
        )
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in port):
        raise PostRouteStaError(f"{field} key {port!r} contains a control character")
    if which == "input" and port == clock_port:
        raise PostRouteStaError(
            f"{field} must not name the clock port {port!r} -- "
            "clock inputs are excluded from input delays"
        )


def _parse_port_delay_entry(field: str, port: str, entry: Any) -> dict[str, float]:
    """Validate one ``{"min_ns": n, "max_ns": n}`` entry of an I/O delay map."""
    if not isinstance(entry, dict):
        raise PostRouteStaError(
            f"{field}[{port!r}] must be a JSON object with 'min_ns' and/or 'max_ns'"
        )
    unknown = sorted(set(entry) - {"min_ns", "max_ns"})
    if unknown:
        raise PostRouteStaError(
            f"{field}[{port!r}] has unknown key(s) "
            + ", ".join(repr(k) for k in unknown)
            + " -- only 'min_ns' and 'max_ns' are accepted"
        )
    if not entry:
        raise PostRouteStaError(
            f"{field}[{port!r}] is empty -- give 'min_ns', 'max_ns', or both"
        )
    bounds: dict[str, float] = {}
    for bound in ("min_ns", "max_ns"):
        if bound not in entry:
            continue
        bound_value = entry[bound]
        if not _is_finite_number(bound_value):
            raise PostRouteStaError(
                f"{field}[{port!r}].{bound} must be a finite number "
                f"(got {bound_value!r})"
            )
        bounds[bound] = float(bound_value)
    if (
        "min_ns" in bounds
        and "max_ns" in bounds
        and bounds["min_ns"] > bounds["max_ns"]
    ):
        raise PostRouteStaError(
            f"{field}[{port!r}].min_ns ({bounds['min_ns']}) must not "
            f"exceed max_ns ({bounds['max_ns']})"
        )
    return bounds


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
    field = "request.constraints.report_ports"
    value = constraints.get("report_ports")
    if value is None:
        return None
    if not isinstance(value, list):
        raise PostRouteStaError(f"{field} must be a list of port names")
    seen: set[str] = set()
    for port in value:
        if not isinstance(port, str) or not port or port != port.strip():
            raise PostRouteStaError(
                f"{field} entries must be non-empty port-name strings without "
                f"leading/trailing whitespace (got {port!r})"
            )
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in port):
            raise PostRouteStaError(
                f"{field} entry {port!r} contains a control character"
            )
        if port == clock_port:
            raise PostRouteStaError(
                f"{field} must not name the clock port {port!r} -- "
                "it has no data path of its own"
            )
        if port in seen:
            raise PostRouteStaError(f"{field} must not repeat a port ({port!r})")
        seen.add(port)
    return list(value) or None


def _validate_false_path_from(
    constraints: dict[str, Any], clock_port: str
) -> list[str] | None:
    """``constraints.false_path_from`` (issue #2968): optional list of unique
    literal input-port names (one scalar port or one bus bit, e.g.
    ``cfg[3]`` -- the same key rules as :func:`_validate_report_ports`) whose
    paths are excluded from timing with ``set_false_path -from``. Returns the
    list, or ``None`` when the field is omitted *or* empty (nothing is
    emitted, so the generated Tcl is byte-identical to a request without
    it). Existence and direction are verified inside the engine session
    (:func:`_port_check_lines`); the clock port is rejected here."""
    field = "request.constraints.false_path_from"
    value = constraints.get("false_path_from")
    if value is None:
        return None
    if not isinstance(value, list):
        raise PostRouteStaError(f"{field} must be a list of input port names")
    seen: set[str] = set()
    for port in value:
        if not isinstance(port, str) or not port or port != port.strip():
            raise PostRouteStaError(
                f"{field} entries must be non-empty port-name strings without "
                f"leading/trailing whitespace (got {port!r})"
            )
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in port):
            raise PostRouteStaError(
                f"{field} entry {port!r} contains a control character"
            )
        if port == clock_port:
            raise PostRouteStaError(
                f"{field} must not name the clock port {port!r} -- "
                "a false path from the clock would untime every register"
            )
        if port in seen:
            raise PostRouteStaError(f"{field} must not repeat a port ({port!r})")
        seen.add(port)
    return list(value) or None


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


def _validate_propagated_clock(constraints: dict[str, Any], has_def: bool) -> bool:
    """Validate ``constraints.propagated_clock`` (issue #2739), returning the
    validated boolean (``False`` when omitted or ``null`` -- the ideal-clock
    default this command has always run, byte-for-byte).

    Only a JSON boolean is accepted; every other non-null value is a request
    error. ``True`` is **def-mode only**: a ``verilog``-mode request links a
    structural netlist with no placement at all, so there is no placed clock
    tree to propagate through and the option is rejected with an actionable
    error rather than silently ignored.

    Meaningful is not the same as *correct* for a ``def``-mode request: the
    option is only worth what the DEF's clock tree is worth (a pre-CTS DEF
    propagates through plain wires, arriving with ~no insertion delay), and
    this module cannot infer tree presence from ``geometry_source`` or from
    a non-null ``clock_skew_ns`` -- the response's ``clock_propagation``
    evidence block is what says what propagation actually reached.

    ``constraints`` is already known to be a JSON object by the time this is
    called -- :func:`_validate_constraints` (always called first) raises
    otherwise.
    """
    value = constraints.get("propagated_clock")
    if value is None:
        return False
    if not isinstance(value, bool):
        raise PostRouteStaError(
            "constraints.propagated_clock must be a boolean (true enables "
            "propagated-clock timing in def mode; omit or false keeps the "
            "ideal-clock default)"
        )
    if value and not has_def:
        raise PostRouteStaError(
            "constraints.propagated_clock is only valid in def mode -- a "
            "verilog-mode request links a netlist with no placed clock tree "
            "to propagate through"
        )
    return value


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
    completed: _OpenRoadResult,
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


def _propagated_clock_lines(clock_port: str) -> list[str]:
    """``set_propagated_clock`` (issue #2739) on exactly the clock object
    :func:`_clock_lines` created -- ``[get_clocks <clock_port>]``, not
    ``[all_clocks]``: this session has exactly one clock (the one this
    command itself defines), and naming it keeps the command honest if a
    future session shape ever loads more than one. Emitted immediately
    after ``create_clock`` and before any I/O delay / SPEF / metric
    collection, so every report the session produces is the propagated
    analysis. The clock-port spelling matches :func:`_clock_lines`'s own
    (plain interpolation -- the same string ``create_clock -name`` used)."""
    return [f"set_propagated_clock [get_clocks {clock_port}]"]


def _clock_latency_lines() -> list[str]:
    """Marker-delimited ``report_clock_latency -digits 6`` (issue #2739) --
    the machine-readable evidence of what propagation actually reached: one
    ``Clock <name>`` section per clock, ``network latency <pin>`` rows per
    clock sink. Emitted after any ``read_spef`` and before the timing
    metrics, so the latencies it reports reflect the same parasitics the
    timing numbers do. A clock whose network reaches no clock pin prints
    the header and nothing else (:func:`_parse_clock_latency` -> ``sinks:
    0``). Wrapped in ``catch`` so an OpenSTA build without the command
    degrades the evidence to ``unknown`` rather than aborting the run (the
    same degradation :func:`_parasitic_annotation_lines` established)."""
    return [
        f'puts "{_CLOCK_LATENCY_BEGIN}"',
        "catch {report_clock_latency -digits 6} klt_clock_latency_msg",
        f'puts "{_CLOCK_LATENCY_END}"',
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


_TCL_SPECIAL = frozenset(' \t\r\n\\"$[]{};#')


def _tcl_literal(text: str) -> str:
    """Backslash-quote ``text`` into one literal Tcl word: no word
    splitting, no ``$``/``[...]``/backslash substitution, no brace
    matching. Control characters never reach here
    (:func:`_validate_port_delay_maps` rejects them), so only the
    Tcl-special printable characters need escaping. Same idiom as
    ``lvs_netgen.py``'s ``_tcl_word``, kept module-local per this module's
    self-contained convention."""
    return "".join("\\" + ch if ch in _TCL_SPECIAL else ch for ch in text)


def _request_io_delay_lines(
    clock_port: str,
    input_delay_ns: float | None,
    output_delay_ns: float | None,
    input_delays: dict[str, dict[str, float]] | None,
    output_delays: dict[str, dict[str, float]] | None,
    input_transition_ns: float | None = None,
    input_transitions: dict[str, float] | None = None,
    report_ports: list[str] | None = None,
    false_path_from: list[str] | None = None,
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
    ):
        delay_lines = (
            _port_check_lines(
                input_delays,
                output_delays,
                input_transitions,
                report_ports,
                false_path_from,
            )
            + delay_lines
        )
    return (
        delay_lines
        + _input_transition_lines(clock_port, input_transition_ns, input_transitions)
        + _false_path_lines(false_path_from)
    )


def _false_path_lines(false_path_from: list[str] | None) -> list[str]:
    """``set_false_path -from`` per ``constraints.false_path_from`` port
    (issue #2968), emitted after the I/O-delay lines. Nothing when unset."""
    return [
        f"set_false_path -from [get_ports {_tcl_literal(port)}]"
        for port in false_path_from or ()
    ]


def _port_check_lines(
    input_delays: dict[str, dict[str, float]] | None,
    output_delays: dict[str, dict[str, float]] | None,
    input_transitions: dict[str, float] | None = None,
    report_ports: list[str] | None = None,
    false_path_from: list[str] | None = None,
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
        ("report", dict.fromkeys(report_ports or ()), _ANY_PORT_DIRECTIONS),
        (
            "false_path",
            dict.fromkeys(false_path_from or ()),
            _INPUT_PORT_DIRECTIONS,
        ),
    ):
        for port in mapping or {}:
            entries.append(f"{side} {_tcl_literal(port)} {{{' '.join(allowed)}}}")
    return [
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
        f'puts "{_PORT_CHECK_BEGIN}"',
        "foreach klt_err $klt_port_errors {",
        '    puts "[lindex $klt_err 0]\\t[lindex $klt_err 1]\\t[lindex $klt_err 2]"',
        "}",
        f'puts "{_PORT_CHECK_END}"',
        "if {[llength $klt_port_errors] > 0} {",
        f"    exit {_PORT_CHECK_EXIT_CODE}",
        "}",
    ]


def _port_io_delay_lines(
    clock_port: str,
    input_delay_ns: float | None,
    output_delay_ns: float | None,
    input_delays: dict[str, dict[str, float]] | None,
    output_delays: dict[str, dict[str, float]] | None,
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
                    f"set klt_clock_port [get_ports {clock_port}]",
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
                f"    {command} {scalar} -clock {clock_port} $klt_{side}_default_ports",
                "}",
            ]
        for port, bounds in mapping.items():
            for bound, flag in (("min_ns", "-min"), ("max_ns", "-max")):
                if bound in bounds:
                    lines.append(
                        f"{command} {flag} {bounds[bound]!r} -clock {clock_port} "
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
            f"set klt_transition_clock_port [get_ports {clock_port}]",
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


def _worst_path_lines() -> list[str]:
    """Per-path-group worst setup (``max``) and hold (``min``) path ends,
    marker-delimited (issue #2740).

    ``find_timing_paths -group_path_count 1 -endpoint_path_count 1`` returns
    the single worst constrained path end of every path group (data clock
    groups, ``asynchronous``, ``gated clock``, ...); every constrained path
    end belongs to exactly one group, so the minimum over these records is
    the same population ``report_worst_slack_metric`` reduces over --
    :func:`_worst_path_record` re-checks that equality per run rather than
    assuming it. ``check_role`` is OpenSTA's own name for the check at the
    endpoint (``setup``, ``recovery``, ``clock gating hold``, ...), so
    recovery/removal are never relabelled as data setup/hold. Output is
    bounded by the number of path groups, not design size. The whole query
    is ``catch``-wrapped: an engine without these commands yields an
    ``ERROR`` row (reported as ``status: "unavailable"``), never a fabricated
    path."""
    return [
        f'puts "{_WORST_PATHS_BEGIN}"',
        "if {[catch {",
        "    foreach klt_path_side {max min} {",
        "        foreach klt_pe [find_timing_paths -path_delay $klt_path_side "
        "-group_path_count 1 -endpoint_path_count 1] {",
        '            puts "PATH\\t$klt_path_side\\t[$klt_pe check_role]\\t'
        "[$klt_pe slack]\\t[get_full_name [get_property $klt_pe startpoint]]\\t"
        '[get_full_name [get_property $klt_pe endpoint]]"',
        "        }",
        "    }",
        "} klt_worst_path_error]} {",
        '    puts "ERROR\\t[string map [list \\n { } \\t { }] $klt_worst_path_error]"',
        "}",
        f'puts "{_WORST_PATHS_END}"',
    ]


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
        "catch {",
        "  foreach klt_pin [get_pins -hierarchical *] {",
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
        "  }",
        "} klt_driver_nets_msg",
        f'puts "{_DRIVER_NETS_END}"',
    ]


def _violation_count_lines() -> list[str]:
    """Marker-delimited setup/hold violator reports for this module's
    standalone ``klt sta`` session (single corner and each
    ``corners[]`` entry -- every per-corner session calls this).

    Delegates the check-family policy to
    :func:`~klayout_tools._openroad_engine._timing_violation_report_lines`
    (issue #2741): the setup side counts data setup + recovery + clock-gating
    setup and the hold side data hold + removal + clock-gating hold -- the
    same populations ``report_worst_slack_metric``/``report_tns_metric``
    measure -- so ``setup_violation_count``/``hold_violation_count`` agree
    with the WNS/TNS fields next to them. Unit: violating endpoints (one
    ``-format end`` row per endpoint per path group), not enumerated paths.
    """
    return _timing_violation_report_lines(
        setup_begin=_SETUP_VIOLATIONS_BEGIN,
        setup_end=_SETUP_VIOLATIONS_END,
        hold_begin=_HOLD_VIOLATIONS_BEGIN,
        hold_end=_HOLD_VIOLATIONS_END,
    )


def _electrical_check_lines() -> list[str]:
    """Marker-delimited max-transition / max-capacitance violator reports
    (issue #3005), run in the same session that already did ``read_spef`` so
    the numbers are against the extracted parasitics when a ``spef`` was
    given.

    Units are pinned with ``set_cmd_units`` (``ns`` / ``pF``) so the report
    is in the units the response states regardless of the Liberty's own.
    Each report runs through ``sta::redirect_string_begin``/``_end`` inside a
    ``catch``: a clean design prints nothing from ``report_check_types``, so
    the explicit ``KLT_ELECTRICAL_OK`` sentinel is what distinguishes
    "checked, zero violations" from a command that never ran
    (``KLT_ELECTRICAL_UNSUPPORTED`` when the ``catch`` fired; a missing
    block is ``not_run``). For every violating pin a ``KLT_ELECTRICAL_NET``
    row maps it to its net (ports are their own net) so distinct nets can be
    counted."""
    lines = ["set_cmd_units -time ns -capacitance pF"]
    for flag, begin, end in (
        ("-max_slew", _MAX_SLEW_BEGIN, _MAX_SLEW_END),
        ("-max_capacitance", _MAX_CAP_BEGIN, _MAX_CAP_END),
    ):
        lines += [
            f'puts "{begin}"',
            "set klt_elec_rep {}",
            "if {[catch {",
            "  sta::redirect_string_begin",
            f"  report_check_types {flag} -violators -digits 5",
            "  set klt_elec_rep [sta::redirect_string_end]",
            "} klt_elec_msg]} {",
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
    input_delays: dict[str, dict[str, float]] | None = None,
    output_delays: dict[str, dict[str, float]] | None = None,
    input_transition_ns: float | None = None,
    input_transitions: dict[str, float] | None = None,
    report_ports: list[str] | None = None,
    false_path_from: list[str] | None = None,
    propagated_clock: bool = False,
    sdf_path: str | None = None,
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

    ``propagated_clock`` (issue #2739) emits ``set_propagated_clock`` on the
    clock ``_clock_lines`` just created -- after the design and the timing
    constraint are loaded, before any I/O delay / SPEF / metric collection,
    so every report this session produces (including the SPEF delay
    fingerprints and the timing metrics) is the propagated analysis. The
    evidence dump (:func:`_clock_latency_lines`) runs *after* ``read_spef``
    so its network latencies reflect the same parasitics the timing
    numbers do.

    ``sdf_path`` (issue #2886; requires ``spef_path``) appends the shared
    ``write_sdf`` command (:func:`~klayout_tools._openroad_engine.write_sdf_line`)
    immediately after ``read_spef`` -- outside the reader-warning markers,
    so SDF-writer diagnostics are never counted as SPEF reader warnings,
    and before every report command, the same ordering ``klt
    place-and-route``'s ``post_route_sdf`` uses.
    """
    if sdf_path is not None and spef_path is None:
        raise ValueError("sdf_path requires spef_path")
    lines = [
        f"read_lef {tech_lef}",
        f"read_lef {cell_lef}",
        f"read_def {def_path}",
        f"read_liberty {liberty_path}",
    ]
    lines += _clock_lines(clock_port, clock_period_ns)
    if propagated_clock:
        lines += _propagated_clock_lines(clock_port)
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
    )
    if spef_path is not None:
        lines += _spef_net_check_lines(spef_net_names or [])
        lines += _delay_fingerprint_lines(_DELAY_PRE_BEGIN, _DELAY_PRE_END)
        lines.append(f'puts "{_SPEF_READ_BEGIN}"')
        lines.append(f"read_spef {spef_path}")
        lines.append(f'puts "{_SPEF_READ_END}"')
        if sdf_path is not None:
            lines.append(write_sdf_line(sdf_path))
        lines += _delay_fingerprint_lines(_DELAY_POST_BEGIN, _DELAY_POST_END)
        lines += _parasitic_annotation_lines()
    if propagated_clock:
        lines += _clock_latency_lines()
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
    lines += _worst_path_lines()
    lines += _port_path_lines(report_ports)
    # Before the completeness query, which must stay the script's last
    # statements (issue #2780): it can take the session down.
    lines += _electrical_check_lines()
    lines += _constraint_completeness_lines()
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
    input_delays: dict[str, dict[str, float]] | None = None,
    output_delays: dict[str, dict[str, float]] | None = None,
    input_transition_ns: float | None = None,
    input_transitions: dict[str, float] | None = None,
    report_ports: list[str] | None = None,
    false_path_from: list[str] | None = None,
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
    )
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
    lines += _worst_path_lines()
    lines += _port_path_lines(report_ports)
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
    here -- this verb never runs TritonRoute).

    :func:`~klayout_tools._openroad_engine._container_runtime_hint` is
    appended as a further ``--`` clause when the captured output shows the
    engine never really ran at all because its container runtime was
    unreachable (issue #2632) -- the same append-a-recognized-hint structure
    ``place_and_route.py`` already uses for ``_mount_namespace_hint`` (issue
    #1868). A failure carrying neither signature keeps exactly its previous
    message."""
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
        message = f"openroad sta run failed: {bracket_lines[0]}"
    elif bare_error_lines:
        message = f"openroad sta run failed: {bare_error_lines[-1]}"
    else:
        tail_source = (completed.stderr or completed.stdout or "").strip().splitlines()
        snippet = " ".join(tail_source[-3:]) if tail_source else "no output captured"
        message = f"openroad sta run exited with code {completed.returncode}: {snippet}"

    hint = _container_runtime_hint(completed)
    if hint is not None:
        message = f"{message} -- {hint}"
    return message


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


def _parse_clock_latency(stdout: str, clock_name: str) -> dict[str, Any]:
    """The ``clock_propagation`` evidence block (issue #2739), parsed from
    one session's :data:`_CLOCK_LATENCY_BEGIN`/:data:`_CLOCK_LATENCY_END`
    ``report_clock_latency`` dump for the request's clock:

    - ``clock_sinks`` -- distinct clock pins the clock network reaches
      (deduplicated across the per-transition rise/fall tables OpenSTA
      prints). ``0`` is the explicit "propagation could not be established"
      signal: the section header printed but no ``network latency`` row
      under it. ``None`` means unknown -- the clock's section never appeared
      (an OpenSTA build whose ``catch``-wrapped ``report_clock_latency``
      produced nothing, or a marker-less stdout from an older stub/capture).
    - ``max_network_latency_ns`` -- the worst ``network latency`` value the
      section reports (the max-column continuation values included);
      ``None`` when there are no sinks (or the section is absent).
    - ``clock_sinks_sample`` -- the sink names in report order, capped at
      :data:`_CLOCK_SINK_SAMPLE_LIMIT` (a diagnostic sample, mirroring the
      SPEF missing-nets sample, not an exhaustive list).

    A request clock whose section is absent is ``unknown``, never ``0``:
    ``create_clock`` already failed the session for a missing clock port,
    so absence here means the evidence command itself did not run -- not
    that the tree is empty.
    """
    block = _extract_block(stdout, _CLOCK_LATENCY_BEGIN, _CLOCK_LATENCY_END)
    section_seen, rows = (
        _clock_latency_rows(block, clock_name) if block is not None else (False, [])
    )
    seen: set[str] = set()
    sample: list[str] = []
    for _latency_s, pin in rows:
        if pin not in seen:
            seen.add(pin)
            if len(sample) < _CLOCK_SINK_SAMPLE_LIMIT:
                sample.append(pin)
    max_latency = _max_network_latency(rows)
    return {
        "clock_sinks": len(seen) if section_seen else None,
        "max_network_latency_ns": (
            round(max_latency, 6) if max_latency is not None else None
        ),
        "clock_sinks_sample": sample,
    }


def _clock_latency_rows(
    block: str, clock_name: str
) -> tuple[bool, list[tuple[str, str]]]:
    """``(section_seen, rows)`` for *clock_name* in a ``report_clock_latency``
    block: whether the clock's section header appeared at all, and the
    ``(latency, pin)`` string pairs of every ``network latency`` row under
    it, in report order (duplicates across rise/fall tables kept)."""
    rows: list[tuple[str, str]] = []
    section_seen = False
    current_section: str | None = None
    for line in block.splitlines():
        header = _CLOCK_SECTION_RE.match(line.rstrip())
        if header:
            current_section = header.group(1)
            section_seen = section_seen or current_section == clock_name
            continue
        if current_section != clock_name:
            continue
        row = _NETWORK_LATENCY_RE.match(line.rstrip())
        if row is not None:
            latency_s, pin = row.groups()
            rows.append((latency_s, pin))
    return section_seen, rows


def _max_network_latency(rows: list[tuple[str, str]]) -> float | None:
    """The largest parseable latency among *rows* (see
    :func:`_clock_latency_rows`), or ``None`` when none parses."""
    max_latency: float | None = None
    for latency_s, _pin in rows:
        try:
            latency = float(latency_s)
        except ValueError:
            continue
        if max_latency is None or latency > max_latency:
            max_latency = latency
    return max_latency


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


def _electrical_class(
    coverage: str, unit: str, rows: list[tuple[str, float, float]], nets: set[str]
) -> dict[str, Any]:
    """One limit class of the ``electrical_checks`` block. Counts, excess and
    pin are ``null`` unless ``coverage == "checked"``."""
    checked = coverage == "checked"
    worst = max(rows, key=lambda row: row[2], default=None) if checked else None
    return {
        "coverage": coverage,
        "limit_source": "liberty",
        "unit": unit,
        "violation_count": len(rows) if checked else None,
        "net_count": len(nets) if checked else None,
        "worst_excess": round(worst[2], 5) if worst is not None else None,
        "worst_pin": worst[0] if worst is not None else None,
    }


def _parse_electrical_check(
    stdout: str, begin: str, end: str, unit: str
) -> dict[str, Any]:
    """Parse one ``report_check_types -violators`` block (issue #3005).

    ``coverage`` is ``"not_run"`` when the markers are absent (the session
    never reached the block), ``"unsupported"`` when the Tcl ``catch`` fired,
    and ``"checked"`` only when the ``KLT_ELECTRICAL_OK`` sentinel closed the
    block -- so zero violations is only ever reported for a check that ran.
    ``worst_excess`` is ``value - limit`` of the worst violator (positive)."""
    block = _extract_block(stdout, begin, end)
    if block is None:
        return _electrical_class("not_run", unit, [], set())
    if _ELECTRICAL_UNSUPPORTED in block or _ELECTRICAL_OK not in block:
        return _electrical_class("unsupported", unit, [], set())
    rows: list[tuple[str, float, float]] = []
    nets: set[str] = set()
    for line in block.splitlines():
        if line.startswith(_ELECTRICAL_NET_PREFIX + " "):
            parts = line.split(None, 2)
            if len(parts) == 3:
                nets.add(parts[2].strip())
            continue
        match = _ELECTRICAL_ROW_RE.match(line)
        if match:
            excess = float(match.group("value")) - float(match.group("limit"))
            rows.append((match.group("pin"), float(match.group("limit")), excess))
    return _electrical_class("checked", unit, rows, nets)


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


_PORT_CHECK_PREFIXES: dict[str | None, str] = {
    "report": "constraints.report_ports rejected",
    "false_path": "constraints.false_path_from rejected",
    "input_transition": "constraints.input_transitions rejected",
}


def _port_check_error_message(rows: list[tuple[str, str, str]]) -> str:
    details = []
    for side, port, problem in rows:
        if side == "input_transition":
            field = f"constraints.input_transitions[{port!r}]"
            side = "input"
        elif side == "report":
            field = f"constraints.report_ports[{port!r}]"
        elif side == "false_path":
            field = f"constraints.false_path_from[{port!r}]"
        else:
            field = f"constraints.{side}_delays[{port!r}]"
        if problem == "not_found":
            details.append(f"{field}: no port with exactly this name in the design")
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


def _empty_path_record(status: str, reason: str | None) -> dict[str, Any]:
    return {
        "status": status,
        "check_type": None,
        "engine_check_role": None,
        "startpoint": None,
        "endpoint": None,
        "slack_ns": None,
        "consistent_with_aggregate": None,
        "reason": reason,
    }


def _parse_worst_path_rows(
    stdout: str,
) -> tuple[dict[str, list[dict[str, Any]]] | None, str | None]:
    """``({"max": [...], "min": [...]}, None)`` from
    :func:`_worst_path_lines`' block, or ``(None, reason)`` when the block is
    absent, reports an engine error, or carries a malformed ``PATH`` row.
    Lines that are neither ``PATH`` nor ``ERROR`` rows (stray engine
    warnings) are ignored -- they can never be mistaken for a path."""
    block = _extract_block(stdout, _WORST_PATHS_BEGIN, _WORST_PATHS_END)
    if block is None:
        return None, "worst-path records absent from engine output"
    rows: dict[str, list[dict[str, Any]]] = {"max": [], "min": []}
    for line in block.splitlines():
        if line.startswith("ERROR\t"):
            return None, (
                "engine path introspection failed: " + line[len("ERROR\t") :].strip()
            )
        if not line.startswith("PATH\t"):
            continue
        parts = line.split("\t")
        if len(parts) != 6 or parts[1] not in rows or not all(parts[2:]):
            return None, f"malformed worst-path record: {line!r}"
        try:
            slack_s = float(parts[3])
        except ValueError:
            return None, f"malformed worst-path slack: {line!r}"
        if slack_s != slack_s or abs(slack_s) == float("inf"):
            return None, f"non-finite worst-path slack: {line!r}"
        rows[parts[1]].append(
            {
                "engine_check_role": parts[2],
                "slack_ns": slack_s * 1e9,
                "startpoint": parts[4],
                "endpoint": parts[5],
            }
        )
    return rows, None


def _worst_path_record(
    rows: list[dict[str, Any]], aggregate_ns: float | None
) -> dict[str, Any]:
    """The worst (minimum-slack) record among one side's per-group rows,
    cross-checked against that side's aggregate WNS from the same session.

    ``status``:

    - ``"ok"`` -- a path was found and its slack equals the aggregate WNS
      (within :data:`_SLACK_CONSISTENCY_TOL_NS`), or the aggregate was not
      reported at all (``consistent_with_aggregate: null``).
    - ``"no_paths"`` -- the engine found no constrained path on this side,
      and the aggregate agrees (sentinel or absent). Identity fields null.
    - ``"inconsistent"`` -- the path population and the aggregate WNS
      disagree (no path but a real WNS, or a different slack). The record
      is still the engine's real worst *found* path, but it must not be
      read as the path that binds the aggregate.
    """
    aggregate_real = aggregate_ns is not None and abs(aggregate_ns) < 1e29
    if not rows:
        if aggregate_real:
            record = _empty_path_record(
                "inconsistent",
                "engine reported no constrained path but an aggregate WNS of "
                f"{aggregate_ns}",
            )
            record["consistent_with_aggregate"] = False
            return record
        return _empty_path_record("no_paths", None)
    worst = min(rows, key=lambda row: row["slack_ns"])
    role = worst["engine_check_role"]
    record = {
        "status": "ok",
        "check_type": _CHECK_ROLE_TYPES.get(role, "unknown"),
        "engine_check_role": role,
        "startpoint": worst["startpoint"],
        "endpoint": worst["endpoint"],
        "slack_ns": round(worst["slack_ns"], 5),
        "consistent_with_aggregate": None,
        "reason": None,
    }
    if aggregate_ns is None:
        return record
    consistent = aggregate_real and (
        abs(worst["slack_ns"] - aggregate_ns) <= _SLACK_CONSISTENCY_TOL_NS
    )
    record["consistent_with_aggregate"] = consistent
    if not consistent:
        record["status"] = "inconsistent"
        record["reason"] = (
            f"worst path slack {record['slack_ns']} ns differs from the "
            f"aggregate WNS {aggregate_ns}"
        )
    return record


def _worst_paths(
    stdout: str, setup_ws: float | None, hold_ws: float | None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(worst_setup_path, worst_hold_path)`` for one session."""
    rows, reason = _parse_worst_path_rows(stdout)
    if rows is None:
        return (
            _empty_path_record("unavailable", reason),
            _empty_path_record("unavailable", reason),
        )
    return (
        _worst_path_record(rows["max"], setup_ws),
        _worst_path_record(rows["min"], hold_ws),
    )


def _apply_port_record(
    parts: list[str],
    line: str,
    directions: dict[str, str],
    port_rows: dict[str, list[dict[str, Any]]],
) -> str | None:
    """Fold one ``DIR``/``PATH``/``ERROR`` record into ``directions`` /
    ``port_rows``; return a failure reason when the record is bad, else
    ``None``."""
    kind, port = parts[0], parts[1]
    if kind == "ERROR":
        return "engine path introspection failed: " + "\t".join(parts[2:]).strip()
    if kind == "DIR":
        if len(parts) != 3 or not parts[2]:
            return f"malformed port record: {line!r}"
        directions[port] = "inout" if parts[2] == "bidirect" else parts[2]
        return None
    if len(parts) != 7 or parts[2] not in ("max", "min") or not all(parts[3:]):
        return f"malformed port path record: {line!r}"
    try:
        slack_s = float(parts[4])
    except ValueError:
        return f"malformed port path slack: {line!r}"
    if slack_s != slack_s or abs(slack_s) == float("inf"):
        return f"non-finite port path slack: {line!r}"
    port_rows[parts[2]].append(
        {
            "engine_check_role": parts[3],
            "slack_ns": slack_s * 1e9,
            "startpoint": parts[5],
            "endpoint": parts[6],
        }
    )
    return None


def _parse_port_path_rows(
    stdout: str, ports: list[str]
) -> dict[str, tuple[str | None, dict[str, list[dict[str, Any]]] | None, str | None]]:
    """``{port: (direction, rows, reason)}`` from :func:`_port_path_lines`'
    block, in request order. ``rows`` is ``{"max": [...], "min": [...]}``
    (``None`` with a ``reason`` when the port's query failed, its record was
    malformed, or the block/port is absent). Lines that are neither ``DIR``,
    ``PATH`` nor ``ERROR`` rows (stray engine warnings) are ignored."""
    block = _extract_block(stdout, _PORT_PATHS_BEGIN, _PORT_PATHS_END)
    if block is None:
        reason = "per-port path records absent from engine output"
        return {port: (None, None, reason) for port in ports}
    directions: dict[str, str] = {}
    rows: dict[str, dict[str, list[dict[str, Any]]]] = {
        port: {"max": [], "min": []} for port in ports
    }
    failures: dict[str, str] = {}
    for line in block.splitlines():
        parts = line.split("\t")
        if parts[0] not in ("DIR", "PATH", "ERROR") or len(parts) < 2:
            continue
        port = parts[1]
        if port not in rows or port in failures:
            continue
        failure = _apply_port_record(parts, line, directions, rows[port])
        if failure is not None:
            failures[port] = failure
    parsed: dict[
        str, tuple[str | None, dict[str, list[dict[str, Any]]] | None, str | None]
    ] = {}
    for port in ports:
        if port in failures:
            parsed[port] = (directions.get(port), None, failures[port])
        elif port not in directions:
            parsed[port] = (None, None, "no record emitted for this port")
        else:
            parsed[port] = (directions[port], rows[port], None)
    return parsed


def _port_paths(stdout: str, report_ports: list[str]) -> dict[str, dict[str, Any]]:
    """``port_paths`` for one session (issue #3042): per requested port, in
    request order, its ``direction`` and the worst ``setup``/``hold``
    records. There is no per-port aggregate to cross-check against, so
    ``consistent_with_aggregate`` stays ``null`` and ``inconsistent`` is
    never produced; a side with no constrained path is ``no_paths``."""
    result: dict[str, dict[str, Any]] = {}
    for port, (direction, rows, reason) in _parse_port_path_rows(
        stdout, report_ports
    ).items():
        if rows is None:
            setup = _empty_path_record("unavailable", reason)
            hold = _empty_path_record("unavailable", reason)
        else:
            setup = _worst_path_record(rows["max"], None)
            hold = _worst_path_record(rows["min"], None)
        result[port] = {"direction": direction, "setup": setup, "hold": hold}
    return result


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

"""Post-route multi-corner sweep and SPEF-annotated STA subsystem for
``klt place-and-route``.

Split out of ``place_and_route.py`` (issue #1808) as a self-contained
subsystem: the post-route multi-corner setup/hold sweep
(:func:`_run_corner_sweep`/:func:`_corner_sweep_script_lines`, issue #949)
and the post-route SPEF-annotated STA pass
(:func:`_post_route_spef_metrics`/:func:`_spef_sta_script_lines`, issue
#948), including its own DEF-pin-name recovery helper
(:func:`_def_pin_net_names`, issue #961). This mirrors the shape of the
earlier ``extract.py``/``extract_abstract.py`` (#1303), ``gen.py``/
``gen_pcells`` (#1698), and ``gen_compose.py``/``gen_compose_routing.py``
(#1708) splits: a self-contained, single-purpose subsystem relocated
verbatim out of a file that had grown past the point one module should hold
stage orchestration *and* this.

Both public entry points into this module
(:func:`_run_corner_sweep`/:func:`_post_route_spef_metrics`) are called from
exactly two sites in ``place_and_route.py``'s own
:func:`~klayout_tools.place_and_route.run_place_and_route` -- everything
upstream of those two call sites stays in ``place_and_route.py``; only this
subsystem's own Tcl-generation/subprocess/report-parsing concern moved here.

Dependency surface is intentionally narrow and mostly one-directional, the
same discipline ``gen_compose_routing.py`` documents for its own reverse
dependency: this module's own already-shared helpers (``_run_openroad``/
``_count_violations`` from ``_openroad_engine.py``, ``_tcl_net_list``/
``_count_spef_nets_annotated`` from ``_paths.py``) are imported at module
scope -- neither creates a cycle, since neither of those modules imports
back from here or from ``place_and_route.py``. The handful of names that
*are* still defined in ``place_and_route.py`` itself (``PlaceAndRouteError``,
``_write_script``, ``_clock_lines``, ``_design_rule_constraint_lines``,
``_design_rule_check_lines``, ``_metrics_report_lines``,
``_violation_count_lines``, ``_read_metrics``, ``_engine_error_message``,
``_EXTRACT_DECK_FOR_CELL_LIBRARY``, and the ``_SETUP_VIOLATIONS_*``/
``_HOLD_VIOLATIONS_*``/``_MAX_TRANSITION_VIOLATIONS_*``/
``_MAX_CAPACITANCE_VIOLATIONS_*``/``_SPEF_NET_CHECK_*`` markers) are
imported *inside*
the functions that use them, deferred rather than at module scope, because
``place_and_route.py`` in turn imports this module's own public entry
points back (module scope, no cycle -- this module never imports
``place_and_route`` at its own module scope) purely to preserve
``klayout_tools.place_and_route.<name>`` as a working import path for every
name the test suite/callers used before this split.
"""

from __future__ import annotations

import os
import re
from typing import Any

from ._openroad_engine import _count_violations, _run_openroad, _timing_status
from ._paths import _count_spef_nets_annotated, _tcl_net_list


def _corner_sweep_script_lines(
    *,
    checkpoint_in: str,
    corners: list[dict[str, str]],
    io_spec: dict[str, str],
    clock_port: str | None,
    clock_period_ns: float | None,
    max_transition_ns: float | None = None,
    max_capacitance_pf: float | None = None,
    max_fanout: float | None = None,
    input_delay_ns: float | None = None,
    output_delay_ns: float | None = None,
) -> list[str]:
    """Tcl for the post-route multi-corner setup/hold sweep (issue #949,
    ``docs/design/post-route-sta-survey.md`` section 4.2) -- a **second**,
    lightweight OpenROAD invocation run *after* the ``"route"`` stage's own
    script (:func:`~klayout_tools.place_and_route._stage_script_lines`) has
    already written its checkpoint, deliberately never folded into that
    script's own OpenSTA session.

    Why a separate session, not more Tcl appended to the route stage's own
    script: OpenSTA's ``define_corners`` must run before *any*
    ``read_liberty`` in a session (``STA-482``), and the route stage's own
    script already runs one bare (un-corner-tagged) ``read_liberty`` for its
    existing single-corner report calls. Loading additional corners into
    *that* session would not just be disallowed by that ordering rule -- it
    would silently change ``report_worst_slack_metric``'s own result even if
    the ordering were reworked around it: that proc (and the ``worst_slack``
    primitive it calls) reports the worst value across **every corner
    currently loaded** with no way to scope it back to one corner, live-
    verified against a real ``openroad/orfs:latest`` container over a real
    volare-fetched ``sky130A`` install (2026-08-13, issue #949) -- loading a
    second corner into the main script's session would silently turn the
    existing ``worst_slack_ns``/``total_negative_slack_ns``/
    ``setup_violation_count``/``hold_violation_count`` fields from
    nominal-corner-only into swept-worst-case, breaking the
    backward-compatibility this change must preserve
    (``docs/json-contract.md``'s additive posture). A wholly separate
    session sidesteps that risk entirely, at the cost of a second OpenROAD
    process launch per ``"route"``-stage run.

    Reads the checkpoint the route stage's own ``write_db`` already
    produced -- no ``read_lef``/``read_verilog``/``link_design`` needed,
    matching every other non-floorplan stage's own
    ``read_db``-restores-the-linked-network convention
    (:func:`~klayout_tools.place_and_route._stage_script_lines`'s own
    docstring/comments). Parasitics are re-estimated
    (``estimate_parasitics -global_routing``) since ``write_db``/``read_db``
    does not carry Sta's own parasitics state across the process boundary
    any more than it carries SDC/linked-library state (same docstring) --
    this reproduces the *same* global-routing RC estimate the route stage's
    own existing ``worst_slack_ns`` is already based on, not a fidelity
    regression or a second, differently-sourced number.

    ``report_worst_slack_metric -setup``/``-hold`` (run here for the first
    and only time in this session, so no scoping concern applies) writes
    this invocation's own ``-metrics`` dump's ``timing__setup__ws``/
    ``timing__hold__ws`` keys -- automatically the worst value across every
    corner :func:`klayout_tools.pdk.list_lib_corners` enumerated (confirmed
    live: setup slack worst-cases at the slowest loaded corner, hold slack
    at the fastest, exactly the industrial "setup at slow PVT, hold at fast
    PVT" convention this survey's section 3.3 describes -- no manual
    slow/fast corner classification needed in Python).

    ``report_tns_metric -setup``/``-hold`` (issue #1866) rides alongside the
    two ``report_worst_slack_metric`` calls above, in the same session and
    for the same reason: it is the matching total-negative-slack pair
    ``klt sta``'s own script (:func:`~klayout_tools.post_route_sta.
    _sta_script_lines`) already issues, writing ``timing__setup__tns``/
    ``timing__hold__tns`` into this invocation's own ``-metrics`` dump. Its
    caller (:func:`_run_corner_sweep`) reads those two keys the same way it
    already reads the worst-slack pair, to populate each ``corners[]``
    entry's ``total_negative_setup_slack_ns``/``total_negative_hold_slack_ns``
    fields -- without this, recovering TNS per swept corner cost a caller one
    extra ``klt sta`` run (and OpenSTA session) per corner, re-reading the
    same ODB/DEF and liberty this session already has loaded.

    The trailing ``report_check_types`` pair (issue #1709's "Two smaller
    things found alongside" item 1, folded into that issue's own Builder
    scope by its 2026-09-15 revision -- see
    :func:`~klayout_tools.place_and_route._design_rule_check_lines`) is the
    **design-rule** counterpart of the two slack metrics above, and rides in
    this same session for exactly the same reason they do: this is the only
    session that loads every swept deck's own ``max_transition``/
    ``max_capacitance`` limits, so it is the only place a max-transition /
    max-capacitance verdict at those decks can be measured at all. Run
    **after** ``estimate_parasitics``, since a slew/capacitance check against
    an un-estimated network is not a meaningful number. Adds no OpenROAD
    invocation of its own -- only two more report calls inside an invocation
    the ``"route"`` stage already pays for.
    """
    from .place_and_route import (
        _clock_lines,
        _design_rule_check_lines,
        _design_rule_constraint_lines,
        _io_delay_lines,
    )

    lines = [f"read_db {checkpoint_in}"]
    lines += ["define_corners " + " ".join(corner["name"] for corner in corners)]
    lines += [
        f"read_liberty -corner {corner['name']} {corner['path']}" for corner in corners
    ]
    lines += _clock_lines(clock_port, clock_period_ns)
    lines += _design_rule_constraint_lines(
        max_transition_ns, max_capacitance_pf, max_fanout
    )
    # Issue #1865: the same `set_input_delay`/`set_output_delay` pair the
    # `"route"` stage's own script already emitted. `read_db` does not carry
    # SDC state across the process boundary (see this function's own
    # docstring), so without re-emitting them here every swept corner would
    # report the unconstrained sentinel for a design whose only paths are
    # input-port -> register / register -> output-port.
    lines += _io_delay_lines(clock_port, input_delay_ns, output_delay_ns)
    lines += [
        f"set_wire_rc -layer {io_spec['layer_v']}",
        "estimate_parasitics -global_routing",
        "report_worst_slack_metric -setup",
        "report_worst_slack_metric -hold",
        "report_tns_metric -setup",
        "report_tns_metric -hold",
    ]
    lines += _design_rule_check_lines()
    return lines


# --------------------------------------------------------------------------- #
# Post-route SPEF STA (`request.post_route_spef`, issue #948)
# --------------------------------------------------------------------------- #


def _spef_sta_script_lines(
    *,
    checkpoint_in: str,
    liberty_path: str,
    clock_port: str | None,
    clock_period_ns: float | None,
    spef_path: str,
    net_names: list[str],
    sdf_path: str | None = None,
    max_transition_ns: float | None = None,
    max_capacitance_pf: float | None = None,
    max_fanout: float | None = None,
    input_delay_ns: float | None = None,
    output_delay_ns: float | None = None,
) -> list[str]:
    """Build the Tcl script for the second, ``post_route_spef``-only
    ``openroad`` invocation (issue #948, Epic #700 Phase 3) -- a fresh
    OpenSTA session seeded from the ``"route"`` stage's own checkpoint
    (``read_db``), with ``klt extract --parasitics``'s SPEF output
    (``spef_path``) fed in via ``read_spef`` instead of that stage's own
    ``estimate_parasitics -global_routing`` estimate, so the two can be
    diffed on the identical routed design (the A/B measurement plan
    ``docs/design/post-route-sta-survey.md`` §4.1/§5 describes).

    The net-name-correlation sanity check (survey §4.1's flagged open risk:
    do `klt extract`'s net names correlate with OpenSTA's own flat netlist
    net names?) runs **before** ``read_spef`` -- deliberately independent of
    whatever `read_spef` itself does with an unmatched name, so an uncaught
    Tcl error partway through that call cannot also silently skip this check.
    It is measured in **both directions**, because they answer different
    questions and only the second one decides whether the timing numbers
    below are a real-parasitics measurement:

    - *SPEF-side* (``get_nets -quiet <name>`` per SPEF net): how many of the
      names the SPEF declares exist in the linked design. Flat extraction
      also yields each standard cell's own internal nodes, which the
      gate-level design legitimately has no counterpart for, so this ratio
      is expected to sit well below 1 even on a perfectly-correlated run.
      ``-quiet`` suppresses OpenSTA's own "not found" error, so a mismatched
      name degrades to "not annotated" rather than aborting the script.
    - *design-side* (every ``get_nets *`` in the design, looked up in the
      SPEF's own name set): how many of the design's nets the SPEF actually
      names. **This** is the ratio that says whether every net OpenSTA times
      got real routed parasitics.

    Net names are embedded verbatim, not glob-escaped. ``_tcl_net_list``
    wraps each in Tcl braces, and brace-quoting performs no backslash
    substitution -- so a backslash-escaped ``a\\[10\\]`` would reach
    ``get_nets`` still carrying its backslashes and match nothing, while the
    plain ``a[10]`` matches the real net directly (verified live against
    OpenSTA, issue #951; the escaping this replaced was never exercised
    against a name that existed in the design, since #948's baseline
    correlation was 0%).

    ``sdf_path`` (issue #1002, survey §4.3), when given, appends one
    ``write_sdf`` call **immediately after ``read_spef``** and before any
    report command -- the ordering §4.3 states explicitly ("after §4.1's
    ``read_spef``, so the written SDF reflects real routed-parasitic
    delays"), and the reason this rides in *this* session rather than the
    primary ``"route"`` stage's own. Two flags are passed deliberately:

    - ``-divider .`` -- SDF hierarchy divider. OpenSTA's own default is
      ``/``, but the consumer of this file is a Verilog simulator whose
      hierarchy separator is ``.`` (this repo's own consumer being
      ``klt functional-verification``'s ``options.sdf`` block, whose
      ``$sdf_annotate`` root-instance argument is a Verilog path). Emitting
      ``/`` would leave every ``INSTANCE`` name unmatchable.
    - ``-include_typ`` -- populate the *typ* member of every ``min:typ:max``
      triplet. Icarus selects a corner from the triplet at compile time
      (``iverilog -T min|typ|max``, its default being ``typ``; verified live
      in ``docs/design/sdf-annotate-feasibility-spike.md`` §3.5), so a
      triplet written with an empty typ member -- OpenSTA's default -- would
      leave the *default* Icarus corner selecting nothing.
    """
    from .place_and_route import (
        _SPEF_NET_CHECK_BEGIN,
        _SPEF_NET_CHECK_END,
        _clock_lines,
        _design_rule_constraint_lines,
        _io_delay_lines,
        _metrics_report_lines,
        _violation_count_lines,
    )

    lines = [f"read_db {checkpoint_in}", f"read_liberty {liberty_path}"]
    lines += _clock_lines(clock_port, clock_period_ns)
    lines += _design_rule_constraint_lines(
        max_transition_ns, max_capacitance_pf, max_fanout
    )
    # Issue #1865: re-emitted here for the same reason `_clock_lines` above
    # is -- this is a fresh OpenSTA session seeded from a `read_db`, which
    # carries no SDC state of its own.
    lines += _io_delay_lines(clock_port, input_delay_ns, output_delay_ns)
    lines += [
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
        "foreach klt_design_net [get_nets -quiet *] {",
        "    incr klt_design_total",
        "    if {[info exists klt_spef_have([get_full_name $klt_design_net])]} {",
        "        incr klt_design_annotated",
        "    }",
        "}",
        f'puts "{_SPEF_NET_CHECK_BEGIN}"',
        'puts "$klt_spef_annotated [llength $klt_spef_nets]"',
        'puts "$klt_design_annotated $klt_design_total"',
        f'puts "{_SPEF_NET_CHECK_END}"',
        f"read_spef {spef_path}",
    ]
    if sdf_path is not None:
        lines.append(f"write_sdf -divider . -include_typ {sdf_path}")
    lines += _metrics_report_lines(include_fmax=False, include_power=False)
    lines += _violation_count_lines()
    return lines


#: Matches a DEF ``PINS`` section's opening line (``PINS <numPins> ;``,
#: LEF/DEF 5.8 Language Reference section 6.8) -- the *only* place a routed
#: DEF declares which nets are genuine design-boundary I/O, independent of
#: whatever internal net a later ``klt extract --def-net-names`` pass names
#: from routed-metal geometry (see :func:`_def_pin_net_names`).
_DEF_PINS_BEGIN_RE = re.compile(r"^\s*PINS\s+\d+\s*;\s*$")
#: Matches the section's closing ``END PINS`` line.
_DEF_PINS_END_RE = re.compile(r"^\s*END\s+PINS\s*$")
#: Matches one pin record's opening line, ``- <pinName> ...`` -- a new
#: record always starts a fresh line inside the section (LEF/DEF grammar).
_DEF_PIN_START_RE = re.compile(r"^\s*-\s+(\S+)")
#: Matches the ``+ NET <netName>`` clause a pin record carries when the
#: pin's own name differs from the net it connects to (bus pins are the
#: common case DEF allows this for) -- searched line-by-line across a
#: record's own (possibly multi-line) body.
_DEF_PIN_NET_RE = re.compile(r"\bNET\s+(\S+)")


def _def_pin_net_names(def_path: str) -> frozenset[str] | None:
    """Parse ``def_path``'s own ``PINS`` section for the design's genuine
    top-level port *net* names (issue #961's defect 1: ``klt extract``'s
    ``--def-net-names`` mode names every routed net from DEF geometry, which
    ``Netlist.make_top_level_pins()`` then promotes to a circuit pin
    indiscriminately -- so without this, the post-route SPEF's ``*PORTS``
    list wrongly declares every routed net a top-level port instead of only
    the design's actual I/O).

    Each pin record is ``- <pinName> [+ NET <netName>] ... ;`` -- the net
    name is reported when the ``+ NET`` clause is present (the case a bus
    pin whose own name differs from its net needs), else the pin's own name
    (the common case for a flat, one-bit-per-pin digital design, where the
    two are identical). This is deliberately a small, targeted scan (not a
    general DEF parser) -- this repo already reads DEF geometry through
    ``klayout.db``'s LEF/DEF reader
    (:func:`~klayout_tools.place_and_route._merge_def_to_gds`) for every
    other purpose; a full parse here would duplicate that reader just to
    recover text this section already states directly.

    Returns ``None`` -- never an empty set -- when the scan recovers no pin
    name at all: either the file carries no ``PINS`` section (a non-DEF or
    malformed file, e.g. this repo's own test fixtures' placeholder DEF
    text) or the section is present but empty/unparseable. Both are
    "cannot determine the design's ports," which the caller must not
    confuse with "the design declares zero ports" -- the latter would demote
    *every* net back to internal and reproduce a different (equally wrong)
    failure mode. A routed design that reached this function always has at
    least a clock port (``constraints.clock_port`` is required from
    ``target_stage: "place"`` on), so an empty result here is always a parse
    failure, never a real design with no I/O.

    Also exported under the public name :data:`def_pin_names` (issue #1390):
    ``klt extract --def-pins <path>`` reuses this same parser to derive the
    declared top-level port set automatically from a routed DEF, for a
    caller who has not separately hand-derived a ``--pins`` list -- see
    ``docs/cli/extract.md``'s "DEF-derived declared pins" section.
    """
    try:
        with open(def_path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return None

    names: set[str] = set()
    in_pins = False
    current_pin: str | None = None
    current_net: str | None = None

    def _flush() -> None:
        nonlocal current_pin, current_net
        if current_pin is not None:
            names.add(current_net if current_net is not None else current_pin)
        current_pin = None
        current_net = None

    for line in text.splitlines():
        if not in_pins:
            if _DEF_PINS_BEGIN_RE.match(line):
                in_pins = True
            continue
        if _DEF_PINS_END_RE.match(line):
            _flush()
            break
        start_match = _DEF_PIN_START_RE.match(line)
        if start_match:
            _flush()
            current_pin = start_match.group(1)
        if current_pin is not None:
            net_match = _DEF_PIN_NET_RE.search(line)
            if net_match:
                current_net = net_match.group(1)
        if line.rstrip().endswith(";"):
            _flush()

    return frozenset(names) if names else None


#: Public alias for :func:`_def_pin_net_names` (issue #1390): ``klt extract
#: --def-pins <path>`` reuses this exact DEF ``PINS``-section parser to
#: derive the declared top-level port set automatically from a routed DEF,
#: instead of requiring a caller to hand-list it via ``--pins``. Exported
#: under a public name (no leading underscore) because it is now a
#: cross-module entry point (``cli/extract_cmd.py`` imports it), while
#: ``_def_pin_net_names`` itself stays as the name this module's own
#: internal call site (:func:`_post_route_spef_metrics`) and its existing
#: direct-import tests (``tests/test_place_and_route.py``) already use --
#: same function object, two names, no behavior difference either way.
def_pin_names = _def_pin_net_names


def _post_route_spef_metrics(
    *,
    output_dir: str,
    hdl_toplevel: str,
    gds_path: str,
    def_path: str,
    cell_library: str,
    liberty_path: str,
    clock_port: str | None,
    clock_period_ns: float | None,
    checkpoint_in: str,
    write_sdf: bool = False,
    max_transition_ns: float | None = None,
    max_capacitance_pf: float | None = None,
    max_fanout: float | None = None,
    input_delay_ns: float | None = None,
    output_delay_ns: float | None = None,
    engine_logs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """``request.post_route_spef``'s own pipeline (issue #948, Epic #700
    Phase 3): extract real per-net R/C from the just-merged routed GDS via
    `klt extract --parasitics`, write it as SPEF, and feed that SPEF into a
    fresh OpenSTA session (seeded from the ``"route"`` stage's own
    checkpoint) via ``read_spef`` -- the real-parasitics A/B counterpart to
    that stage's own ``estimate_parasitics -global_routing``-derived
    top-level fields. See ``docs/design/post-route-sta-survey.md`` §4.1 and
    ``docs/cli/place-and-route.md``'s ``spef_sta`` field for the full
    contract.

    Returns::

        {
            "spef_path": str,
            "sdf_path": str | None,
            "worst_slack_ns": float | None,
            "total_negative_slack_ns": float | None,
            "timing_status": str | None,
            "setup_violation_count": int,
            "hold_violation_count": int,
            "nets_annotated": int,
            "nets_total": int,
            "design_nets_annotated": int,
            "design_nets_total": int,
            "annotation_complete": bool,
            "annotation_warning": str | None,
        }

    ``write_sdf`` (``request.post_route_sdf``, issue #1002, survey §4.3):
    when true, the same OpenSTA session also writes its delays out as an
    IEEE-1497 SDF file, immediately after ``read_spef`` -- so the emitted
    cell and interconnect delays are the real ones this session computed
    from the resolved liberty plus the extracted routed parasitics, never a
    synthetic or uniform model. The path is reported as ``sdf_path``
    (``None`` when not requested), mirroring ``spef_path``. It is the
    artifact ``klt functional-verification``'s ``options.sdf`` block
    consumes; see :func:`_spef_sta_script_lines` for the two ``write_sdf``
    flags that make the output consumable by a Verilog simulator at all.

    Two independent coverage ratios are reported, because they answer
    different questions (see :func:`_spef_sta_script_lines`):

    - ``nets_annotated``/``nets_total`` -- SPEF-side. Extraction is flat, so
      the SPEF also carries every standard cell's own internal nodes, which a
      gate-level linked design has no counterpart for by construction; this
      ratio is therefore expected to sit well below 1 even when correlation
      is perfect, and is reported for completeness rather than as a pass/fail
      gate.
    - ``design_nets_annotated``/``design_nets_total`` -- design-side, and the
      one that decides whether these timing numbers are a real-parasitics
      measurement: how many of the nets OpenSTA actually times the SPEF names
      at all.

    ``annotation_complete``/``annotation_warning`` are the *loud* half of the
    "do not silently drop annotation on unmatched nets" requirement issue
    #948's own scope states: keyed on the **design-side** ratio, whenever
    ``design_nets_annotated < design_nets_total`` the warning string spells
    the shortfall out in the response itself, so a caller diffing
    ``worst_slack_ns`` against the top-level (estimate-derived) value cannot
    mistake a partly-annotated re-report for a real-parasitics measurement.

    Reaching complete annotation is what the ``def_net_names=True`` argument
    to ``klt extract`` below buys (issue #951): without it, extraction names
    nets from GDS text labels, the DEF->GDS merge emits labels for top-level
    pins only, and every internal routed net reaches the SPEF under a
    KLayout-synthesized ``$<n>`` name no OpenSTA net is called -- measured at
    literally `0` annotated nets on all three corpus designs when #948
    shipped. See ``docs/cli/place-and-route.md``'s "Net-name correlation"
    subsection for the measurement.

    ``declared_pins`` (issue #961's defect 1): every routed net now carries a
    real DEF-derived name (the paragraph above), so ``klt extract``'s own
    ``Netlist.make_top_level_pins()`` -- which promotes *every* named net to
    a top-level circuit pin, with no concept of "design port" beyond "has a
    name" -- promotes all of them, not just the design's actual I/O. Left
    unfixed, the written SPEF's ``*PORTS``/``*P`` list wrongly declares every
    one of those (537 of 537 on the routed `gcd` corpus fixture) a top-level
    port. :func:`_def_pin_net_names` reads ``def_path``'s own ``PINS``
    section -- the DEF's own declaration of which nets are genuine
    design-boundary ports, unaffected by the net-name renaming above -- and
    that set is passed through as ``declared_pins`` (the pre-existing
    ``--pins`` mechanism, issue #514): every promoted pin *not* in the set is
    demoted back to an internal net, so only real ports remain. A DEF with no
    parseable ``PINS`` section (should not happen for a real routed DEF; only
    exercised by this module's own placeholder-DEF test fixtures) makes
    :func:`_def_pin_net_names` return ``None``, which is ``run_extract``'s own
    "no restriction" default -- falls back to `klt extract`'s pre-#961
    behavior rather than wrongly declaring zero ports.

    Measured on the committed routed corpus fixture
    (``tests/corpus/place_and_route/gcd.gds.gz``, extracted with
    ``def_net_names=True`` exactly as below): ``*PORTS`` entries drop from
    **463 to 54** (`gcd`'s 52 real I/O plus `VPWR`/`VGND`), while the written
    SPEF's ``*D_NET`` set is **bit-for-bit the same 1392 blocks** -- the
    demotion changes which nets are *declared ports*, never which nets carry
    parasitics, so the net-name annotation ratio issue #951 closed is
    untouched. Asserted by
    ``tests/test_extract.py::test_declared_pins_restricts_spef_ports_on_the_routed_corpus``.

    ``def_net_connections`` (issue #961's own root defect: device-terminal
    ``*I <inst>:<pin>`` connectivity): :func:`~klayout_tools.extract.
    def_net_instance_pins` reads ``def_path``'s own ``NETS`` section for
    each net's real cell-instance connections, in the same instance/pin
    spelling the linked gate-level design already uses (DEF preserves the
    flow's original names verbatim). Passed through as ``run_extract``'s
    ``def_net_connections``, this makes every real design pin on an
    unambiguously-named net a genuine, resolvable node in the written
    SPEF's RC network -- see :func:`~klayout_tools.extract._write_spef`'s
    docstring for the exact ``*CONN``/``*RES`` shape and the duplicate-net-
    name guard (a net name shared by several un-strapped islands, e.g.
    `gcd`'s 105 ``VGND`` islands, is skipped rather than asserting
    connectivity no single island actually has). This is what lets a real
    OpenSTA `read_spef` session actually attach a net's total capacitance
    to a driver/load pin instead of discarding the whole ``*D_NET`` block
    as unconnected -- it does **not** model resistance *between* DEF-level
    pins (this repo's own extracted parasitics have no notion of which pin
    drives a net or the physical wire path to each load), only that they
    are the same net at its own lumped potential.

    **Live OpenSTA re-verification of this increment's own acceptance
    criteria (zero "partially unannotated drivers", `worst_slack_ns`
    changing across `read_spef`) was not possible while implementing this
    -- no running Docker daemon was available in this session** (the same
    constraint issue #961's own PR #974 comment recorded). The `*CONN`/
    `*RES` shape above was verified structurally (unit tests assert the
    written SPEF's exact text), not against a real OpenSTA session; a
    follow-up pass with container access should re-run the
    `report_parasitic_annotation`/`worst_slack` measurement issue #961's
    acceptance criteria describe before this is treated as fully closed.

    Raises :class:`~klayout_tools.place_and_route.PlaceAndRouteError` for an
    unsupported ``cell_library`` (no known `klt extract --deck`, see
    :data:`~klayout_tools.place_and_route._EXTRACT_DECK_FOR_CELL_LIBRARY`), a
    `klt extract --parasitics` failure on the merged GDS, or a failed second
    `openroad` invocation -- the same "fail loud, never silently skip"
    posture this module's other per-cell-library reference tables already
    follow (see
    :data:`~klayout_tools.place_and_route._CTS_BUFFER_CELLS` and its call
    sites).
    """
    from .place_and_route import (
        _EXTRACT_DECK_FOR_CELL_LIBRARY,
        _HOLD_VIOLATIONS_BEGIN,
        _HOLD_VIOLATIONS_END,
        _SETUP_VIOLATIONS_BEGIN,
        _SETUP_VIOLATIONS_END,
        _SPEF_NET_CHECK_BEGIN,
        _SPEF_NET_CHECK_END,
        _SPEF_NET_CHECK_RE,
        PlaceAndRouteError,
        _engine_error_message,
        _read_metrics,
        _write_script,
    )

    extract_deck = _EXTRACT_DECK_FOR_CELL_LIBRARY.get(cell_library)
    if extract_deck is None:
        raise PlaceAndRouteError(
            "request.post_route_spef requires a known klt extract deck for "
            f"cell_library '{cell_library}' (supported: "
            + ", ".join(sorted(_EXTRACT_DECK_FOR_CELL_LIBRARY))
            + ")"
        )

    # Local import (mirrors `_merge_def_to_gds`'s own local `klayout.db`
    # import): keeps `extract.py`'s own (heavier) import cost out of every
    # `place_and_route.py` import that never exercises this opt-in path.
    from .extract import ExtractError, def_net_instance_pins, run_extract

    spice_path = os.path.join(output_dir, f"{hdl_toplevel}_route_parasitics.spice")
    spef_path = os.path.join(output_dir, f"{hdl_toplevel}_route.spef")
    # Issue #961 defect 1: the DEF's own `PINS` section is the ground truth
    # for which nets are genuine top-level design ports, independent of the
    # `def_net_names=True` renaming below (which gives *every* routed net a
    # real name, and would otherwise get all of them promoted to `*PORTS`).
    # `None` (no parseable `PINS` section -- not expected for a real routed
    # DEF) falls back to not passing `declared_pins` at all, never to
    # declaring zero ports.
    declared_pins = _def_pin_net_names(def_path)
    # Issue #961's own remaining scope (device-terminal `*CONN` pin
    # correlation): the DEF's own `NETS` section names each net's real
    # `(instance, pin)` connections, in the same instance/pin spelling the
    # linked gate-level design uses -- see `def_net_instance_pins`'s own
    # docstring for the exact `*I`/`*RES` shape this produces and the
    # duplicate-net-name guard that skips it. `{}` (never `None`, matching
    # `def_net_instance_pins`'s own "absence is not proof of zero" posture)
    # for a DEF with no parseable `NETS` section.
    net_instance_pins = def_net_instance_pins(def_path)
    try:
        extraction = run_extract(
            gds_path,
            extract_deck,
            output=spice_path,
            top=hdl_toplevel,
            parasitics=True,
            spef_output=spef_path,
            # Issue #951: name every routed net from the DEF net name
            # KLayout's LEF/DEF reader left on its geometry (GDS shape
            # property 1) when `_merge_def_to_gds` produced this GDS, rather
            # than from text labels -- which the merge only emits for
            # top-level pins. This is what makes the SPEF's `*D_NET` names
            # the same strings the OpenSTA session below has linked, and so
            # what makes `read_spef` annotate anything at all.
            def_net_names=True,
            # Issue #961: restrict the promoted-pin set (and so the written
            # SPEF's `*PORTS`/`*P` entries) to the DEF's own declared ports --
            # see this function's docstring, "declared_pins" paragraph.
            # `None` is `run_extract`'s own "no restriction" default, i.e.
            # exactly the pre-#961 behaviour.
            declared_pins=declared_pins,
            # Issue #961: real cell-instance `*CONN`/`*RES` correlation --
            # see this function's docstring, "def_net_connections" paragraph.
            def_net_connections=net_instance_pins,
        )
    except ExtractError as exc:
        raise PlaceAndRouteError(
            f"request.post_route_spef: klt extract --parasitics failed on "
            f"the routed GDS '{gds_path}': {exc}"
        ) from exc

    parasitics = extraction["parasitics"]
    net_names = sorted({entry["net"] for entry in parasitics["nets"]})

    script_path = os.path.join(output_dir, f"pnr_{hdl_toplevel}_route_spef.tcl")
    metrics_path = os.path.join(output_dir, f"{hdl_toplevel}_route_spef_metrics.json")
    sdf_path = (
        os.path.join(output_dir, f"{hdl_toplevel}_route.sdf") if write_sdf else None
    )
    lines = _spef_sta_script_lines(
        checkpoint_in=checkpoint_in,
        liberty_path=liberty_path,
        clock_port=clock_port,
        clock_period_ns=clock_period_ns,
        spef_path=spef_path,
        net_names=net_names,
        sdf_path=sdf_path,
        max_transition_ns=max_transition_ns,
        max_capacitance_pf=max_capacitance_pf,
        max_fanout=max_fanout,
        input_delay_ns=input_delay_ns,
        output_delay_ns=output_delay_ns,
    )
    _write_script(script_path, lines)

    completed = _run_openroad(
        script_path,
        metrics_path,
        error_cls=PlaceAndRouteError,
        engine_logs=engine_logs,
    )
    with completed.diagnostics(PlaceAndRouteError):
        if completed.returncode != 0:
            raise PlaceAndRouteError(_engine_error_message("route_spef_sta", completed))

        # Fail loud rather than reporting a path to a file that is not there
        # (issue #1002): a `sdf_path` in the response is a promise a downstream
        # `klt functional-verification --options.sdf` run will consume, and a
        # missing-but-reported artifact would surface there as a *silent*
        # zero-delay run (Icarus's `$sdf_annotate` treats an unopenable file as a
        # non-fatal `SDF WARNING`, `vvp` still exits 0 -- see
        # `docs/design/sdf-annotate-feasibility-spike.md` §3.3).
        if sdf_path is not None and not os.path.isfile(sdf_path):
            raise PlaceAndRouteError(
                "request.post_route_sdf: the post-route OpenSTA session reported "
                f"success but wrote no SDF file at '{sdf_path}'"
            )

        metrics = _read_metrics(metrics_path, "route_spef_sta")
    setup_violation_count = _count_violations(
        completed.stdout, _SETUP_VIOLATIONS_BEGIN, _SETUP_VIOLATIONS_END
    )
    hold_violation_count = _count_violations(
        completed.stdout, _HOLD_VIOLATIONS_BEGIN, _HOLD_VIOLATIONS_END
    )
    nets_check = _count_spef_nets_annotated(
        completed.stdout,
        begin=_SPEF_NET_CHECK_BEGIN,
        end=_SPEF_NET_CHECK_END,
        pattern=_SPEF_NET_CHECK_RE,
    )
    nets_annotated, nets_total, design_nets_annotated, design_nets_total = (
        nets_check if nets_check is not None else (0, len(net_names), 0, 0)
    )

    # The loud half of issue #948's "do not silently drop annotation on
    # unmatched nets": an incomplete correlation is stated in the response
    # itself, in words, rather than left for a caller to notice by comparing
    # two integers -- because the numbers next to it (`worst_slack_ns` &c.)
    # look exactly like a real measurement whether or not any net was
    # actually annotated. Keyed on the *design*-side ratio (issue #951): the
    # SPEF-side one counts flat extraction's intra-standard-cell nodes, which
    # a gate-level linked design never has and whose absence says nothing
    # about the quality of the annotation.
    annotation_complete = (
        design_nets_total > 0 and design_nets_annotated == design_nets_total
    )
    annotation_warning: str | None = None
    if not annotation_complete:
        annotation_warning = (
            f"only {design_nets_annotated} of {design_nets_total} nets in the "
            "linked design are named by this SPEF -- the slack/violation "
            "values in this block are NOT a real-parasitics measurement to "
            "the extent annotation is missing, and should not be compared "
            "against the top-level estimate_parasitics-derived fields as if "
            "they were. See docs/cli/place-and-route.md's 'Net-name "
            "correlation' subsection."
        )

    worst_slack = metrics.get("timing__setup__ws")
    tns = metrics.get("timing__setup__tns")

    return {
        "spef_path": spef_path,
        # Additive (issue #1002): `None` unless `request.post_route_sdf` was
        # `true`, mirroring `spef_path`'s own shape.
        "sdf_path": sdf_path,
        "worst_slack_ns": round(worst_slack, 5) if worst_slack is not None else None,
        "total_negative_slack_ns": round(tns, 5) if tns is not None else None,
        # Additive (issue #1865): whether this block's own `worst_slack_ns`
        # is a measurement or OpenSTA's unconstrained sentinel (`1e+39`) --
        # the same field, computed the same way, as the top-level one.
        "timing_status": _timing_status((worst_slack,)),
        "setup_violation_count": setup_violation_count,
        "hold_violation_count": hold_violation_count,
        "nets_annotated": nets_annotated,
        "nets_total": nets_total,
        "design_nets_annotated": design_nets_annotated,
        "design_nets_total": design_nets_total,
        "annotation_complete": annotation_complete,
        "annotation_warning": annotation_warning,
    }


def _run_corner_sweep(
    *,
    checkpoint_in: str,
    corners: list[dict[str, str]],
    io_spec: dict[str, str],
    clock_port: str | None,
    clock_period_ns: float | None,
    output_dir: str,
    hdl_toplevel: str,
    max_transition_ns: float | None = None,
    max_capacitance_pf: float | None = None,
    max_fanout: float | None = None,
    input_delay_ns: float | None = None,
    output_delay_ns: float | None = None,
    engine_logs: list[dict[str, Any]] | None = None,
) -> tuple[float | None, float | None, list[dict[str, Any]], int | None, int | None]:
    """Run the post-route multi-corner setup/hold sweep (issue #949) as a
    second OpenROAD invocation, after the ``"route"`` stage's own script has
    already written ``checkpoint_in`` -- see :func:`_corner_sweep_script_lines`
    for why this cannot be folded into that script's own session.

    Returns ``(worst_setup_slack_ns, worst_hold_slack_ns, corners,
    max_transition_violation_count, max_capacitance_violation_count)``. The
    first two, each rounded to 5 decimal places (matching
    :func:`~klayout_tools.place_and_route._extract_stage_metrics`'s own
    ``worst_slack_ns`` rounding) or ``None`` if that invocation's own
    ``-metrics`` dump didn't populate the corresponding key, are issue #949's
    original aggregate fields -- unchanged by issue #1092, still sourced from
    this same combined, every-``corners``-loaded-at-once session's own
    unscoped ``report_worst_slack_metric -setup``/``-hold`` calls.
    ``(None, None, [], None, None)`` when ``corners`` is empty --
    either the "should not happen in practice" case the original #949
    docstring named (a resolved ``liberty_path`` implies at least one
    shipped ``.lib``), or issue #1092's own explicit
    ``request.pdk.sweep_corners: []`` (sweep zero corners) -- degrading to
    ``null``/``[]`` rather than raising on an empty sweep target list keeps
    this helper total either way.

    The last two elements are issue #1709's own addition: the
    **design-rule-check verdict** at those same swept decks -- how many pins
    violate the max-transition (``-max_slew``) and max-capacitance limits in
    force at the corners actually loaded, counted out of this same combined
    invocation's own stdout by the identical marker-delimited
    ``"(VIOLATED)"`` scrape ``setup_violation_count``/``hold_violation_count``
    already use (see
    :func:`~klayout_tools.place_and_route._design_rule_check_lines`). They
    are a *verdict against whatever limits the loaded decks declare*, so they
    are meaningful whether or not the caller set
    ``request.constraints.max_transition_ns``/``.max_capacitance_pf`` -- with
    those set, they additionally say whether the ``repair_design`` pass that
    was aimed at the caller's own tighter target actually hit it. No
    **fanout** counterpart is reported, deliberately: see
    ``_design_rule_check_lines``'s own docstring for the
    ``sta::max_fanout_violation_count`` SIGSEGV this avoids.

    The third element is issue #1092's own addition: a per-corner
    breakdown, ``[{"name": ..., "setup_slack_ns": ..., "hold_slack_ns":
    ..., "timing_status": ...}, ...]``, naming which corner produced each
    of the two aggregates above -- closing the "response never names the
    corner that decided either one" gap #1092 reports. Each entry also
    carries ``total_negative_setup_slack_ns``/``total_negative_hold_slack_ns``
    (issue #1866): the matching *total*-negative-slack pair alongside the
    *worst*-slack pair above, from that same corner's own
    ``report_tns_metric -setup``/``-hold`` call
    (:func:`_corner_sweep_script_lines`) -- purely additive, so a caller who
    only ever read ``setup_slack_ns``/``hold_slack_ns`` sees no change.
    Naming mirrors ``klt sta``'s own explicit-setup/explicit-hold pair
    (``total_negative_slack_ns``/``total_negative_hold_slack_ns``) but
    spells both sides explicitly here, since ``corners[]`` already has both
    an explicit ``setup_slack_ns`` and ``hold_slack_ns`` sitting next to
    each other. ``timing_status`` (issue #1865) is that corner's *own*
    constrained/unconstrained verdict, computed from its own two
    worst-slack values rather than copied from the aggregate, so a corner
    OpenSTA could not time is never mistaken for a clean one. Deliberately
    **not** derived by adding a
    ``-corner`` argument to ``report_worst_slack_metric`` inside the
    existing combined session: this module's own live-verified finding
    (``docs/cli/place-and-route.md``'s "Multi-corner setup/hold sweep"
    section) is that ``report_worst_slack_metric`` has no way to scope its
    result back to one corner once more than one is loaded, and
    ``docs/design/post-route-sta-survey.md`` section 4.2 itself left "N
    re-runs (or one ``define_corners`` session, whichever a live-verified
    audit finds cheaper/more correct)" as an explicitly open implementation
    choice. Absent a real OpenROAD container to re-verify a corner-scoped
    variant against (not available in this environment either), this
    resolves that choice conservatively: one lightweight single-corner
    OpenROAD invocation per swept corner, reusing this exact same
    single-corner-session shape the combined sweep script already reduces
    to at ``len(corners) == 1`` -- a mechanism this module has run and
    tested for years, not new, unverified Tcl surface. The common case
    (a library with exactly one shipped/scoped corner) needs **no** extra
    invocation at all: with only one corner loaded, the combined session's
    own aggregate values already *are* that corner's own values, so the
    breakdown is built directly from them. Only ``len(corners) > 1`` pays
    the documented extra wall-clock cost of ``len(corners)`` additional
    OpenROAD subprocess launches -- see ``docs/cli/place-and-route.md``'s
    "Multi-corner setup/hold sweep" section for the measured baseline this
    adds on top of.

    Raises :class:`~klayout_tools.place_and_route.PlaceAndRouteError` on an
    OpenROAD engine failure, exactly like every other stage invocation in
    this module -- silently degrading these fields to ``null`` on a broken
    sweep would undermine the very multi-corner evidence claim they exist
    to support (T1 checklist item 5, ``docs/design-evidence-tiers.md``).
    """
    from .place_and_route import (
        _MAX_CAPACITANCE_VIOLATIONS_BEGIN,
        _MAX_CAPACITANCE_VIOLATIONS_END,
        _MAX_TRANSITION_VIOLATIONS_BEGIN,
        _MAX_TRANSITION_VIOLATIONS_END,
        PlaceAndRouteError,
        _engine_error_message,
        _read_metrics,
        _write_script,
    )

    if not corners:
        return None, None, [], None, None

    script_path = os.path.join(output_dir, f"pnr_{hdl_toplevel}_route_corners.tcl")
    metrics_path = os.path.join(
        output_dir, f"{hdl_toplevel}_route_corners_metrics.json"
    )
    lines = _corner_sweep_script_lines(
        checkpoint_in=checkpoint_in,
        corners=corners,
        io_spec=io_spec,
        clock_port=clock_port,
        clock_period_ns=clock_period_ns,
        max_transition_ns=max_transition_ns,
        max_capacitance_pf=max_capacitance_pf,
        max_fanout=max_fanout,
        input_delay_ns=input_delay_ns,
        output_delay_ns=output_delay_ns,
    )
    _write_script(script_path, lines)

    completed = _run_openroad(
        script_path,
        metrics_path,
        error_cls=PlaceAndRouteError,
        engine_logs=engine_logs,
    )
    with completed.diagnostics(PlaceAndRouteError):
        if completed.returncode != 0:
            raise PlaceAndRouteError(
                _engine_error_message("route (corner sweep)", completed)
            )

        metrics = _read_metrics(metrics_path, "route (corner sweep)")
    worst_setup_raw = metrics.get("timing__setup__ws")
    worst_hold_raw = metrics.get("timing__hold__ws")
    worst_setup = round(worst_setup_raw, 5) if worst_setup_raw is not None else None
    worst_hold = round(worst_hold_raw, 5) if worst_hold_raw is not None else None
    # Issue #1866: the combined session's own TNS pair, read the same way as
    # the worst-slack pair above -- used only in the `len(corners) == 1`
    # branch below (the combined aggregate *is* that single corner's own
    # value there); the `len(corners) > 1` loop below reads each corner's
    # own single-corner invocation's TNS instead.
    tns_setup_raw = metrics.get("timing__setup__tns")
    tns_hold_raw = metrics.get("timing__hold__tns")
    tns_setup = round(tns_setup_raw, 5) if tns_setup_raw is not None else None
    tns_hold = round(tns_hold_raw, 5) if tns_hold_raw is not None else None

    # Issue #1709: the design-rule verdict at the swept decks. Scraped from
    # this same combined invocation's own stdout -- with every swept corner
    # loaded, `report_check_types` reports a pin that violates the limit at
    # *any* of them, the same worst-case-across-loaded-corners semantics
    # `report_worst_slack_metric` above already has.
    max_transition_violations = _count_violations(
        completed.stdout,
        _MAX_TRANSITION_VIOLATIONS_BEGIN,
        _MAX_TRANSITION_VIOLATIONS_END,
    )
    max_capacitance_violations = _count_violations(
        completed.stdout,
        _MAX_CAPACITANCE_VIOLATIONS_BEGIN,
        _MAX_CAPACITANCE_VIOLATIONS_END,
    )

    if len(corners) == 1:
        # The combined session's own aggregate already *is* this single
        # corner's own value -- no second invocation needed (see docstring).
        corner_breakdown = [
            {
                "name": corners[0]["name"],
                "setup_slack_ns": worst_setup,
                "hold_slack_ns": worst_hold,
                "total_negative_setup_slack_ns": tns_setup,
                "total_negative_hold_slack_ns": tns_hold,
                # Issue #1865: per-corner counterpart of the top-level
                # `timing_status` -- a corner whose slack pair is OpenSTA's
                # unconstrained sentinel never looks like a clean corner.
                "timing_status": _timing_status((worst_setup, worst_hold)),
                "max_transition_violation_count": max_transition_violations,
                "max_capacitance_violation_count": max_capacitance_violations,
            }
        ]
        return (
            worst_setup,
            worst_hold,
            corner_breakdown,
            max_transition_violations,
            max_capacitance_violations,
        )

    corner_breakdown = []
    for corner in corners:
        corner_name = corner["name"]
        corner_script_path = os.path.join(
            output_dir, f"pnr_{hdl_toplevel}_route_corner_{corner_name}.tcl"
        )
        corner_metrics_path = os.path.join(
            output_dir, f"{hdl_toplevel}_route_corner_{corner_name}_metrics.json"
        )
        corner_lines = _corner_sweep_script_lines(
            checkpoint_in=checkpoint_in,
            corners=[corner],
            io_spec=io_spec,
            clock_port=clock_port,
            clock_period_ns=clock_period_ns,
            max_transition_ns=max_transition_ns,
            max_capacitance_pf=max_capacitance_pf,
            max_fanout=max_fanout,
            input_delay_ns=input_delay_ns,
            output_delay_ns=output_delay_ns,
        )
        _write_script(corner_script_path, corner_lines)

        corner_stage_name = f"route (corner sweep: {corner_name})"
        corner_completed = _run_openroad(
            corner_script_path,
            corner_metrics_path,
            error_cls=PlaceAndRouteError,
            engine_logs=engine_logs,
        )
        with corner_completed.diagnostics(PlaceAndRouteError):
            if corner_completed.returncode != 0:
                raise PlaceAndRouteError(
                    _engine_error_message(corner_stage_name, corner_completed)
                )

            corner_metrics = _read_metrics(corner_metrics_path, corner_stage_name)
        corner_setup_raw = corner_metrics.get("timing__setup__ws")
        corner_hold_raw = corner_metrics.get("timing__hold__ws")
        # Issue #1866: this corner's own TNS pair, from this same
        # single-corner invocation's `report_tns_metric -setup`/`-hold`
        # (`_corner_sweep_script_lines`) -- the per-corner counterpart of
        # the worst-slack pair above, exactly like the design-rule verdict
        # below is.
        corner_tns_setup_raw = corner_metrics.get("timing__setup__tns")
        corner_tns_hold_raw = corner_metrics.get("timing__hold__tns")
        corner_setup = (
            round(corner_setup_raw, 5) if corner_setup_raw is not None else None
        )
        corner_hold = round(corner_hold_raw, 5) if corner_hold_raw is not None else None
        corner_breakdown.append(
            {
                "name": corner_name,
                "setup_slack_ns": corner_setup,
                "hold_slack_ns": corner_hold,
                "total_negative_setup_slack_ns": (
                    round(corner_tns_setup_raw, 5)
                    if corner_tns_setup_raw is not None
                    else None
                ),
                "total_negative_hold_slack_ns": (
                    round(corner_tns_hold_raw, 5)
                    if corner_tns_hold_raw is not None
                    else None
                ),
                # Issue #1865: see the single-corner branch above.
                "timing_status": _timing_status((corner_setup, corner_hold)),
                # Issue #1709: this corner's *own* design-rule verdict, from
                # its own single-corner invocation's stdout -- the per-corner
                # counterpart of the combined aggregates above, letting a
                # caller see which deck's limits a pin actually breaks
                # (exactly the per-corner attribution #1092 added for slack).
                "max_transition_violation_count": _count_violations(
                    corner_completed.stdout,
                    _MAX_TRANSITION_VIOLATIONS_BEGIN,
                    _MAX_TRANSITION_VIOLATIONS_END,
                ),
                "max_capacitance_violation_count": _count_violations(
                    corner_completed.stdout,
                    _MAX_CAPACITANCE_VIOLATIONS_BEGIN,
                    _MAX_CAPACITANCE_VIOLATIONS_END,
                ),
            }
        )

    return (
        worst_setup,
        worst_hold,
        corner_breakdown,
        max_transition_violations,
        max_capacitance_violations,
    )

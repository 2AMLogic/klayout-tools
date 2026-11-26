"""Unconstrained-run policy for ``klt place-and-route`` (issue #5672) plus
the ``cts``-stage Tcl assembly both of its shapes share.

With neither ``constraints.clock_port`` nor ``constraints.clock_period_ns``,
a run past ``floorplan`` is unconstrained, not an error: every timing-driven
pass is skipped and ``timing_status`` reads ``"unconstrained"`` -- so a
timing-free (structural) Liberty can still reach ``place``/``route``.

These helpers live outside ``place_and_route.py``/``place_and_route_script.py``
to keep those files under their size ceilings (the ``place_and_route_hints.py``
precedent, issue #4914); moving the ``cts`` Tcl assembly here also returns
``_stage_script_lines`` to its complexity-ratchet baseline. The stage order is
shared with request validation."""

from __future__ import annotations

import os

from .place_and_route_base import STAGE_ORDER


def run_is_unconstrained(clock_port: str | None, clock_period_ns: float | None) -> bool:
    """True when the request defines no clock at all (issue #5672)."""
    return clock_port is None and clock_period_ns is None


def resizer_pass_lines(unconstrained: bool) -> list[str]:
    """The ``place`` stage's timing-driven resizer passes. They have nothing
    to work from when no ``create_clock`` was emitted above, so under an
    unconstrained run (issue #5672) they are skipped."""
    if unconstrained:
        return []
    return ["repair_design", "repair_timing"]


def unconstrained_warnings(
    target_stage: str, clock_port: str | None, clock_period_ns: float | None
) -> list[str]:
    """Warn when a ``place``-or-later run has no clock (issue #5672): every
    timing-driven pass was skipped, so a caller who merely forgot
    ``constraints.clock_period_ns`` is told rather than silently handed an
    unoptimised result."""
    if not run_is_unconstrained(clock_port, clock_period_ns):
        return []
    if STAGE_ORDER.index(target_stage) < STAGE_ORDER.index("place"):
        return []
    skipped = ["global_placement -timing_driven", "repair_design", "repair_timing"]
    if STAGE_ORDER.index(target_stage) >= STAGE_ORDER.index("cts"):
        skipped += ["clock_tree_synthesis", "repair_timing -hold"]
    return [
        "unconstrained run: request.constraints.clock_period_ns was not "
        "given, so no clock was created and these passes did not run: "
        + ", ".join(skipped)
        + "; the clock net (if any) is routed as an ordinary signal net and "
        "timing_status is 'unconstrained'"
    ]


def cts_stage_lines(
    unconstrained: bool,
    buf_cell: str,
    ports: tuple[str, ...],
    wire_rc_lines: list[str],
    output_dir: str,
    hdl_toplevel: str,
) -> list[str]:
    """Only clock nets with registered fanout are candidates for TritonCTS (#1506)."""
    if unconstrained:
        return [
            *wire_rc_lines,
            "estimate_parasitics -placement",
            'puts "klt place-and-route: no clock constraint (request.'
            "constraints.clock_period_ns omitted) -- skipping "
            'clock_tree_synthesis and repair_timing -hold"',
            "detailed_placement",
            f"write_def {os.path.join(output_dir, f'{hdl_toplevel}.cts.def')}",
        ]
    net_filter = " -clk_nets $klt_cts_nets" if len(ports) > 1 else ""
    return [
        *wire_rc_lines,
        "estimate_parasitics -placement",
        "set klt_cts_nets {}",
        "foreach clk [all_clocks] {",
        "    if {[llength [all_registers -clock $clk]] > 0} {",
        "        set term [[ord::get_db_block] findBTerm [get_name $clk]]",
        "        lappend klt_cts_nets [[$term getNet] getName]",
        "    }",
        "}",
        "if {[llength $klt_cts_nets] > 0} {",
        f"clock_tree_synthesis -root_buf {buf_cell} -buf_list {buf_cell} "
        f"-sink_clustering_enable -obstruction_aware{net_filter}",
        # Post-CTS parasitics must be re-estimated (the clock tree just
        # added real buffers/wire) *before* hold repair runs -- hold
        # slack is only meaningful once a real clock tree (with real
        # skew) exists, the general reason production flows run
        # hold-fixing immediately after CTS, not before (survey
        # section 2.7/3.2, `docs/design/place-and-route-improvements-
        # survey.md`). `repair_timing -hold` inserts hold buffers,
        # which `detailed_placement` below then legalizes alongside
        # CTS's own buffers -- one legalization pass covers both.
        "estimate_parasitics -placement",
        "repair_timing -hold",
        "} else {",
        'puts "klt place-and-route: declared clocks have no '
        "sequential (registered) fanout -- skipping "
        'clock_tree_synthesis (see issue #1506)"',
        "}",
        # `report_clock_skew_metric -setup` -- the caller appends it via
        # `_metrics_report_lines(include_clock_skew=True)` right after this
        # list -- is safe to run either way -- live-verified: it does not
        # error when no clock tree was built in the `else` branch above, it
        # simply reports on the (unbuffered) ideal clock same as it would
        # if this stage never ran at all.
        "detailed_placement",
        # A post-CTS, still-unrouted DEF (issue #1826, reversing #785's own
        # "internal artifact only" decision for the `"place"`-stage DEF in
        # place_and_route_script -- this one is surfaced in the response
        # from the start, never internal-only). Written unconditionally,
        # exactly like that `"place"`-stage `place_def_path` write --
        # `run_place_and_route` only threads it into `unrouted_def_path`
        # when `target_stage` is `"cts"` itself (mirroring `def_path`'s own
        # "only when this is the actual target" convention), but the file
        # always exists on disk once this stage runs, the same "no dead
        # branch" reasoning `place_def_path` already established.
        f"write_def {os.path.join(output_dir, f'{hdl_toplevel}.cts.def')}",
    ]

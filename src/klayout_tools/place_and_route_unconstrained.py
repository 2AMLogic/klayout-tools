"""Unconstrained-run policy for ``klt place-and-route`` (issue #5672) plus
the ``cts``-stage Tcl assembly both of its shapes share.

With neither ``constraints.clock_port`` nor ``constraints.clock_period_ns``,
a run past ``floorplan`` is unconstrained, not an error: every timing-driven
pass is skipped and ``timing_status`` reads ``"unconstrained"`` -- so a
timing-free (structural) Liberty can still reach ``place``/``route``.

These helpers live outside ``place_and_route.py``/``place_and_route_script.py``
to keep those files under their size ceilings (the ``place_and_route_hints.py``
precedent, issue #4914); moving the ``cts`` Tcl assembly here also returns
``_stage_script_lines`` to its complexity-ratchet baseline. ``STAGE_ORDER``
lives in ``place_and_route``, which imports this module, so it is imported
lazily where needed -- the same cycle-avoidance
``place_and_route_request``'s target-stage validation uses."""

from __future__ import annotations

import os


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
    # cycle: place_and_route imports this module (see module docstring)
    from .place_and_route import STAGE_ORDER

    if STAGE_ORDER.index(target_stage) < STAGE_ORDER.index("place"):
        return []
    skipped = ["repair_design", "repair_timing"]
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
    cts_clock: str,
    wire_rc_lines: list[str],
    output_dir: str,
    hdl_toplevel: str,
) -> list[str]:
    """The ``cts`` stage in both shapes. With no clock (issue #5672) there
    is no clock tree to build -- a timing-free Liberty cannot characterise
    a CTS buffer anyway (TritonCTS CTS-0111) -- so the stage is a
    ``puts``-only no-op and the clock routes as an ordinary signal net;
    with a clock it probes for registered sinks, builds and buffers the
    clock tree, then hold-repairs and legalizes. Both Tcl assemblies
    moved verbatim out of ``place_and_route_script._stage_script_lines``
    to keep that file's size ceiling and bring the function back under
    its complexity-ratchet baseline; ``buf_cell``/``cts_clock`` are
    resolved by the caller, which owns the ``library.cts_buffer``
    override and the virtual-clock fallback (both unused but safe to
    resolve when unconstrained -- the same eager per-PDK lookups the
    ``route`` branch already performs on clockless runs)."""
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
    return [
        *wire_rc_lines,
        "estimate_parasitics -placement",
        # Issue #1506: `clock_tree_synthesis` (TritonCTS) segfaults --
        # confirmed live (a real `openroad/orfs:latest` container,
        # `26Q3-1510-g6cb3f2b704`, against a real sky130A liberty/LEF
        # pair) reproducing the reported `exit 139` byte-for-byte, stack
        # trace bottoming out in `TritonCTS::separateMacroRegSinks` --
        # when `constraints.clock_port` names a real net with **zero**
        # fanout to any sequential (clocked) cell. This is a legitimate,
        # schema-forced input this command must survive: every stage
        # past `"floorplan"` requires `clock_port`/`clock_period_ns`
        # unconditionally, so a genuinely clockless, all-combinational
        # block still has to declare *some* clock net to reach `"cts"`/
        # `"route"` at all. `all_registers -clock [get_clocks ...]` (a
        # standard OpenSTA query -- already loaded by this stage's own
        # `read_liberty`/`read_db`) answers "does this clock drive any
        # sequential element" directly, without this module needing its
        # own liberty/netlist parser to classify cells as sequential --
        # OpenSTA already knows, from the same liberty this stage
        # already reads (live-verified: 0 registers for the zero-
        # fanout repro above, 2 for an otherwise-identical design with
        # two real `dfxtp` sinks on the same clock). Zero registered
        # sinks -> skip `clock_tree_synthesis` entirely as a clean
        # no-op and continue -- matching the script module's own existing
        # precedent of leaning on OpenROAD/OpenSTA as the authority
        # rather than re-deriving what it already knows (see the
        # place_and_route_script docstring, "Macro-pin routability cross-check").
        f"set _klt_cts_seq_sinks [llength [all_registers -clock "
        f"[get_clocks {{{cts_clock}}}]]]",
        "if {$_klt_cts_seq_sinks > 0} {",
        # `-sink_clustering_enable -obstruction_aware` (P&R survey
        # section 3.4, issue #783): TritonCTS clusters nearby sinks
        # under a shared buffer instead of one buffer per sink, and
        # routes the clock tree around placed macro/blockage
        # obstructions instead of ignoring them -- both are real
        # TritonCTS flags on this OpenROAD version, confirmed live via
        # `info body clock_tree_synthesis` against a real
        # `openroad/orfs:latest` container (`26Q3-1080-gab6fd26351`).
        # `-balance_levels` was evaluated and deliberately **not**
        # added: the same introspection shows OpenROAD now treats it as
        # obsolete (`utl::warn CTS 132 "-balance_levels is obsolete."`)
        # -- passing it would only emit a warning, a no-op flag this
        # command's Tcl generator has no reason to carry.
        f"clock_tree_synthesis -root_buf {buf_cell} -buf_list {buf_cell} "
        "-sink_clustering_enable -obstruction_aware",
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
        f'puts "klt place-and-route: clock {cts_clock} has no '
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

"""Synthesize RTL against a standard-cell liberty via Yosys, headless.

Pure library: :func:`run_synthesize` returns plain Python data (a ``dict`` of
JSON-serialisable primitives) and never prints, mirroring ``lvs.py``/
``sim.py``. Serialisation and human-readable formatting live in the CLI
command module (``cli/synthesize_cmd.py``).

This is Phase 2 of [Epic #391](https://github.com/2AMLogic/klayout-tools/issues/391)
("adopt the digital engine class -- Yosys + OpenROAD -- RTL->GDS as a first-
class ``klt`` flow"), the build carried by the two accepted Phase 1 spikes --
read them first:

- ``docs/design/yosys-synthesis-spike.md`` (#396) settles the invocation
  shape (a generated ``.ys`` script, never the ``-S`` shortcut or an
  ever-growing ``-p`` string) and the output-parsing recipe
  (``stat -liberty <lib> -json -top <top>`` captured via ``tee -q -o
  <path>``, never re-derived by parsing ``write_verilog``'s netlist).
- ``docs/design/digital-flow-contracts-spike.md`` section 4 (#399) settles
  the request/response JSON contract and exit-code table this module
  implements.

Like ``klt lvs``/``klt sim``, ``klt synthesize`` takes a **request document**
(RTL sources plus PDK/liberty selection plus optional constraints is richer
than a flag line carries cleanly), not positional file args.

Engine: ``"yosys"`` (only value implemented today; ``request.engine`` exists
from day one so a later engine is an additive enum value, per the contract
spike section 2). Invoked as a subprocess (``yosys -s <script>``), never
in-process -- there is no Python binding for Yosys the way ``klayout.db``
already is for ``klt lvs``/``klt extract``.

Liberty resolution reuses ``klayout_tools.pdk.find_pdk``/
``list_cell_libraries`` -- the same ``libs_ref`` discovery ``klt pdk``/
``klt cells`` already use (Yosys survey section 0) -- **no new PDK-fetch
mechanism**. ``request.pdk`` carries only ``cell_library``/``corner`` (no
``variant``/``root``): the resolved PDK install is whatever ``find_pdk()``'s
own default search order (``$PDK_ROOT``/``$PDK``, then the ciel/volare
stores, then the conventional prefixes) finds, exactly as ``klt pdk find``
with no flags would -- unless the CLI's own ``--pdk``/``--pdk-root`` flags
(mirroring ``klt extract``'s identical pair) pin a specific installed
variant/root, threaded straight through to :func:`run_synthesize`'s
``pdk_variant``/``pdk_root`` keyword args and from there to ``find_pdk()``.
``cell_library`` is never restricted to a single PDK family -- any
standard-cell library the resolved install ships a ``libs_ref`` entry for
resolves the same way, sky130's ``sky130_fd_sc_hd`` included only as this
module's first-proven example. When that install has no matching liberty at
all, this
raises a clear "liberty not found for deck" :class:`SynthesizeError` --
matching ``klt drc``'s existing "deck requires an asset the resolved install
doesn't ship" posture (the Yosys survey's own open question, resolved here).
When ``request.pdk.corner`` is omitted, the nominal (typical-process,
room-temperature) corner ``klt pdk cells``' own ``nominal_corner`` selection
already picks is used, via :func:`klayout_tools.pdk.list_cell_libraries`.

``timing`` carries ABC's own ``stime -p`` critical-path estimate (issue
#807, Epic #704 Phase 1a). The Yosys survey section 3.5/3.6 finding stands
unchanged -- *Yosys*'s own ``sta``/``ltp`` passes still produce nothing
usable against a liberty-mapped netlist -- but the conclusion drawn from it
("no delay number is available from this flow at all") was too strong: ABC
prints one itself, inside the subprocess this module already runs, as long
as it is invoked with ``-constr`` (see
``docs/design/synthesize-qor-improvements-survey.md`` section 1.2/1.4). It
is a **pre-layout, wire-free** estimate (ABC reports ``WireLoad = "none"``),
never signoff STA -- hence the self-labelling ``timing`` object shape
documented in ``docs/cli/synthesize.md``, never a bare ``delay_ps`` float.
Signoff timing is still Phase 4's OpenROAD/OpenSTA step.

``sta`` carries a real, whole-netlist critical-path report from
``klt_statime_native`` (``native/statime/``, issue #809, wired in by issue
#925 -- Epic #704 Phase 3), computed via :func:`klayout_tools.sta.
compute_critical_path` against the exact ``write_verilog -noattr`` netlist
this module already produces. Unlike ``timing`` (ABC's combinational-cone-
only estimate above, unaffected by this addition -- a purely additive
sibling field, never a replacement), ``sta`` walks the *whole* mapped
netlist's rise/fall-aware timing graph and reports the worst path (register-
to-register, register-to-port, or port-to-port, whichever is globally
worst) with its full per-hop cell breakdown, plus the worst pure
register-to-register path separately when the design has registers. It uses
the same uniform, SDC-free boundary condition (``0.05`` ns input transition,
``0.03`` pF output load on every primary input/output) the spike's own
``native/statime/README.md`` accuracy comparison used -- **not**
``synthesize.py``'s own :data:`_ABC_CONSTR_INPUTS` table, a different knob
in different units (see ``klayout_tools/sta.py``'s module docstring).
``sta`` is ``None`` -- never a fabricated number -- when the
``klt_statime_native`` extension is not installed (an optional Rust
toolchain, like every other native ``klt`` engine) or when the engine could
not analyze this particular netlist/liberty pair; see ``docs/cli/
synthesize.md``'s ``sta`` section for the full caveat this inherits from the
spike unresolved (no wire delay/parasitics, no SDC).

Constant drivers are mapped to real tie cells (issue #854). Yosys leaves
``1'b0``/``1'b1`` constants as bare Verilog literals, which OpenSTA's
Verilog reader turns into ``zero_``/``one_`` nets that OpenROAD types
``GROUND``/``POWER`` and TritonRoute then refuses to route (``[ERROR
DRT-0305] … is not routable by TritonRoute``) -- so a netlist with a bare
constant is un-place-and-routable end to end. The generated script therefore
runs Yosys's ``hilomap`` pass with the resolved library's own tie-high/
tie-low cells (:data:`_TIE_CELLS`, ORFS's ``TIEHI_CELL_AND_PORT``/
``TIELO_CELL_AND_PORT`` values), exactly as ORFS's own ``synth.tcl`` does.

**Acceptance gate**: :func:`run_synthesize`'s ``verify_equivalence`` flag
(the CLI's ``--verify-equivalence``, default off/additive) wires the just-
produced netlist through ``klt equiv``
(:func:`klayout_tools.equiv.run_equiv`, #726) against the same source RTL
this run just synthesized -- a synthesized netlist is not considered done
until ``klt equiv`` reports it ``"equivalent"`` to its own RTL. A non-
equivalent or inconclusive verdict is a hard :class:`SynthesizeError`, never
a silent warning. Combinational designs only, matching ``klt equiv``'s own
Phase 0 scope (#707) -- see ``docs/cli/synthesize.md``'s "Equivalence gate"
section. This is Phase 1 of Epic #704.

``restructure_timing`` (the CLI's ``--restructure-timing`` flag; issue #926,
Epic #704 Phase 3) runs :func:`klayout_tools.restructure.restructure_for_timing`
-- a bounded cell-resizing loop -- against the just-produced ``sta`` critical
path whenever it exceeds ``constraints.clock_period_ns``, and reports the
outcome in the response's ``restructuring`` field. Off by default
(additive/opt-in, same posture as ``verify_equivalence``); requires both
``constraints.clock_period_ns`` (the target to restructure against) and a
working ``sta`` stage (the optional ``klt_statime_native`` extension) -- see
:func:`_run_timing_restructuring` and ``docs/cli/synthesize.md``'s
"Timing-driven restructuring" section. Any resize actually applied is
validated by ``klt equiv`` against the same source RTL before it is ever
handed back -- a restructured netlist that cannot be proven equivalent is a
hard failure, never a silent "trust me" (acceptance criterion 3).

``structural`` (issue #1588) is an **always-present** additive verdict built
from the same Yosys ``synth``/``stat`` run this module already performs --
no extra Yosys invocation. ``latches``/``unexpected_latches`` come from
counting ``stat -json``'s own ``num_cells_by_type`` entries whose cell-type
name contains ``"dlatch"`` (case-insensitive): ``dfflibmap`` maps only
flip-flops (verified against ``yosys -p 'help dfflibmap'``), never latches,
so an inferred latch survives, unmapped, all the way to this module's own
final ``stat``/``write_verilog`` step, as a bare gate-level primitive
(``$_DLATCH_P_`` and siblings). ``comb_loops``/``multi_driven`` come from
parsing the captured Yosys run log for the ``Warning: found logic loop`` /
``Warning: multiple conflicting drivers`` lines ``synth -top <top>``'s own
internal ``check`` sub-stages already emit -- verified live that these must
be read from ``synth``'s *own* internal check (which runs before ABC's
loop-breaking heuristic silently severs a real combinational loop): an
*additional* ``check`` step run by this module *after* ``synth`` completes
finds zero problems on a design ``synth``'s own internal check already
flagged. See :func:`_compute_structural`. This reverses this module's own
prior "no exit code 3" design decision (`docs/design/
digital-flow-contracts-spike.md` section 4) -- see ``docs/cli/
synthesize.md``'s "Exit codes" section for the current contract.

``warnings`` (issue #1588) is a bounded, deterministic summary of every
``Warning: `` line in the same captured log -- grouped into a small,
sorted category taxonomy (never the raw log itself), so a caller gets a
signal without wading through interleaved Yosys pass output. See
:func:`_summarize_warnings`.

``baseline`` (issue #1588, optional) compares this run's own
``instance_count``/``area_um2``/critical-path number against a prior run,
named by ``request.baseline.response_path`` (a previously captured ``klt
synthesize --format json`` response file) or ``request.baseline.
netlist_path`` (a bare prior netlist, re-``stat``/re-timed against this
run's own resolved liberty). ``None`` unless one of those two request
fields is given. See :func:`_compute_baseline`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any

from ._paths import _load_request_json, validate_request_shape
from ._provenance import _combined_content_hash, _yosys_version, build_provenance
from .equiv import EquivError, run_equiv
from .pdk import PdkNotFoundError, find_pdk, list_cell_libraries
from .restructure import RestructureError, restructure_for_timing
from .sta import StaError, compute_critical_path

#: Bumped only on a non-additive (breaking) change to this command's own
#: JSON shape -- see docs/json-contract.md.
SCHEMA_VERSION = 1

#: ``"yosys"`` is the only engine implemented today; the field is present in
#: the request from day one (contract spike section 2) so a later backend is
#: an additive enum value, never a contract-shape change.
SUPPORTED_ENGINES = ("yosys",)

#: ABC's own ``stime -p`` summary line, as it reaches the Yosys log (and,
#: via ``tee -q -o``, the captured ABC log this module parses):
#: ``ABC: WireLoad = "none"  Gates = 297 ( 5.7 %)   Cap = 6.2 ff ( 12.4 %)
#: Area = 1986.91 ( 66.3 %)   Delay = 2485.93 ps  ( 32.3 %)``. Verified live
#: (Yosys 0.68+48, sky130_fd_sc_hd, issue #807).
_ABC_STIME_RE = re.compile(
    r'WireLoad\s*=\s*"(?P<wire_load>[^"]*)".*?Delay\s*=\s*(?P<delay_ps>[\d.]+)\s*ps'
)

#: Per-cell-library ABC constraint inputs -- ``(set_driving_cell cell,
#: set_load femtofarads)`` -- written to the ``<top>_abc.constr`` file this
#: module passes as ``abc -constr``. Same not-derivable-from-the-install,
#: verified-not-guessed posture as ``place_and_route.py``'s
#: :data:`~klayout_tools.place_and_route._CTS_BUFFER_CELLS` /
#: :data:`~klayout_tools.place_and_route._ROUTING_LAYER_RANGE` tables
#: (issues #629/#637): a driving cell for the primary inputs and an output
#: load are properties of how a platform is *used*, not of the liberty file
#: itself. ORFS pins both per platform, as ``ABC_DRIVER_CELL`` /
#: ``ABC_LOAD_IN_FF`` (read 2026-08-12 from
#: ``The-OpenROAD-Project/OpenROAD-flow-scripts`` @ ``master``), and builds
#: exactly this two-line file from them
#: (``flow/scripts/synth_preamble.tcl``: ``puts $constr "set_driving_cell
#: $::env(ABC_DRIVER_CELL)"`` / ``puts $constr "set_load
#: $::env(ABC_LOAD_IN_FF)"``). Sourced, per library:
#:
#: - ``sky130_fd_sc_hd`` -> ``sky130_fd_sc_hd__buf_1`` / ``5.0`` fF, both
#:   verbatim from ``platforms/sky130hd/config.mk`` (``ABC_DRIVER_CELL =
#:   sky130_fd_sc_hd__buf_1``, ``ABC_LOAD_IN_FF = 5``). Liberty cross-check
#:   against the installed ``sky130_fd_sc_hd__tt_025C_1v80.lib``:
#:   ``sky130_fd_sc_hd__buf_1`` is present, and its own input pin
#:   ``A`` has ``capacitance : 0.0021030000`` in that liberty's
#:   ``capacitive_load_unit(1.0, "pf")`` -- i.e. 2.1 fF, so ORFS's ``5``
#:   is ~2.4 single-input loads and is unambiguously already the
#:   femtofarad figure Yosys's ``help abc`` documents ``set_load`` to take
#:   ("sets the load in femtofarads for each primary output").
#: - ``gf180mcu_fd_sc_mcu9t5v0`` -> ``gf180mcu_fd_sc_mcu9t5v0__buf_4`` /
#:   ``13.43`` fF. The cell is ``platforms/gf180/config.mk``'s own
#:   ``ABC_DRIVER_CELL = gf180mcu_fd_sc_mcu$(TRACK_OPTION)$(POWER_OPTION)__
#:   buf_4`` under that platform's defaults (``TRACK_OPTION ?= 9t``,
#:   ``POWER_OPTION ?= 5v0``) -- the same resolution
#:   ``_CTS_BUFFER_CELLS``'s gf180 entry already documents. The load is the
#:   one value in either table **not** copied verbatim, and the liberty
#:   cross-check is why: that `config.mk` sets ``ABC_LOAD_IN_FF = 0.01343``,
#:   which is the *picofarad* figure (the installed
#:   ``gf180mcu_fd_sc_mcu9t5v0__tt_025C_5v00.lib`` declares
#:   ``capacitive_load_unit(1, pf)`` and gives ``buf_4``'s own input pin
#:   ``I`` ``capacitance : 0.0137`` -- 13.7 fF, matching 0.01343 pF to
#:   within 2%). Taken literally as femtofarads it would be 13.43 *atto*
#:   farads, three orders of magnitude below any real pin in that library,
#:   so the unit-consistent value for Yosys's femtofarad-denominated
#:   ``set_load`` is 13.43 fF -- ORFS's own number, converted, not a guess.
#:
#: ORFS is **reference data only**, never a runtime dependency -- nothing
#: here shells out to, reads, or requires an ORFS checkout, and no ORFS file
#: is vendored. A ``cell_library`` with no entry keeps this command's
#: pre-#807 behaviour exactly (no ``-constr``, no sizing/buffering, no
#: ``timing``), rather than guessing a driving cell for it.
_ABC_CONSTR_INPUTS: dict[str, tuple[str, float]] = {
    "sky130_fd_sc_hd": ("sky130_fd_sc_hd__buf_1", 5.0),
    "gf180mcu_fd_sc_mcu9t5v0": ("gf180mcu_fd_sc_mcu9t5v0__buf_4", 13.43),
}

#: Per-cell-library ``abc -dont_use`` glob list: the non-logic cell classes
#: a real flow keeps out of a mapped netlist. Same sourcing discipline as
#: :data:`_ABC_CONSTR_INPUTS` above -- each entry is ORFS's own
#: ``DONT_USE_CELLS`` for that platform, cross-checked against the installed
#: liberty's actual cell list (read 2026-08-12 from
#: ``The-OpenROAD-Project/OpenROAD-flow-scripts`` @ ``master``):
#:
#: - ``sky130_fd_sc_hd`` -> ``sky130_fd_sc_hd__lpflow_*`` (multi-power-domain
#:   isolation/level-shifter/decap cells) and ``sky130_fd_sc_hd__probe*``
#:   (test-probe cells with metal shapes on every layer).
#:   ``platforms/sky130hd/config.mk`` spells its ``DONT_USE_CELLS`` out as
#:   36 explicit cell names with exactly that intent (its own comment: "The
#:   *probe* are for inserting probe points and have metal shapes on all
#:   layers. *lpflow* cells are for multi-power domains"). Verified against
#:   the installed ``sky130_fd_sc_hd__tt_025C_1v80.lib``: the two globs
#:   above match **exactly** those 36 cells -- no cell ORFS excludes is
#:   missed, and no additional cell is caught (set difference empty in both
#:   directions). Globs are used rather than the 36 names because
#:   ``abc -dont_use`` supports them ("supports simple glob patterns in the
#:   cell name", ``help abc``) and because they stay correct if a future
#:   open_pdks release adds another ``lpflow_``/``probe`` cell.
#: - ``gf180mcu_fd_sc_mcu9t5v0`` -> **deliberately absent**, and this is the
#:   one place this table does not simply mirror ORFS. That platform's
#:   ``DONT_USE_CELLS = *_1`` (``platforms/gf180/config.mk``) excludes every
#:   *minimum-drive* cell in the library -- 62 of the installed
#:   ``gf180mcu_fd_sc_mcu9t5v0__tt_025C_5v00.lib``'s 229 cells -- which is a
#:   drive-strength policy for P&R congestion ("Dont use cells to ease
#:   congestion", that `config.mk`'s own comment), not the "keep non-logic
#:   cells out of the netlist" exclusion this table exists for. The QoR
#:   survey section 3.2 flags exactly this asymmetry and warns that the two
#:   libraries must not be given the same treatment by analogy; measured
#:   live here rather than assumed (issue #807, 8x8 multiplier, Yosys
#:   0.68+48): applying it costs **+31% area** (9217.96 -> 12119.39 um^2)
#:   and **+12% delay** (6150.64 -> 6887.58 ps) against the same run with
#:   ``-constr`` and no exclusions. Every logic function does keep a
#:   higher-drive variant (zero functions lost entirely, checked live), so
#:   the exclusion is *safe* -- it is simply not a QoR win at this stage of
#:   the flow, and a synthesis-time default that costs a third of the area
#:   for a congestion benefit no step of this command can observe would be a
#:   guess dressed as sourcing. gf180 still gets its ``-constr`` entry
#:   above; a caller wanting ORFS's P&R-oriented exclusion can be served
#:   later by an explicit request field, measured on its own terms.
#:
#: A ``cell_library`` with no entry gets no ``-dont_use`` flags at all --
#: this command's pre-#807 behaviour -- rather than a guessed exclusion.
_ABC_DONT_USE_GLOBS: dict[str, tuple[str, ...]] = {
    "sky130_fd_sc_hd": (
        "sky130_fd_sc_hd__lpflow_*",
        "sky130_fd_sc_hd__probe*",
    ),
}

#: Per-cell-library tie-high/tie-low cell for Yosys's ``hilomap`` pass,
#: as ``((hi_cell, hi_port), (lo_cell, lo_port))`` -- issue #854.
#:
#: **Why this exists.** Yosys's ``synth``/``abc`` passes leave constant
#: drivers as bare Verilog literals (``assign q[5] = 1'h0;``, ``.D(1'h1)``).
#: OpenSTA's Verilog reader materialises one net per constant *value* when
#: it reads such a netlist -- conventionally named ``zero_``/``one_`` -- and
#: OpenROAD types those nets ``GROUND``/``POWER``. TritonRoute then refuses
#: them outright: ``[ERROR DRT-0305] Net zero_ of signal type GROUND is not
#: routable by TritonRoute. Move to special nets.`` So *any* design needing
#: a constant tie (i.e. almost any real design -- the repo's own ``gcd.v``
#: happening not to need one is the exception) was unroutable by ``klt
#: place-and-route`` end to end. ``hilomap`` replaces each constant driver
#: with a real, routable standard-cell instance before the netlist ever
#: leaves this command, so no such net is ever created downstream.
#:
#: This mirrors ORFS, which runs the same pass at the same point in its own
#: synthesis script (``flow/scripts/synth.tcl``: ``splitnets`` ->
#: ``opt_clean -purge`` -> ``hilomap -singleton -hicell {*}$::env(TIEHI_CELL
#: _AND_PORT) -locell {*}$::env(TIELO_CELL_AND_PORT)``), and the values below
#: are that flow's own ``TIEHI_CELL_AND_PORT``/``TIELO_CELL_AND_PORT`` per
#: platform, each cross-checked against the installed liberty (read
#: 2026-08-12 from ``The-OpenROAD-Project/OpenROAD-flow-scripts`` @
#: ``master``; liberty checks run against a real volare install):
#:
#: - ``sky130_fd_sc_hd`` -> ``sky130_fd_sc_hd__conb_1``, ports ``HI``/``LO``.
#:   One cell drives both constants: the installed
#:   ``sky130_fd_sc_hd__tt_025C_1v80.lib`` gives ``conb_1`` exactly two
#:   non-power pins, ``HI`` with ``function : "1"`` and ``LO`` with
#:   ``function : "0"`` (everything else is a ``pg_pin``). Present as
#:   ``MACRO sky130_fd_sc_hd__conb_1`` / ``CLASS CORE`` in that library's own
#:   LEF, and not matched by :data:`_ABC_DONT_USE_GLOBS`.
#: - ``gf180mcu_fd_sc_mcu9t5v0`` -> two **distinct** cells,
#:   ``gf180mcu_fd_sc_mcu9t5v0__tieh`` port ``Z`` (``function : "1"``) and
#:   ``gf180mcu_fd_sc_mcu9t5v0__tiel`` port ``ZN`` (``function : "0"``),
#:   confirmed in the installed ``gf180mcu_fd_sc_mcu9t5v0__tt_025C_5v00.lib``.
#:   Deliberately **not** sky130's single dual-output shape carried over by
#:   analogy -- this library has no ``conb``-equivalent, and its own
#:   ``__filltie`` cell is a well-tie filler, not a logic constant driver.
#:
#: **Deviation from ORFS: no ``-singleton``.** ORFS collapses every constant
#: in the design onto one tie-hi and one tie-lo instance, then splits that
#: (potentially design-wide) fanout back out at floorplan time with
#: ``repair_tie_fanout``, which only ever *duplicates cells that already
#: exist* (``Resizer::repairTieFanout`` iterates
#: ``findCellInstances(tie_cell, …)``). ``klt place-and-route`` runs no such
#: step, so ``-singleton`` here would hand P&R a single net with every
#: constant load on it. Yosys's default -- one tie instance per constant bit
#: -- distributes that fanout at the only stage this flow can, and any
#: residual high-fanout tie net is still picked up by the ``place`` stage's
#: existing ``repair_design``.
#:
#: A ``cell_library`` with no entry gets **no** ``hilomap`` pass at all
#: (byte-identical script to before #854) rather than a guessed cell name --
#: the same graceful degradation :data:`_ABC_CONSTR_INPUTS`/
#: :data:`_ABC_DONT_USE_GLOBS` already apply.
_TIE_CELLS: dict[str, tuple[tuple[str, str], tuple[str, str]]] = {
    "sky130_fd_sc_hd": (
        ("sky130_fd_sc_hd__conb_1", "HI"),
        ("sky130_fd_sc_hd__conb_1", "LO"),
    ),
    "gf180mcu_fd_sc_mcu9t5v0": (
        ("gf180mcu_fd_sc_mcu9t5v0__tieh", "Z"),
        ("gf180mcu_fd_sc_mcu9t5v0__tiel", "ZN"),
    ),
}

#: Matches every gate-level latch primitive/cell-type name Yosys or a
#: liberty leaves behind (``$_DLATCH_P_``, ``$_DLATCHSR_PPP_``, ``$dlatch``,
#: a liberty cell like ``sky130_fd_sc_hd__dlrtp_1``'s underlying
#: ``dlatch``-family class name if ever surfaced) -- case-insensitive since
#: Yosys's own internal primitives are all-caps and liberty cell names are
#: typically lowercase. Used by :func:`_compute_structural` to total
#: ``structural.latches`` from ``stat -json``'s ``num_cells_by_type``,
#: issue #1588.
_LATCH_CELL_TYPE_RE = re.compile(r"dlatch", re.IGNORECASE)

#: The exact header lines Yosys's own ``check`` pass prints for the two
#: structural problems ``structural.comb_loops``/``multi_driven`` report --
#: verified live (Yosys 0.68, issue #1588): ``check`` (run unconditionally,
#: twice, inside every ``synth -top <top>`` invocation's own ``coarse``/
#: ``check`` sub-stages -- ``yosys -p 'help synth'``) prints one such line
#: per distinct problem, each followed by non-``Warning:``-prefixed detail
#: lines this module does not need to parse.
_COMB_LOOP_WARNING_RE = re.compile(r"^Warning: found logic loop in module\b")
_MULTI_DRIVEN_WARNING_RE = re.compile(r"^Warning: multiple conflicting drivers for\b")

#: ``(category, pattern)`` taxonomy :func:`_summarize_warnings` groups every
#: ``Warning: `` line in the captured Yosys run log into -- a small,
#: hand-picked set of the messages this module's own docstring already
#: names as structurally meaningful, plus a catch-all ``"other"`` bucket for
#: everything else. Patterns are matched against the warning text with the
#: leading ``"Warning: "`` prefix already stripped.
_WARNING_CATEGORY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("latch_inferred", re.compile(r"^Latch inferred\b", re.IGNORECASE)),
    ("logic_loop", re.compile(r"^found logic loop\b", re.IGNORECASE)),
    ("multiple_drivers", re.compile(r"^multiple conflicting drivers\b", re.IGNORECASE)),
    ("undriven_wire", re.compile(r"\bis used but has no driver\b", re.IGNORECASE)),
)

#: Bound on ``warnings.representatives`` -- one entry per category, capped
#: so a design with a large, varied warning taxonomy never turns this field
#: into an unbounded list (issue #1588's "bounded ... never the raw log"
#: requirement).
_MAX_WARNING_REPRESENTATIVES = 10


class SynthesizeError(Exception):
    """Raised when a synthesis run cannot even be attempted: a missing/
    malformed request file, an unresolvable/unreadable RTL source, an
    unresolvable ``pdk.cell_library``/``corner`` (no matching liberty via
    ``find_pdk()``), an elaboration/hierarchy error, or a Yosys/ABC engine
    error.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback -- either a netlist is produced, or this is raised; there is
    no third "ran but the request itself was unusable" outcome. This is
    unaffected by issue #1588's additive ``structural`` verdict: a run that
    *does* produce a netlist but finds an inferred latch, a combinational
    loop, or a multiply-driven net still returns normally (``status: "ok"``)
    with ``structural.has_critical: true`` and exit code ``3`` -- never this
    exception. See ``docs/cli/synthesize.md``'s "Exit codes" section.
    """


def load_request(request_path: str) -> dict[str, Any]:
    """Read and minimally validate a ``klt synthesize`` request JSON file.

    Raises :class:`SynthesizeError` if the file is missing/unreadable, not
    valid JSON, or missing a required top-level field (``sources``,
    ``hdl_toplevel``, ``pdk``). Does not require a ``schema`` field,
    matching ``klt lvs``/``klt sim``'s ``load_request`` (user-authored
    input, never emitted by this tool).
    """
    request = _load_request_json(request_path, SynthesizeError)
    return validate_request_shape(
        request,
        "request file",
        error_cls=SynthesizeError,
        required_fields=("sources", "hdl_toplevel", "pdk"),
    )


def run_synthesize(
    request_path: str,
    *,
    pdk_variant: str | None = None,
    pdk_root: str | None = None,
    verify_equivalence: bool = False,
    equiv_timeout_s: float | None = None,
    restructure_timing: bool = False,
    restructure_max_iterations: int | None = None,
) -> dict[str, Any]:
    """Run the Yosys synthesis declared by the request at ``request_path``.

    ``pdk_variant``/``pdk_root`` (the CLI's ``--pdk``/``--pdk-root`` flags,
    mirroring ``klt extract``'s identical pair) optionally pin a specific
    installed PDK variant/root, passed straight through to
    :func:`_resolve_liberty`'s own ``find_pdk()`` call. ``None`` (the
    default) leaves ``find_pdk()``'s own default search order
    (``$PDK_ROOT``/``$PDK``, then the ciel/volare stores, then the
    conventional prefixes) in effect, unchanged from before this parameter
    existed.

    ``verify_equivalence`` (the CLI's ``--verify-equivalence`` flag; default
    ``False``, additive/opt-in -- unchanged behaviour for every existing
    caller) gates the produced netlist through ``klt equiv`` (:func:`
    klayout_tools.equiv.run_equiv`) against the same ``sources``/
    ``hdl_toplevel`` this request just synthesized, before returning: the
    just-produced ``netlist_path`` (with ``liberty`` set to the same
    resolved liberty this synthesis run used, so standard-cell instances
    resolve as real logic rather than an undefined blackbox) is proven
    equivalent to the RTL that was fed into Yosys. A non-``"equivalent"``
    verdict (``"counterexample"`` or ``"inconclusive"``) -- or an
    :class:`~klayout_tools.equiv.EquivError`, e.g. a **sequential** design
    (this MVP's ``klt equiv`` is combinational-only -- see
    ``docs/cli/equiv.md``'s "Scope" section; a design containing flip-flops/
    latches/memories cannot use this flag today) -- is a hard
    :class:`SynthesizeError`, never a silent warning: a synthesized netlist
    this gate cannot prove faithful to its own source RTL is not "done".
    ``equiv_timeout_s`` (the CLI's ``--equiv-timeout-s``) overrides ``klt
    equiv``'s own default proof timeout; ``None`` leaves
    :data:`klayout_tools.equiv.DEFAULT_TIMEOUT_S` in effect. It also bounds
    the ``klt equiv`` check ``restructure_timing`` runs, below.

    ``restructure_timing`` (the CLI's ``--restructure-timing`` flag; issue
    #926, Epic #704 Phase 3) runs a bounded cell-resizing loop
    (:func:`klayout_tools.restructure.restructure_for_timing`) against the
    ``sta`` stage's ``worst_path`` whenever it exceeds
    ``constraints.clock_period_ns``. Requires ``constraints.clock_period_ns``
    to be set and the ``sta`` stage to have produced a result (the optional
    ``klt_statime_native`` extension must be installed) -- either being
    missing is a hard :class:`SynthesizeError`, since the flag was
    explicitly requested and there would be nothing to restructure against.
    Any resize the loop actually applies is validated by ``klt equiv``
    against the source RTL before the run returns (reusing the same
    combinational-only scope ``verify_equivalence`` has); a non-equivalent
    verdict is a hard failure, mirroring ``verify_equivalence``'s own "never
    a silent warning" discipline. See the response's ``restructuring`` field
    and ``docs/cli/synthesize.md``'s "Timing-driven restructuring" section.
    ``restructure_max_iterations`` overrides
    :data:`klayout_tools.restructure.DEFAULT_MAX_ITERATIONS`; ``None``
    (default) leaves that default in effect.

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/synthesize.md`` / ``docs/design/digital-flow-contracts-spike.md``
    section 4). Raises :class:`SynthesizeError` for anything that prevents a
    netlist from being produced at all -- bad request, unreadable RTL
    source, elaboration/hierarchy error, unresolvable ``pdk.cell_library``/
    ``corner``, a Yosys/ABC engine error, or (when ``verify_equivalence`` is
    set) a failed/inconclusive equivalence check.

    The generated ``.ys`` script, the mapped netlist, the captured stats
    JSON, and -- when the resolved ``cell_library`` has an
    :data:`_ABC_CONSTR_INPUTS` entry -- the generated ``<top>_abc.constr``
    file and captured ``<top>_abc.log`` are written to ``.klt/synthesize/``
    next to the request file (the same "next to the input" default ``klt
    sim``'s ``.klt/sim/`` artifacts directory already uses) and kept as
    debuggable artifacts, never deleted.
    """
    request = load_request(request_path)
    request_dir = os.path.dirname(os.path.abspath(request_path))

    engine = request.get("engine", "yosys")
    if engine not in SUPPORTED_ENGINES:
        raise SynthesizeError(
            f"unsupported engine '{engine}' (supported: {', '.join(SUPPORTED_ENGINES)})"
        )

    resolved_sources = _resolve_sources(request["sources"], request_dir)

    hdl_toplevel = request["hdl_toplevel"]
    if not isinstance(hdl_toplevel, str) or not hdl_toplevel:
        raise SynthesizeError("request.hdl_toplevel must be a non-empty string")

    pdk_spec = request["pdk"]
    if not isinstance(pdk_spec, dict):
        raise SynthesizeError("request.pdk must be a JSON object")
    cell_library = pdk_spec.get("cell_library")
    if not isinstance(cell_library, str) or not cell_library:
        raise SynthesizeError("request.pdk.cell_library is required")
    requested_corner = pdk_spec.get("corner")
    if requested_corner is not None and not (
        isinstance(requested_corner, str) and requested_corner
    ):
        raise SynthesizeError(
            "request.pdk.corner must be a non-empty string when given"
        )

    constraints = request.get("constraints")
    if constraints is not None and not isinstance(constraints, dict):
        raise SynthesizeError("request.constraints must be a JSON object")
    delay_target_ps = _resolve_delay_target_ps(constraints)
    expected_latches = _resolve_expected_latches(request)

    liberty_path, corner, pdk_info = _resolve_liberty(
        cell_library, requested_corner, variant=pdk_variant, root=pdk_root
    )

    output_dir = os.path.join(request_dir, ".klt", "synthesize")
    try:
        os.makedirs(output_dir, exist_ok=True)
    except OSError as exc:
        raise SynthesizeError(
            f"could not create output directory '{output_dir}': {exc}"
        ) from exc

    script_path = os.path.join(output_dir, f"synth_{hdl_toplevel}.ys")
    netlist_path = os.path.join(output_dir, f"{hdl_toplevel}_synth.v")
    stats_path = os.path.join(output_dir, f"{hdl_toplevel}_stats.json")

    constr_inputs = _ABC_CONSTR_INPUTS.get(cell_library)
    constr_path: str | None = None
    abc_log_path: str | None = None
    if constr_inputs is not None:
        constr_path = os.path.join(output_dir, f"{hdl_toplevel}_abc.constr")
        abc_log_path = os.path.join(output_dir, f"{hdl_toplevel}_abc.log")
        _write_constr(constr_path, *constr_inputs)

    dont_use_globs: tuple[str, ...] = ()
    if _ABC_DONT_USE_GLOBS.get(cell_library) and _abc_supports_dont_use():
        dont_use_globs = _ABC_DONT_USE_GLOBS[cell_library]

    _write_script(
        script_path=script_path,
        sources=resolved_sources,
        hdl_toplevel=hdl_toplevel,
        liberty_path=liberty_path,
        stats_path=stats_path,
        netlist_path=netlist_path,
        constr_path=constr_path,
        abc_log_path=abc_log_path,
        delay_target_ps=delay_target_ps,
        dont_use_globs=dont_use_globs,
        tie_cells=_TIE_CELLS.get(cell_library),
    )

    yosys_log = _run_yosys(script_path)

    if not os.path.isfile(netlist_path):
        raise SynthesizeError(
            f"yosys exited successfully but did not produce '{netlist_path}'"
        )

    module_stats = _read_stats(stats_path, hdl_toplevel)
    engine_version = _yosys_version()
    structural = _compute_structural(module_stats, yosys_log, expected_latches)
    warnings_summary = _summarize_warnings(yosys_log)

    deck_name = f"{cell_library}__{corner}"
    provenance = build_provenance(
        deck_name=deck_name,
        deck_path=liberty_path,
        pdk=pdk_info,
        input_path=resolved_sources[0] if len(resolved_sources) == 1 else None,
    )
    if len(resolved_sources) > 1:
        provenance["input"] = {"content_hash": _combined_content_hash(resolved_sources)}

    equivalence = None
    if verify_equivalence:
        equivalence = _verify_synthesis_equivalence(
            synthesize_output_dir=output_dir,
            resolved_sources=resolved_sources,
            hdl_toplevel=hdl_toplevel,
            netlist_path=netlist_path,
            liberty_path=liberty_path,
            timeout_s=equiv_timeout_s,
        )

    sta = _read_sta_timing(netlist_path, liberty_path, hdl_toplevel)

    restructuring = None
    if restructure_timing:
        restructuring = _run_timing_restructuring(
            netlist_path=netlist_path,
            liberty_path=liberty_path,
            hdl_toplevel=hdl_toplevel,
            delay_target_ps=delay_target_ps,
            sta=sta,
            output_dir=output_dir,
            resolved_sources=resolved_sources,
            max_iterations=restructure_max_iterations,
            equiv_timeout_s=equiv_timeout_s,
        )

    response: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "engine": engine,
        "engine_version": engine_version,
        "hdl_toplevel": hdl_toplevel,
        "status": "ok",
        "instance_count": module_stats["num_cells"],
        "area_um2": module_stats["area"],
        # ``sequential_area`` is only emitted by newer Yosys builds (0.67+);
        # distro-packaged Yosys (e.g. Ubuntu 24.04's 0.33) omits it from
        # `stat -json` entirely. Degrade to `None` rather than a raw
        # `KeyError` -- see #560.
        "sequential_area_um2": module_stats.get("sequential_area"),
        "instance_counts_by_type": dict(
            sorted((module_stats.get("num_cells_by_type") or {}).items())
        ),
        "timing": _read_abc_timing(abc_log_path, delay_target_ps),
        "sta": sta,
        "structural": structural,
        "warnings": warnings_summary,
        "netlist_path": netlist_path,
        "script_path": script_path,
        "provenance": provenance,
        "equivalence": equivalence,
        "restructuring": restructuring,
        "baseline": None,
    }
    response["baseline"] = _compute_baseline(
        request,
        request_dir=request_dir,
        response=response,
        liberty_path=liberty_path,
        hdl_toplevel=hdl_toplevel,
        output_dir=output_dir,
    )
    return response


def _verify_synthesis_equivalence(
    *,
    synthesize_output_dir: str,
    resolved_sources: list[str],
    hdl_toplevel: str,
    netlist_path: str,
    liberty_path: str,
    timeout_s: float | None,
    request_filename: str | None = None,
) -> dict[str, Any]:
    """The ``verify_equivalence`` gate :func:`run_synthesize` calls after a
    successful synthesis: reuses :func:`klayout_tools.equiv.run_equiv`'s
    existing request contract, with ``gold`` set to the same RTL ``sources``
    this synthesis run just read and ``gate`` set to the netlist it just
    produced (``liberty`` attached so the standard-cell instances resolve as
    real logic, not an undefined blackbox -- see
    :func:`klayout_tools.equiv._resolve_side`'s own docs).

    The equiv request is written to a real file under
    ``synthesize_output_dir`` (this run's own ``.klt/synthesize/`` -- never
    passed as an inline JSON string) so
    :func:`klayout_tools.equiv.run_equiv` resolves its own artifacts
    directory as ``.klt/synthesize/.klt/equiv/``, right alongside this run's
    own script/netlist -- rather than the process's current working
    directory (the inline-JSON form's own relative-path anchor, per
    :func:`klayout_tools.equiv.load_request_arg`'s docs), which would
    silently scatter equivalence-check artifacts somewhere unrelated to the
    request being synthesized. ``request_filename`` (default
    ``equiv_request_<hdl_toplevel>.json``) lets a second caller in the same
    run (:func:`_run_timing_restructuring`, checking a *different* netlist)
    use a distinct file rather than overwriting ``verify_equivalence``'s own.

    Returns a small summary dict (attached to the response's
    ``equivalence`` field) on an ``"equivalent"`` verdict. Raises
    :class:`SynthesizeError` -- a hard failure, never a silent warning --
    for every other outcome: a proven ``"counterexample"`` (the synthesized
    netlist diverges from its own source RTL), an ``"inconclusive"`` verdict
    (a solver/process timeout -- never treated as a pass), or an
    :class:`~klayout_tools.equiv.EquivError` (e.g. a sequential design,
    outside this MVP's combinational-only ``klt equiv`` scope).
    """
    equiv_request_path = os.path.join(
        synthesize_output_dir,
        request_filename or f"equiv_request_{hdl_toplevel}.json",
    )
    equiv_request = {
        "gold": {"sources": resolved_sources, "top": hdl_toplevel},
        "gate": {
            "sources": [netlist_path],
            "top": hdl_toplevel,
            "liberty": liberty_path,
        },
    }
    try:
        with open(equiv_request_path, "w", encoding="utf-8") as handle:
            json.dump(equiv_request, handle, indent=2)
    except OSError as exc:
        raise SynthesizeError(
            f"could not write equivalence-check request '{equiv_request_path}': {exc}"
        ) from exc

    try:
        equiv_report = run_equiv(equiv_request_path, timeout_s=timeout_s)
    except EquivError as exc:
        raise SynthesizeError(
            f"equivalence check against source RTL could not be completed: {exc}"
        ) from exc

    status = equiv_report["status"]
    if status != "equivalent":
        raise SynthesizeError(_equivalence_failure_message(status, equiv_report))

    return {
        "status": status,
        "engine": equiv_report["engine"],
        "engine_version": equiv_report["engine_version"],
        "timeout_s": equiv_report["timeout_s"],
        "elapsed_s": equiv_report["elapsed_s"],
        "artifacts": equiv_report["artifacts"],
    }


def _equivalence_failure_message(status: str, equiv_report: dict[str, Any]) -> str:
    """An actionable :class:`SynthesizeError` message for a non-
    ``"equivalent"`` ``klt equiv`` verdict against a just-produced netlist
    -- names the diverging outputs for a proven ``"counterexample"``, or the
    first diagnostic for an ``"inconclusive"`` (timeout) verdict."""
    log_path = equiv_report.get("artifacts", {}).get("log_path")
    if status == "counterexample":
        counterexample = equiv_report.get("counterexample") or {}
        diverging = counterexample.get("diverging_outputs") or []
        confirmed = counterexample.get("confirmed_by_simulation")
        detail = (
            f"diverging outputs: {', '.join(diverging)}"
            if diverging
            else "no diverging outputs reported"
        )
        message = (
            "synthesized netlist is NOT equivalent to its source RTL "
            f"(klt equiv reported 'counterexample'; {detail}; "
            f"confirmed_by_simulation={confirmed})"
        )
    else:
        diagnostics = equiv_report.get("diagnostics") or []
        detail = diagnostics[0]["message"] if diagnostics else "no diagnostic detail"
        message = (
            "equivalence check against source RTL did not reach a verdict "
            f"(klt equiv reported '{status}'): {detail}"
        )
    if log_path:
        message += f" -- see {log_path} for the full proof"
    return message


def _run_timing_restructuring(
    *,
    netlist_path: str,
    liberty_path: str,
    hdl_toplevel: str,
    delay_target_ps: int | None,
    sta: dict[str, Any] | None,
    output_dir: str,
    resolved_sources: list[str],
    max_iterations: int | None,
    equiv_timeout_s: float | None,
) -> dict[str, Any]:
    """The ``restructure_timing`` gate :func:`run_synthesize` calls after the
    ``sta`` stage: runs
    :func:`klayout_tools.restructure.restructure_for_timing`'s bounded
    cell-resizing loop against ``constraints.clock_period_ns``
    (``delay_target_ps``, converted back to nanoseconds -- the same unit
    :func:`_resolve_delay_target_ps` converted *from*), and, if it actually
    applied any resize, validates the result with ``klt equiv`` before
    returning (issue #926 acceptance criterion 3).

    Requires both ``delay_target_ps`` and ``sta`` to be present -- unlike
    ``sta`` itself (which degrades to ``None`` on a missing extension so a
    synthesis run is never broken by an *optional* stage), this flag was
    *explicitly requested*, so a missing target or a missing/failed ``sta``
    stage is a hard :class:`SynthesizeError` naming exactly what is missing,
    never a silent no-op.

    The restructured netlist (when any resize was applied) is written to
    ``<hdl_toplevel>_synth_restructured.v`` alongside this run's other
    ``.klt/synthesize/`` artifacts -- kept, like every other artifact this
    command writes, as a debuggable file, never deleted -- and echoed back
    as the report's own ``restructured_netlist_path``. This is the
    netlist-handoff contract acceptance criterion 5 asks this issue to
    document (see ``docs/cli/synthesize.md``'s "Timing-driven restructuring"
    section): a future ``klt par`` (#700, once it reaches its own timing
    phase) should prefer this path over the plain ``netlist_path`` whenever
    it is non-``null``, and fall back to ``netlist_path`` otherwise --
    exactly the same "additive sibling, never required" posture ``sta``
    already has relative to ``timing``.
    """
    if delay_target_ps is None:
        raise SynthesizeError(
            "restructure_timing requires request.constraints.clock_period_ns "
            "-- there is no target period to restructure against"
        )
    if sta is None:
        raise SynthesizeError(
            "restructure_timing requires a working sta stage, but it "
            "produced no result -- install the klt_statime_native "
            "extension (`uv sync --group statime`, or `maturin develop "
            "--release` inside native/statime/) or check that the mapped "
            "netlist/liberty pair can be analyzed"
        )

    target_period_ns = delay_target_ps / 1000.0
    output_netlist_path = os.path.join(
        output_dir, f"{hdl_toplevel}_synth_restructured.v"
    )
    kwargs: dict[str, Any] = {}
    if max_iterations is not None:
        kwargs["max_iterations"] = max_iterations

    try:
        report = restructure_for_timing(
            netlist_path,
            liberty_path,
            hdl_toplevel,
            target_period_ns,
            output_netlist_path=output_netlist_path,
            **kwargs,
        )
    except RestructureError as exc:
        raise SynthesizeError(f"timing restructuring failed: {exc}") from exc

    report["equivalence"] = None
    if report["resizes_applied"]:
        report["equivalence"] = _verify_synthesis_equivalence(
            synthesize_output_dir=output_dir,
            resolved_sources=resolved_sources,
            hdl_toplevel=hdl_toplevel,
            netlist_path=output_netlist_path,
            liberty_path=liberty_path,
            timeout_s=equiv_timeout_s,
            request_filename=f"equiv_request_{hdl_toplevel}_restructured.json",
        )

    return report


def _resolve_sources(sources: Any, request_dir: str) -> list[str]:
    """Validate ``request.sources`` and resolve each entry to an absolute,
    readable path (relative to ``request_dir``, the request file's own
    directory -- the same convention every other request-taking verb uses).

    Raises :class:`SynthesizeError` naming the offending entry for a missing
    or unreadable RTL source, matching this module's docstring's "unreadable
    RTL source" error scope.
    """
    if not isinstance(sources, list) or not sources:
        raise SynthesizeError("request.sources must be a non-empty array of paths")
    if not all(isinstance(entry, str) and entry for entry in sources):
        raise SynthesizeError("request.sources entries must be non-empty strings")

    resolved: list[str] = []
    for entry in sources:
        path = entry if os.path.isabs(entry) else os.path.join(request_dir, entry)
        if not os.path.isfile(path):
            raise SynthesizeError(f"RTL source not found: {entry}")
        try:
            with open(path, "rb"):
                pass
        except OSError as exc:
            raise SynthesizeError(
                f"could not read RTL source '{entry}': {exc}"
            ) from exc
        resolved.append(os.path.abspath(path))
    return resolved


def _resolve_liberty(
    cell_library: str,
    requested_corner: str | None,
    *,
    variant: str | None = None,
    root: str | None = None,
) -> tuple[str, str, dict[str, Any]]:
    """Resolve ``(liberty_path, corner, pdk_info)`` for ``cell_library``.

    ``variant``/``root`` (the CLI's ``--pdk``/``--pdk-root`` flags, threaded
    through from :func:`run_synthesize`) select a specific installed PDK
    variant/root exactly as :func:`klayout_tools.pdk.find_pdk` does; ``None``
    for either leaves that resolver's own default search order in effect.

    ``pdk_info`` is :func:`klayout_tools.pdk.find_pdk`'s own resolution dict
    -- passed straight through to :func:`build_provenance`'s ``pdk``
    argument. Raises :class:`SynthesizeError` (never
    :class:`~klayout_tools.pdk.PdkNotFoundError`) for every failure mode:
    no PDK install resolves at all, the resolved install ships no
    ``libs_ref``/``cell_library`` asset, or ``cell_library`` has no
    ``corner`` (explicit or nominal-default) liberty view -- the "liberty
    not found for deck" posture this module's docstring describes.
    """
    try:
        info = find_pdk(variant=variant, root=root)
    except PdkNotFoundError as exc:
        raise SynthesizeError(str(exc)) from exc

    libs_ref = info["assets"]["libs_ref"]
    if libs_ref is None:
        raise SynthesizeError(
            f"liberty not found for deck: resolved PDK install "
            f"'{info['variant']}' at '{info['root']}' ships no libs_ref asset"
        )

    lib_dir = os.path.join(libs_ref, cell_library)
    if not os.path.isdir(lib_dir):
        raise SynthesizeError(
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
            raise SynthesizeError(
                f"liberty not found for deck: could not determine a nominal "
                f"corner for '{cell_library}' -- pass request.pdk.corner "
                "explicitly"
            )

    liberty_path = os.path.join(lib_dir, "lib", f"{cell_library}__{corner}.lib")
    if not os.path.isfile(liberty_path):
        raise SynthesizeError(
            f"liberty not found for deck: no '{corner}' corner for "
            f"'{cell_library}' under resolved PDK install '{info['variant']}' "
            f"(expected '{liberty_path}')"
        )
    return liberty_path, corner, info


def _resolve_delay_target_ps(constraints: dict[str, Any] | None) -> int | None:
    """``constraints.clock_period_ns`` as an integer picosecond ABC delay
    target (``abc -D``), or ``None`` when the request does not supply one.

    ``None``/omitted is a deliberate, documented state, not an oversight
    (``docs/cli/synthesize.md``): the run still gets ``-constr`` -- so ABC's
    ``buffer``/``upsize``/``dnsize`` sizing steps and its ``stime -p`` report
    still run -- but ``&nf``/``upsize``/``dnsize`` optimize untargeted,
    exactly as they did before issue #807. Only the *target* is caller-
    supplied.

    Raises :class:`SynthesizeError` for a non-numeric or non-positive value
    -- a request that means to constrain the run but expresses it wrongly
    must not be silently downgraded to "unconstrained".
    """
    if not constraints:
        return None
    clock_period_ns = constraints.get("clock_period_ns")
    if clock_period_ns is None:
        return None
    if isinstance(clock_period_ns, bool) or not isinstance(
        clock_period_ns, (int, float)
    ):
        raise SynthesizeError(
            "request.constraints.clock_period_ns must be a positive number "
            "(nanoseconds) or null"
        )
    if clock_period_ns <= 0:
        raise SynthesizeError(
            "request.constraints.clock_period_ns must be greater than zero"
        )
    return int(round(clock_period_ns * 1000))


def _write_constr(constr_path: str, driving_cell: str, load_ff: float) -> None:
    """Write the two-line ABC constraint file ``abc -constr`` consumes --
    ``set_driving_cell <cell>`` / ``set_load <femtofarads>``, the exact
    shape ``help abc`` documents and ORFS's own
    ``flow/scripts/synth_preamble.tcl`` generates.

    Kept in ``.klt/synthesize/`` alongside the ``.ys`` script and the mapped
    netlist, as a debuggable artifact, never deleted.
    """
    try:
        with open(constr_path, "w", encoding="utf-8") as handle:
            handle.write(f"set_driving_cell {driving_cell}\nset_load {load_ff:g}\n")
    except OSError as exc:
        raise SynthesizeError(
            f"could not write ABC constraint file '{constr_path}': {exc}"
        ) from exc


def _abc_supports_dont_use() -> bool:
    """Whether the resolved Yosys build's ``abc`` pass accepts ``-dont_use``.

    Probed against ``yosys -p 'help abc'`` rather than inferred from a
    version number: measured live (issue #807), Yosys 0.68 documents
    ``-dont_use`` and Ubuntu 24.04's distro-packaged 0.33 does not mention
    it at all -- and passing an unknown option to ``abc`` is a hard Yosys
    error. Older builds therefore degrade to a run with no exclusion list
    (mapped exactly as before issue #807) rather than failing outright --
    the same graceful-degradation posture ``sequential_area_um2`` already
    takes toward those builds (#560). Never raises.
    """
    try:
        completed = subprocess.run(
            ["yosys", "-p", "help abc"], capture_output=True, text=True
        )
    except OSError:
        return False
    if completed.returncode != 0:
        return False
    return "-dont_use" in (completed.stdout or "")


def _write_script(
    *,
    script_path: str,
    sources: list[str],
    hdl_toplevel: str,
    liberty_path: str,
    stats_path: str,
    netlist_path: str,
    constr_path: str | None = None,
    abc_log_path: str | None = None,
    delay_target_ps: int | None = None,
    dont_use_globs: tuple[str, ...] = (),
    tie_cells: tuple[tuple[str, str], tuple[str, str]] | None = None,
) -> None:
    """Generate the ``.ys`` synthesis script (Yosys survey section 1's exact
    pass sequence: ``read_verilog`` -> ``hierarchy`` -> ``synth`` ->
    ``dfflibmap`` -> ``abc -liberty`` -> ``clean`` -> ``hilomap`` ->
    ``stat``/``write_verilog``) into ``script_path``.

    The ``abc`` line carries the issue #807 additions when the resolved
    ``cell_library`` has table entries for them:

    - ``-constr <file>`` whenever ``constr_path`` is given. This is what
      unlocks ABC's ``buffer``/``upsize``/``dnsize`` sizing-and-buffering
      steps *and* its ``stime -p`` timing report -- all four are
      ``-constr``-gated in Yosys's own default ABC script (``help abc``),
      which is why one flag delivers both halves of this change.
    - ``-D <picoseconds>`` only when the request supplied
      ``constraints.clock_period_ns``.
    - one ``-dont_use <glob>`` per exclusion-table entry.

    When ``constr_path`` is given the whole ``abc`` invocation is wrapped in
    ``tee -q -o <abc_log_path>`` so ``stime -p``'s output is captured to a
    file, per the #396 spike's explicit warning against regexing the
    interleaved Yosys log.

    ``tie_cells`` (issue #854, a :data:`_TIE_CELLS` entry) adds one
    ``hilomap -hicell <cell> <port> -locell <cell> <port>`` line, mapping
    every remaining constant driver onto a real standard cell. Its position
    is load-bearing: **after** ``clean`` (it can only rewrite the constants
    ABC/``clean`` left behind) and **before** ``stat``/``write_verilog`` (the
    tie cells it inserts are real instances, so they must be counted in
    ``instance_count``/``area_um2`` and present in the emitted netlist).

    With none of those (a ``cell_library`` in no table), the emitted script
    is byte-identical to the pre-#807 one.

    Every path embedded in the script is absolute, so the script runs
    correctly regardless of the invoking process's own working directory --
    :func:`_run_yosys` never sets ``cwd=``.
    """
    abc_command = f"abc -liberty {liberty_path}"
    if constr_path is not None:
        abc_command += f" -constr {constr_path}"
    if delay_target_ps is not None:
        abc_command += f" -D {delay_target_ps}"
    for glob in dont_use_globs:
        abc_command += f" -dont_use {glob}"
    if constr_path is not None and abc_log_path is not None:
        abc_command = f"tee -q -o {abc_log_path} {abc_command}"

    lines = [f"read_verilog {path}" for path in sources]
    lines += [
        f"hierarchy -check -top {hdl_toplevel}",
        f"synth -top {hdl_toplevel}",
        f"dfflibmap -liberty {liberty_path}",
        abc_command,
        "clean",
    ]
    if tie_cells is not None:
        (hi_cell, hi_port), (lo_cell, lo_port) = tie_cells
        lines.append(f"hilomap -hicell {hi_cell} {hi_port} -locell {lo_cell} {lo_port}")
    lines += [
        f"tee -q -o {stats_path} "
        f"stat -liberty {liberty_path} -json -top {hdl_toplevel}",
        f"write_verilog -noattr {netlist_path}",
    ]
    try:
        with open(script_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError as exc:
        raise SynthesizeError(
            f"could not write synthesis script '{script_path}': {exc}"
        ) from exc


def _run_yosys(script_path: str) -> str:
    """Invoke ``yosys -s <script_path>`` and raise :class:`SynthesizeError`
    on any failure to run (missing binary, timeout-free run exits nonzero).

    Never raises on a *successful* (exit 0) run -- the caller is responsible
    for validating the declared output files actually appeared. Returns the
    captured ``stdout`` log text on success -- Yosys's own ``Warning: ``
    lines land on stdout, never stderr (verified live; matches
    :func:`_synthesis_error_message`'s own stream-preference note for
    ``ERROR:`` lines) -- so :func:`_compute_structural`/
    :func:`_summarize_warnings` (issue #1588) can parse it without a second
    Yosys invocation.
    """
    try:
        completed = subprocess.run(
            ["yosys", "-s", script_path],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise SynthesizeError(f"could not launch yosys: {exc}") from exc

    if completed.returncode != 0:
        raise SynthesizeError(_synthesis_error_message(completed))

    return completed.stdout or ""


_WASI_SANDBOX_SCRIPT_NOT_FOUND_RE = re.compile(
    r"Can't open script file `(.+)' for reading: No such file or directory"
)

_WASI_SANDBOX_HINT = (
    "; the script file exists on disk but yosys could not read it -- this "
    "usually means the 'yosys' on $PATH is a WASI-sandboxed build (e.g. "
    "yowasp-yosys) whose sandbox does not preopen this path. Try prepending "
    "a native yosys build's directory to $PATH."
)


def _synthesis_error_message(completed: subprocess.CompletedProcess) -> str:
    """Build an actionable error message from a failed ``yosys -s`` run.

    Prefers the last ``ERROR:`` line Yosys itself printed (its own error
    lines go to stderr -- verified live, Yosys survey worked example), on
    either stream; falls back to a generic exit-code message with a short
    tail of captured output when no ``ERROR:`` line is found.

    When the error line is the ``Can't open script file `<path>' for
    reading: No such file or directory`` shape *and* ``<path>`` verifiably
    exists on the host filesystem, appends a hint that the ``yosys`` on
    ``$PATH`` is likely a WASI-sandboxed build (e.g. ``yowasp-yosys``) whose
    sandbox does not preopen that path -- see issue #1368. A script path
    that genuinely does not exist is a different failure and is left
    unchanged.
    """
    for stream in (completed.stderr or "", completed.stdout or ""):
        error_lines = [line.strip() for line in stream.splitlines() if "ERROR:" in line]
        if error_lines:
            message = f"yosys synthesis failed: {error_lines[-1]}"
            match = _WASI_SANDBOX_SCRIPT_NOT_FOUND_RE.search(error_lines[-1])
            if match and os.path.isfile(match.group(1)):
                message += _WASI_SANDBOX_HINT
            return message

    tail_source = (completed.stderr or completed.stdout or "").strip().splitlines()
    snippet = " ".join(tail_source[-3:]) if tail_source else "no output captured"
    return f"yosys exited with code {completed.returncode}: {snippet}"


def _submodule_key(
    modules: dict[str, Any], cell_type: str, seen: frozenset[str]
) -> str | None:
    """The ``modules`` key a ``num_cells_by_type`` entry names, or ``None``
    when the entry is a real leaf standard cell.

    ``stat -json`` keys the ``modules`` dict by escaped identifier
    (``"\\adder16"``) but names submodule instances *unescaped* inside
    ``num_cells_by_type`` (``{"adder16": 2}``) -- verified live against real
    Yosys output (0.33 locally, 0.68 in #821's curation pass). The unescaped
    key is also probed, so a stats file that does not escape module names is
    still resolved. Modules already on the current recursion path resolve to
    ``None`` (treated as leaves), which is what keeps a pathological cyclic
    ``modules`` graph from recursing forever.
    """
    for candidate in (f"\\{cell_type}", cell_type):
        if candidate in modules and candidate not in seen:
            return candidate
    return None


def _aggregate_cell_counts(
    modules: dict[str, Any],
    module_key: str,
    _seen: frozenset[str] = frozenset(),
) -> tuple[int, dict[str, int]]:
    """Recursively total the *leaf* standard-cell counts reachable from
    ``modules[module_key]``, scaled through every level of hierarchy.

    ``stat -liberty ... -json``'s ``modules`` dict is keyed per module
    *definition* (escaped, e.g. ``"\\adder16"``), not per instance path, and
    each block's own ``num_cells_by_type`` mixes two different kinds of
    entries: real leaf standard-cell types (e.g.
    ``"sky130_fd_sc_hd__xnor2_1"``) and, for any sub-module Yosys's default
    ``synth`` left hierarchical (never flattened into the top module), a
    pseudo "cell type" entry named after the sub-module itself (e.g.
    ``"adder16": 2`` for two ``adder16`` instances) -- confirmed live against
    real Yosys output, issue #821. A submodule entry's *count* already
    reflects how many times that submodule is instantiated under this one
    parent (verified live: ``modules["\\<top>"].num_cells_by_type`` shows
    ``{"adder16": 2}`` when the parent instantiates ``adder16`` twice) -- so
    each submodule's own per-instance totals need only be multiplied by that
    count, not by some separately-tracked instance path.

    Returns ``(total_leaf_cells, leaf_cells_by_type)`` for **one instance**
    of ``modules[module_key]`` -- real standard-cell types only; submodule
    names never appear as keys in the returned dict, at any depth.

    **A hierarchical block's own ``num_cells`` is deliberately not used as
    the starting total.** Whether it counts submodule instances is
    Yosys-version-dependent, and getting that wrong silently double-counts
    every nested cell:

    - Yosys 0.68 (#821's live capture): ``mac8`` reports ``num_cells: 0``
      alongside ``num_submodules: 2`` -- submodule instances are *excluded*
      from ``num_cells``.
    - Yosys 0.33 (verified live while implementing #821, no
      ``num_submodules`` field at all): a top with one direct ``$_NOT_`` and
      one submodule instance reports ``num_cells: 2`` -- submodule instances
      are *included* in ``num_cells``.

    ``num_cells_by_type`` is the version-stable source: in both shapes it
    breaks the block down entry by entry, so the count of *direct real
    cells* is exactly the sum of the entries that do not name another
    module. Each entry that *does* name another module contributes that
    submodule's own recursive total instead, scaled by the entry's count,
    and never appears as a key in the result.

    A single-module design -- no ``num_cells_by_type`` entry matches another
    key in ``modules`` -- recurses zero levels and short-circuits to the
    block's verbatim ``num_cells``/``num_cells_by_type``, an exact match for
    the direct pass-through this replaced (#821), byte for byte, even when
    (as in a trimmed test fixture) the by-type dict does not itself sum to
    ``num_cells``.
    """
    stats = modules.get(module_key)
    if not isinstance(stats, dict):
        return 0, {}
    own_num_cells = stats.get("num_cells")
    own_total = own_num_cells if isinstance(own_num_cells, int) else 0
    counts_by_type = stats.get("num_cells_by_type")
    if not isinstance(counts_by_type, dict):
        return own_total, {}

    seen = _seen | {module_key}
    resolved = {
        cell_type: _submodule_key(modules, cell_type, seen)
        for cell_type in counts_by_type
    }
    if not any(resolved.values()):
        # Flat block (the common single-module case): nothing to recurse
        # into, so report exactly what Yosys reported.
        return own_total, dict(counts_by_type)

    total = 0
    aggregated: dict[str, int] = {}
    for cell_type, count in counts_by_type.items():
        if not isinstance(count, int):
            continue
        submodule_key = resolved[cell_type]
        if submodule_key is None:
            total += count
            aggregated[cell_type] = aggregated.get(cell_type, 0) + count
            continue
        sub_total, sub_by_type = _aggregate_cell_counts(modules, submodule_key, seen)
        total += sub_total * count
        for sub_type, sub_count in sub_by_type.items():
            aggregated[sub_type] = aggregated.get(sub_type, 0) + sub_count * count
    return total, aggregated


def _read_stats(stats_path: str, hdl_toplevel: str) -> dict[str, Any]:
    """Parse ``stat -liberty ... -json``'s captured output (Yosys survey
    section 2) and return the top module's stats block, with ``num_cells``/
    ``num_cells_by_type`` replaced by a recursive rollup over the design's
    full hierarchy (#821).

    Prefers ``modules["\\<hdl_toplevel>"]`` (Yosys's own escaped-identifier
    naming for a public module name); falls back to the ``design`` rollup
    key when that lookup misses (a defensive fallback, not the primary
    path -- for a single top-level design the two are identical, per the
    Yosys survey's own worked example). Raises :class:`SynthesizeError` if
    the file is missing, unparseable, or names no recognisable module.

    ``area``/``sequential_area`` are passed through unchanged from the
    resolved block -- ``stat``'s own ``area`` is already a recursive rollup
    at every level of hierarchy (verified live, #821), so only the cell-
    count fields need correcting. For a design where the top module
    instantiates sub-modules Yosys's default ``synth`` leaves hierarchical
    (never flattened away), ``modules["\\<hdl_toplevel>"]``'s own
    ``num_cells``/``num_cells_by_type`` describe only that one module --
    ``num_cells`` is zero (Yosys 0.68) or counts each sub-module instance as
    a single "cell" (Yosys 0.33) when the top is a pure wrapper, and
    ``num_cells_by_type`` reports sub-module *names* as pseudo cell types
    rather than the real leaf standard cells they instantiate.
    :func:`_aggregate_cell_counts` walks the full ``modules`` dict to total
    real leaf standard-cell counts recursively, scaled by each level's own
    instance count, so a multiply-instantiated or deeply-nested sub-module
    is counted correctly rather than once or not at all. A single-module
    design is unaffected: with no submodule entries to recurse into, the
    rollup is identical to the direct pass-through this function used
    before #821.
    """
    if not os.path.isfile(stats_path):
        raise SynthesizeError(
            f"yosys did not produce the expected stats output '{stats_path}'"
        )
    try:
        with open(stats_path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, UnicodeDecodeError) as exc:
        raise SynthesizeError(
            f"could not read stats output '{stats_path}': {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise SynthesizeError(
            f"stats output '{stats_path}' is not valid JSON: {exc}"
        ) from exc

    modules = data.get("modules") if isinstance(data, dict) else None
    module_key = f"\\{hdl_toplevel}"
    module_stats = (modules or {}).get(module_key)
    used_module_lookup = module_stats is not None
    if module_stats is None:
        module_stats = data.get("design") if isinstance(data, dict) else None
    if not isinstance(module_stats, dict) or "num_cells" not in module_stats:
        raise SynthesizeError(
            f"could not find synthesis statistics for top module "
            f"'{hdl_toplevel}' in '{stats_path}'"
        )

    result = dict(module_stats)
    if "area" not in result:
        # Same class of gap as the module-key-miss fallback above, one
        # condition earlier: distro-packaged Yosys (e.g. Ubuntu 24.04's
        # 0.33) omits the `area` key entirely from a module's `stat -json`
        # block when that module's own directly-owned area is exactly zero
        # (a pure structural wrapper with no cells of its own -- the normal
        # shape for an un-flattened hierarchical top). `data["design"]`'s
        # own `area` is already the correct hierarchy-wide rollup in this
        # case (verified live, #1262) -- the same equivalence this
        # function's docstring already asserts for the module-key-miss
        # path above, just reached from a narrower missing-field condition.
        design_stats = data.get("design") if isinstance(data, dict) else None
        if isinstance(design_stats, dict) and "area" in design_stats:
            result["area"] = design_stats["area"]
        else:
            # Neither the module nor the `design` rollup carries an `area`
            # key at all -- verified live (issue #1588): a design whose only
            # cell is an internal, non-liberty primitive (e.g. an inferred
            # latch left as `$_DLATCH_P_` -- `dfflibmap` maps only flip-
            # flops, so a latch is never liberty-mapped) contributes zero
            # liberty-recognized area, and Yosys's own `stat -liberty ...
            # -json` omits the `area` key entirely rather than reporting
            # `0.0` explicitly in that case. `0.0` is the semantically
            # correct value (no standard-cell area to report), not a guess
            # -- degrade to it rather than a raw `KeyError` on the caller's
            # `module_stats["area"]` access, the same "never crash on a
            # legitimately-absent field" discipline `sequential_area_um2`
            # already follows (#560).
            result["area"] = 0.0
    if used_module_lookup and isinstance(modules, dict):
        total_cells, cells_by_type = _aggregate_cell_counts(modules, module_key)
        result["num_cells"] = total_cells
        result["num_cells_by_type"] = cells_by_type
    elif isinstance(modules, dict):
        # Defensive fallback path (`data["design"]` used because the top
        # module's own key was missing from `modules` -- should not happen
        # for a real Yosys run): `design.num_cells` is already the recursive
        # total, but `design.num_cells_by_type` can still mix submodule-name
        # pseudo entries in among real cell types (#821), so drop those.
        raw_by_type = module_stats.get("num_cells_by_type")
        if isinstance(raw_by_type, dict):
            result["num_cells_by_type"] = {
                cell_type: count
                for cell_type, count in raw_by_type.items()
                if _submodule_key(modules, cell_type, frozenset()) is None
            }
    return result


def _read_abc_timing(
    abc_log_path: str | None, delay_target_ps: int | None
) -> dict[str, Any] | None:
    """Parse ABC's own ``stime -p`` line out of the captured ABC log and
    shape it into the response's ``timing`` object, or return ``None`` when
    no such line is available.

    ``None`` covers every "no number to report" case honestly rather than
    inventing one: no ``-constr`` was passed (the resolved ``cell_library``
    has no :data:`_ABC_CONSTR_INPUTS` entry, so ``stime -p`` never ran), the
    captured log is missing/unreadable, or the resolved ABC printed no
    recognisable summary line.

    The shape names its own provenance and limits -- never a bare
    ``delay_ps`` float a caller could mistake for signoff timing (QoR survey
    section 3.3):

    ``{"source": "abc_stime", "wire_load": null, "critical_path_ps": 2485.93,
    "delay_target_ps": 5000}``

    ``wire_load`` is ABC's own ``WireLoad = "..."`` echo, normalised to
    ``None`` for its ``"none"`` -- the load-modelling caveat made
    machine-readable: this is a **pre-layout, wire-free** combinational
    estimate over the cone ABC itself mapped (flip-flops are already
    liberty-mapped by ``dfflibmap`` before ``abc`` runs, so they are outside
    the reported path), not a register-to-register signoff number.

    When a run produces several ``stime`` reports (Yosys invokes ABC once
    per combinational region), the **maximum** reported delay is used --
    the critical path across the whole design, not whichever region
    happened to be mapped last.
    """
    if abc_log_path is None or not os.path.isfile(abc_log_path):
        return None
    try:
        with open(abc_log_path, encoding="utf-8", errors="replace") as handle:
            log_text = handle.read()
    except OSError:
        return None

    best: tuple[float, str] | None = None
    for match in _ABC_STIME_RE.finditer(log_text):
        try:
            delay_ps = float(match.group("delay_ps"))
        except ValueError:  # pragma: no cover - regex only matches numbers
            continue
        if best is None or delay_ps > best[0]:
            best = (delay_ps, match.group("wire_load"))
    if best is None:
        return None

    critical_path_ps, wire_load = best
    return {
        "source": "abc_stime",
        "wire_load": None if wire_load.lower() == "none" else wire_load,
        "critical_path_ps": critical_path_ps,
        "delay_target_ps": delay_target_ps,
    }


def _read_sta_timing(
    netlist_path: str, liberty_path: str, hdl_toplevel: str
) -> dict[str, Any] | None:
    """Run :func:`klayout_tools.sta.compute_critical_path` over the
    just-produced ``netlist_path`` and shape its result into the response's
    ``sta`` field (issue #925, Epic #704 Phase 3) -- a real timing-graph
    walk over the whole mapped netlist (register-to-register,
    register-to-port, or port-to-port, whichever is globally worst),
    computed by ``klt_statime_native`` (``native/statime/``).

    Additive and best-effort, mirroring :func:`_read_abc_timing`'s own
    "no number to report" honesty discipline: returns ``None`` -- never a
    fabricated result -- when the ``klt_statime_native`` extension is not
    installed (a Rust toolchain is optional for every other ``klt synthesize``
    caller, so a missing extension must not turn every synthesis run into a
    hard failure) or when the native engine itself could not analyze this
    particular netlist/liberty pair (:class:`~klayout_tools.sta.StaError`).
    A genuine analysis result is never partial -- either the full
    ``worst_path``/``worst_reg_to_reg_path`` shape comes back, or this
    returns ``None``.
    """
    try:
        return compute_critical_path(netlist_path, liberty_path, hdl_toplevel)
    except StaError:
        return None


def _resolve_expected_latches(request: dict[str, Any]) -> int:
    """``request.structural.expected_latches`` (issue #1588; default ``0``
    when ``request.structural`` is omitted entirely) -- the number of
    latches a caller declares intentional, subtracted from the response's
    ``structural.latches`` to produce ``structural.unexpected_latches``.

    Raises :class:`SynthesizeError` for a non-integer or negative value,
    matching this module's existing request-validation discipline (e.g.
    :func:`_resolve_delay_target_ps`) -- a request that means to declare an
    expected count but expresses it wrongly must not be silently treated as
    "expect zero".
    """
    structural_request = request.get("structural")
    if structural_request is None:
        return 0
    if not isinstance(structural_request, dict):
        raise SynthesizeError("request.structural must be a JSON object")
    expected = structural_request.get("expected_latches", 0)
    if isinstance(expected, bool) or not isinstance(expected, int) or expected < 0:
        raise SynthesizeError(
            "request.structural.expected_latches must be a non-negative integer"
        )
    return expected


def _compute_structural(
    module_stats: dict[str, Any], log_text: str, expected_latches: int
) -> dict[str, Any]:
    """Build the response's ``structural`` field (issue #1588) -- an
    always-present verdict over the three unambiguously-wrong synchronous-
    design conditions Yosys's own ``synth``/``stat`` already surface, from
    the same run this module already performs (no extra Yosys invocation):

    - **Inferred latches.** ``stat -json``'s ``num_cells_by_type`` (already
      parsed by :func:`_read_stats` into ``module_stats``) is scanned for
      any cell-type name matching :data:`_LATCH_CELL_TYPE_RE` (``"dlatch"``,
      case-insensitive) -- ``dfflibmap`` maps only flip-flops (verified
      against ``yosys -p 'help dfflibmap'``), never latches, so an inferred
      latch survives unmapped all the way to this module's own final
      ``stat``/``write_verilog`` step as a bare gate-level primitive
      (``$_DLATCH_P_`` and siblings) -- no separate Yosys pass is needed to
      find it. ``expected_latches`` (``request.structural.expected_latches``,
      via :func:`_resolve_expected_latches`) is subtracted, floored at
      ``0``, to produce ``unexpected_latches``.
    - **Combinational loops / multiply-driven nets.** Parsed from
      ``log_text`` (the captured Yosys run log :func:`_run_yosys` returns) --
      ``synth -top <top>``'s own internal ``check`` sub-stages (``yosys -p
      'help synth'``'s documented ``coarse``/``check`` stages) run
      unconditionally and print one ``Warning: found logic loop`` /
      ``Warning: multiple conflicting drivers`` line per distinct problem
      **before** ABC's own loop-breaking heuristic can silently sever a real
      combinational loop -- verified live that an *additional* ``check``
      step run by this module *after* ``synth`` completes finds zero
      problems on a design ``synth``'s own internal check already flagged,
      because by then the loop no longer structurally exists. Counted as
      the number of **distinct** matching lines (a `set`, not a raw line
      count): a persisting problem's identical warning text is reprinted at
      more than one of ``synth``'s internal ``check`` calls -- verified
      live, a real multi-driver conflict prints twice for one problem -- so
      a naive line count would double-count.

    ``has_critical`` is ``true`` iff ``comb_loops > 0 or multi_driven > 0 or
    unexpected_latches > 0`` -- the response-level, exit-code-3 verdict (see
    ``docs/cli/synthesize.md``'s "Exit codes" section).
    """
    counts_by_type = module_stats.get("num_cells_by_type") or {}
    latches = sum(
        count
        for cell_type, count in counts_by_type.items()
        if isinstance(count, int) and _LATCH_CELL_TYPE_RE.search(cell_type)
    )
    unexpected_latches = max(0, latches - expected_latches)

    warning_lines = {
        line for line in log_text.splitlines() if line.startswith("Warning: ")
    }
    comb_loops = sum(1 for line in warning_lines if _COMB_LOOP_WARNING_RE.match(line))
    multi_driven = sum(
        1 for line in warning_lines if _MULTI_DRIVEN_WARNING_RE.match(line)
    )

    return {
        "latches": latches,
        "expected_latches": expected_latches,
        "unexpected_latches": unexpected_latches,
        "comb_loops": comb_loops,
        "multi_driven": multi_driven,
        "has_critical": comb_loops > 0 or multi_driven > 0 or unexpected_latches > 0,
    }


def _categorize_warning(message: str) -> str:
    """The :data:`_WARNING_CATEGORY_PATTERNS` category name matching
    ``message`` (the warning text with the leading ``"Warning: "`` prefix
    already stripped), or ``"other"`` when none match."""
    for category, pattern in _WARNING_CATEGORY_PATTERNS:
        if pattern.search(message):
            return category
    return "other"


def _summarize_warnings(log_text: str) -> dict[str, Any]:
    """Build the response's ``warnings`` field (issue #1588): a bounded,
    deterministic summary of every ``Warning: `` line in ``log_text`` --
    never the raw log itself.

    ``total`` is a raw line count (deliberately **not** deduplicated the
    way :func:`_compute_structural`'s own ``comb_loops``/``multi_driven``
    counts are -- ``synth``'s internal ``check`` calls can reprint an
    unresolved problem's identical text more than once, so this answers
    "how noisy was this run", not "how many distinct problems").
    ``by_category``/``representatives`` are sorted by category name for
    determinism; ``representatives`` is capped at
    :data:`_MAX_WARNING_REPRESENTATIVES` entries, one per category, each the
    first message text seen for that category (bounded, per issue #1588).
    """
    total = 0
    by_category: dict[str, int] = {}
    first_seen: dict[str, str] = {}
    for line in log_text.splitlines():
        if not line.startswith("Warning: "):
            continue
        message = line[len("Warning: ") :].strip()
        if not message:
            continue
        total += 1
        category = _categorize_warning(message)
        by_category[category] = by_category.get(category, 0) + 1
        first_seen.setdefault(category, message)

    sorted_categories = sorted(by_category)
    representatives = [
        {
            "category": category,
            "count": by_category[category],
            "text": first_seen[category],
        }
        for category in sorted_categories[:_MAX_WARNING_REPRESENTATIVES]
    ]
    return {
        "total": total,
        "by_category": {
            category: by_category[category] for category in sorted_categories
        },
        "representatives": representatives,
    }


def _critical_path_ns(response: dict[str, Any]) -> float | None:
    """The response's own whole-netlist critical-path number, in
    nanoseconds, for ``baseline`` delta comparison (issue #1588) -- prefers
    the real ``sta`` stage's ``worst_path.delay_ns`` (already nanoseconds);
    falls back to ``timing``'s ABC ``stime -p`` estimate
    (``critical_path_ps``, converted) when ``sta`` is unavailable. ``None``
    when neither stage produced a number, mirroring both fields' own "no
    number to report" discipline.
    """
    sta = response.get("sta")
    if isinstance(sta, dict):
        worst_path = sta.get("worst_path")
        if isinstance(worst_path, dict):
            delay_ns = worst_path.get("delay_ns")
            if isinstance(delay_ns, (int, float)):
                return float(delay_ns)
    timing = response.get("timing")
    if isinstance(timing, dict):
        critical_path_ps = timing.get("critical_path_ps")
        if isinstance(critical_path_ps, (int, float)):
            return float(critical_path_ps) / 1000.0
    return None


def _pct_delta(current: float, baseline: float) -> float | None:
    """``(current - baseline) / baseline * 100`` -- ``0.0`` when both are
    ``0`` (no change); ``None`` when ``baseline`` is ``0`` but ``current``
    is not (an undefined percentage change from a zero base, never
    fabricated as an infinite or arbitrary number)."""
    if baseline == 0:
        return 0.0 if current == 0 else None
    return (current - baseline) / baseline * 100.0


def _baseline_metrics_from_response_file(path: str) -> dict[str, Any]:
    """``{instance_count, area_um2, critical_path_ns}`` extracted from a
    prior ``klt synthesize --format json`` response file at ``path`` --
    ``request.baseline.response_path``'s resolution (issue #1588): compare
    against a committed report from an earlier run.

    Raises :class:`SynthesizeError` for a missing/unreadable/malformed
    file, or one that does not look like a ``klt synthesize`` response at
    all (no ``instance_count``).
    """
    if not os.path.isfile(path):
        raise SynthesizeError(f"request.baseline.response_path not found: {path}")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, UnicodeDecodeError) as exc:
        raise SynthesizeError(
            f"could not read request.baseline.response_path '{path}': {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise SynthesizeError(
            f"request.baseline.response_path '{path}' is not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict) or "instance_count" not in data:
        raise SynthesizeError(
            f"request.baseline.response_path '{path}' does not look like a "
            "`klt synthesize` response (missing instance_count)"
        )
    return {
        "instance_count": data.get("instance_count"),
        "area_um2": data.get("area_um2"),
        "critical_path_ns": _critical_path_ns(data),
    }


def _baseline_metrics_from_netlist(
    netlist_path: str,
    liberty_path: str,
    hdl_toplevel: str,
    output_dir: str,
) -> dict[str, Any]:
    """``{instance_count, area_um2, critical_path_ns}`` re-derived from a
    bare prior netlist file -- ``request.baseline.netlist_path``'s
    resolution (issue #1588), for a caller that saved only the netlist, not
    a full response JSON.

    A minimal ``<top>_baseline.ys`` script is written to ``output_dir``
    alongside this run's other artifacts and kept, matching this module's
    "generated deck is never deleted" discipline. ``read_liberty -lib
    <liberty_path>`` runs **before** ``read_verilog``/``hierarchy -check``
    -- verified live: without it, ``hierarchy -check`` fails on a mapped
    netlist's standard-cell instances (``Module '\\<cell>' ... is not part
    of the design``), since a bare gate-level netlist references liberty
    cells that were never elaborated as blackboxes.
    """
    if not os.path.isfile(netlist_path):
        raise SynthesizeError(
            f"request.baseline.netlist_path not found: {netlist_path}"
        )

    script_path = os.path.join(output_dir, f"{hdl_toplevel}_baseline.ys")
    stats_path = os.path.join(output_dir, f"{hdl_toplevel}_baseline_stats.json")
    script_lines = [
        f"read_liberty -lib {liberty_path}",
        f"read_verilog {netlist_path}",
        f"hierarchy -check -top {hdl_toplevel}",
        f"tee -q -o {stats_path} "
        f"stat -liberty {liberty_path} -json -top {hdl_toplevel}",
    ]
    try:
        with open(script_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(script_lines) + "\n")
    except OSError as exc:
        raise SynthesizeError(
            f"could not write baseline script '{script_path}': {exc}"
        ) from exc

    _run_yosys(script_path)
    module_stats = _read_stats(stats_path, hdl_toplevel)

    critical_path_ns = None
    try:
        sta_result = compute_critical_path(netlist_path, liberty_path, hdl_toplevel)
    except StaError:
        sta_result = None
    if sta_result is not None:
        worst_path = sta_result.get("worst_path")
        if isinstance(worst_path, dict):
            delay_ns = worst_path.get("delay_ns")
            if isinstance(delay_ns, (int, float)):
                critical_path_ns = float(delay_ns)

    return {
        "instance_count": module_stats["num_cells"],
        "area_um2": module_stats.get("area"),
        "critical_path_ns": critical_path_ns,
    }


def _compute_baseline(
    request: dict[str, Any],
    *,
    request_dir: str,
    response: dict[str, Any],
    liberty_path: str,
    hdl_toplevel: str,
    output_dir: str,
) -> dict[str, Any] | None:
    """Build the response's optional ``baseline`` field (issue #1588) --
    ``None`` unless ``request.baseline`` names a prior run to compare
    against, via exactly one of ``response_path`` (a previously captured
    ``klt synthesize --format json`` response file) or ``netlist_path`` (a
    bare mapped netlist, re-``stat``/re-timed against this run's own
    resolved liberty).

    ``ref`` identifies what was compared against: ``request.baseline.ref``
    when given, else the literal ``response_path``/``netlist_path`` string
    -- always present, never ``null``, so a caller can always tell what a
    ``baseline`` object was measured against even without an explicit
    label.

    ``area_um2``/``critical_path_ns`` (and their ``delta_pct`` siblings) are
    included only when both this run and the baseline produced a number --
    ``instance_count`` is always present (every ``klt synthesize`` response
    has one). ``delta_pct`` values are ``(current - baseline) / baseline *
    100`` via :func:`_pct_delta`.
    """
    baseline_request = request.get("baseline")
    if baseline_request is None:
        return None
    if not isinstance(baseline_request, dict):
        raise SynthesizeError("request.baseline must be a JSON object")

    response_path = baseline_request.get("response_path")
    netlist_path = baseline_request.get("netlist_path")
    if response_path is not None and not (
        isinstance(response_path, str) and response_path
    ):
        raise SynthesizeError(
            "request.baseline.response_path must be a non-empty string"
        )
    if netlist_path is not None and not (
        isinstance(netlist_path, str) and netlist_path
    ):
        raise SynthesizeError(
            "request.baseline.netlist_path must be a non-empty string"
        )
    if response_path is None and netlist_path is None:
        raise SynthesizeError("request.baseline must set response_path or netlist_path")
    if response_path is not None and netlist_path is not None:
        raise SynthesizeError(
            "request.baseline must set only one of response_path/netlist_path, not both"
        )

    ref = baseline_request.get("ref")
    if ref is not None and not (isinstance(ref, str) and ref):
        raise SynthesizeError(
            "request.baseline.ref must be a non-empty string when given"
        )

    if response_path is not None:
        resolved = (
            response_path
            if os.path.isabs(response_path)
            else os.path.join(request_dir, response_path)
        )
        baseline_metrics = _baseline_metrics_from_response_file(resolved)
        ref = ref or response_path
    else:
        resolved = (
            netlist_path
            if os.path.isabs(netlist_path)
            else os.path.join(request_dir, netlist_path)
        )
        baseline_metrics = _baseline_metrics_from_netlist(
            resolved, liberty_path, hdl_toplevel, output_dir
        )
        ref = ref or netlist_path

    current_critical_path_ns = _critical_path_ns(response)

    result: dict[str, Any] = {
        "ref": ref,
        "instance_count": baseline_metrics["instance_count"],
    }
    delta_pct: dict[str, float | None] = {
        "instance_count": _pct_delta(
            response["instance_count"], baseline_metrics["instance_count"]
        )
    }
    if baseline_metrics.get("area_um2") is not None:
        result["area_um2"] = baseline_metrics["area_um2"]
        delta_pct["area_um2"] = _pct_delta(
            response["area_um2"], baseline_metrics["area_um2"]
        )
    if (
        baseline_metrics.get("critical_path_ns") is not None
        and current_critical_path_ns is not None
    ):
        result["critical_path_ns"] = baseline_metrics["critical_path_ns"]
        delta_pct["critical_path_ns"] = _pct_delta(
            current_critical_path_ns, baseline_metrics["critical_path_ns"]
        )
    result["delta_pct"] = delta_pct
    return result

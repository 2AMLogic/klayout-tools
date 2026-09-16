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

``arithmetic`` (the request's own ``arithmetic`` field; issue #1722) is the
arithmetic-**architecture** lever: instead of always taking whatever adder
structure Yosys's ``alumacc``/``techmap`` expansion produces, a request can
substitute a generated parallel-prefix adder
(:mod:`klayout_tools.arith_gen` -- ``ripple``/``brent-kung``/
``han-carlson``/``sklansky``/``kogge-stone``) for the design's own ``$add``
cells, or ask for ``"auto"`` and have this module **measure** every
candidate (plus Yosys's own default, as a real row) through the same
synth + timing stages it already runs, then keep the smallest one meeting
``constraints.clock_period_ns``. The substitution is a ``techmap -map``
rule file inserted between ``hierarchy`` and ``synth`` (see
:func:`_write_script` for why that position is load-bearing), guarded by
``_TECHMAP_FAIL_`` so only the probed widths are replaced and every other
``$add`` keeps Yosys's own expansion. Every substituted adder is proven
equivalent to a behavioural ``a + b + cin`` by ``klt equiv`` before it is
kept (:func:`_verify_generated_adder`) -- scoped to the *adder module*
rather than the whole design, which is both far cheaper and, unlike a
whole-design check, usable on the sequential canaries that motivated the
lever. Off by default (``arithmetic`` absent -> ``response.arithmetic`` is
``None``, byte-identical script). Multipliers (compressor trees) and search
over cell maps are explicit non-goals. See ``docs/cli/synthesize.md``'s
"Arithmetic architecture" section and
``docs/design/synthesize-qor-improvements-survey.md`` section 3.8.

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

``leakage_power_nw``/``leakage_by_type_nw`` (issue #1626) are the static
(leakage-only, no switching/dynamic power -- that needs an activity factor
this command has no vectors to supply) power figure this run's own resolved
liberty already carries: ``sum(cell_leakage_power[cell_type] *
instance_count[cell_type])`` over the response's own
``instance_counts_by_type``, plus the per-type breakdown that sum is built
from. Both are ``None`` when the resolved liberty reports no
``cell_leakage_power`` for any instantiated cell type at all (some
libraries, e.g. gf180mcu_fd_sc_mcu9t5v0, report leakage only via
per-input-state groups this module deliberately does not average into one
number). See :func:`_compute_leakage`.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any

from . import env_provenance
from ._paths import _load_request_json, validate_request_shape
from ._provenance import (
    _combined_content_hash,
    _yosys_version,
    build_provenance,
    wasi_sandbox_hint_if_applicable,
)
from .arith_gen import (
    ARCHITECTURES as ADDER_ARCHITECTURES,
)
from .arith_gen import (
    ArithGenError,
    emit_techmap_verilog,
    generate_adder,
    normalize_architecture,
)
from .equiv import EquivError, run_equiv
from .pdk_cells import resolve_liberty_for_cell_library
from .restructure import RestructureError, restructure_for_timing
from .sta import StaError, compute_critical_path

#: Bumped only on a non-additive (breaking) change to this command's own
#: JSON shape -- see docs/json-contract.md.
#:
#: Bumped 1 -> 2 (issue #1844): `netlist_path`/`script_path` (top level),
#: each `arithmetic.candidates[].measured.netlist_path`/`script_path`,
#: `restructuring.restructured_netlist_path` (when not `null`), and
#: `baseline.ref`'s literal-path fallback changed from a raw (often
#: absolute) path string to the `{path, scope}` shape
#: `env_provenance.repo_relative_path` already defines for `klt pex`/`klt
#: sim` (issue #1261) -- a committed evidence record wraps this response
#: unmodified (`docs/design/sim-evidence-discipline-spike.md`), so an
#: absolute path here used to leak the author's home directory/worktree
#: layout into every such record. See `_report_path` below.
#: `equivalence.artifacts.{script_path,netlist_path}` is `klt equiv`'s own
#: response shape (echoed through unmodified) and is deliberately **not**
#: normalized here -- doing so would be a `klt equiv` contract change,
#: which needs its own `schema_version` bump on that command, out of this
#: issue's scope.
SCHEMA_VERSION = 2

#: ``"yosys"`` is the only engine implemented today; the field is present in
#: the request from day one (contract spike section 2) so a later backend is
#: an additive enum value, never a contract-shape change.
SUPPORTED_ENGINES = ("yosys",)

#: The label used for the "leave Yosys's own ``$add`` expansion alone" row of
#: ``arithmetic.candidates`` -- a real candidate in the ``"auto"`` sweep (and
#: an accepted ``arithmetic.adders`` value, meaning "measure nothing, change
#: nothing"), never a prefix-adder architecture name.
DEFAULT_ADDER_LABEL = "default"

#: Accepted ``request.arithmetic.adders`` values beyond a bare architecture
#: name.
_ADDER_MODES = ("auto", DEFAULT_ADDER_LABEL)

#: Default ``arithmetic.min_width``: ``$add`` cells narrower than this keep
#: Yosys's own expansion. Below ~8 bits the structures collapse onto each
#: other (a 4-bit Sklansky and a 4-bit Brent-Kung differ by one cell) and the
#: measurement is dominated by mapping noise -- the issue's own "sub-16-bit
#: micro-optimisation" non-goal, with a little headroom.
DEFAULT_ADDER_MIN_WIDTH = 8

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
#: - ``sg13g2_stdcell`` (issue #1784) -> ``sg13g2_buf_4`` / ``6.0`` fF. IHP
#:   ships no ORFS platform config of its own; the source of truth here is
#:   IHP-Open-PDK's own LibreLane platform config
#:   (``libs.tech/librelane/sg13g2_stdcell/config.tcl``, IHP-Open-PDK
#:   v0.3.0), the direct analog of ORFS's ``config.mk`` for this PDK:
#:   ``SYNTH_DRIVING_CELL = sg13g2_buf_4`` / ``OUTPUT_CAP_LOAD = 6.0``.
#:   Liberty cross-check against the installed
#:   ``sg13g2_stdcell_typ_1p20V_25C.lib`` (``capacitive_load_unit(1,pf)``):
#:   ``sg13g2_buf_4``'s own input pin ``A`` has ``capacitance : 0.00370215``
#:   -- 3.70 fF -- so ``6.0`` is ~1.6 single-input loads, the same
#:   unit-consistent (femtofarad) shape as the two entries above, not the
#:   picofarad figure (which would be a physically implausible 6000 fF,
#:   5x the driving cell's own ``pin (X) { max_capacitance : 1.2; }``, i.e.
#:   1200 fF).
#:
#: ORFS is **reference data only**, never a runtime dependency -- nothing
#: here shells out to, reads, or requires an ORFS checkout, and no ORFS file
#: is vendored. A ``cell_library`` with no entry keeps this command's
#: pre-#807 behaviour exactly (no ``-constr``, no sizing/buffering, no
#: ``timing``), rather than guessing a driving cell for it.
_ABC_CONSTR_INPUTS: dict[str, tuple[str, float]] = {
    "sky130_fd_sc_hd": ("sky130_fd_sc_hd__buf_1", 5.0),
    "gf180mcu_fd_sc_mcu9t5v0": ("gf180mcu_fd_sc_mcu9t5v0__buf_4", 13.43),
    "sg13g2_stdcell": ("sg13g2_buf_4", 6.0),
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
#: - ``sg13g2_stdcell`` (issue #1784) -> the five literal cell names IHP's
#:   own LibreLane platform config names as its
#:   ``SYNTH_EXCLUDED_CELL_FILE``
#:   (``libs.tech/librelane/sg13g2_stdcell/synth_exclude.cells``,
#:   IHP-Open-PDK v0.3.0): ``sg13g2_lgcp_1``, ``sg13g2_sighold``,
#:   ``sg13g2_slgcp_1``, ``sg13g2_sdfbbp_1``, ``sg13g2_dfrbp_2`` -- clock-
#:   gate/scan/sign-hold sequential cells this PDK's own flow keeps out of
#:   mapping, the same "keep non-logic/special-purpose cells out of the
#:   netlist" intent as the sky130hd entry above (not a drive-strength
#:   policy like gf180mcu's deliberately-omitted one). ``abc -dont_use``'s
#:   glob support degrades gracefully to an exact match for a pattern with
#:   no wildcard, so these plain cell names are passed through unchanged
#:   rather than turned into an invented glob.
#:
#: A ``cell_library`` with no entry gets no ``-dont_use`` flags at all --
#: this command's pre-#807 behaviour -- rather than a guessed exclusion.
_ABC_DONT_USE_GLOBS: dict[str, tuple[str, ...]] = {
    "sky130_fd_sc_hd": (
        "sky130_fd_sc_hd__lpflow_*",
        "sky130_fd_sc_hd__probe*",
    ),
    "sg13g2_stdcell": (
        "sg13g2_lgcp_1",
        "sg13g2_sighold",
        "sg13g2_slgcp_1",
        "sg13g2_sdfbbp_1",
        "sg13g2_dfrbp_2",
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
#: - ``sg13g2_stdcell`` (issue #1784) -> two distinct cells,
#:   ``sg13g2_tiehi`` port ``L_HI`` and ``sg13g2_tielo`` port ``L_LO`` --
#:   IHP's own LibreLane platform config's ``SYNTH_TIEHI_PORT``/
#:   ``SYNTH_TIELO_PORT`` values verbatim
#:   (``libs.tech/librelane/sg13g2_stdcell/config.tcl``, IHP-Open-PDK
#:   v0.3.0: ``"sg13g2_tiehi L_HI"`` / ``"sg13g2_tielo L_LO"``), confirmed in
#:   the installed ``sg13g2_stdcell_typ_1p20V_25C.lib``: ``sg13g2_tiehi``'s
#:   only non-power pin is ``L_HI`` (``function : "1"``, ``driver_type :
#:   open_drain``) and ``sg13g2_tielo``'s only non-power pin is ``L_LO``
#:   (``function : "0"``, ``driver_type : open_source``). Same
#:   two-distinct-cells shape as gf180mcu, not sky130's single dual-output
#:   cell -- this library has no ``conb``-equivalent either.
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
    "sg13g2_stdcell": (
        ("sg13g2_tiehi", "L_HI"),
        ("sg13g2_tielo", "L_LO"),
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

#: Matches a liberty ``cell (name) {`` header -- used by
#: :func:`_parse_liberty_leakage_nw` to isolate each cell's own body without
#: a full brace-matching parse. ``cell_leakage_power`` is always a direct,
#: top-level attribute of its enclosing ``cell (...) { ... }`` block, never
#: nested inside a ``pin``/``timing`` sub-block (the Liberty grammar does not
#: permit it there), so the text between one cell header and the next is
#: exactly that cell's own body -- :func:`_match_brace`'s full parse (as
#: ``restructure.py``'s pin-compatibility check needs, since a pin *is*
#: nested) is not required here. The cell name's own quotes are optional --
#: a real volare `sky130_fd_sc_hd` liberty quotes it (``cell
#: ("sky130_fd_sc_hd__buf_1") {``); a synthetic/other-vendor liberty may not
#: (verified live against both, issue #1626).
_LIBERTY_CELL_HEADER_RE = re.compile(
    r'\bcell\s*\(\s*"?(?P<name>[A-Za-z0-9_]+)"?\s*\)\s*\{'
)

#: The scalar ``cell_leakage_power : <value>;`` attribute Liberty defines for
#: a cell's state-independent leakage figure -- what issue #1626 asks this
#: module to sum. Not every cell library populates it: some (verified live,
#: ``gf180mcu_fd_sc_mcu9t5v0``) report leakage only via per-input-state
#: ``leakage_power () { when : "..."; value : "..."; }`` groups instead, with
#: no way to reduce those to one number without assuming a state probability
#: this command has no vector data to supply -- exactly the same "no
#: activity factor available" gap the issue explicitly carves switching
#: power out for. :func:`_parse_liberty_leakage_nw` therefore reads *only*
#: this scalar field and leaves a cell with no such entry absent from its
#: result, rather than guessing from the conditional groups.
_CELL_LEAKAGE_POWER_RE = re.compile(
    r"\bcell_leakage_power\s*:\s*(?P<value>[-+0-9.eE]+)\s*;"
)

#: ``leakage_power_unit : "1nW";`` (sky130_fd_sc_hd) / ``leakage_power_unit :
#: 1uW ;`` (gf180mcu_fd_sc_mcu9t5v0, unquoted, space before the semicolon) --
#: the multiplier a bare ``cell_leakage_power`` number in this liberty is
#: expressed in. Verified live against both installed liberty families
#: (issue #1626).
_LEAKAGE_POWER_UNIT_RE = re.compile(
    r'\bleakage_power_unit\s*:\s*"?(?P<magnitude>[-+0-9.eE]+)\s*(?P<unit>[a-zA-Z]+)"?\s*;'
)

#: Multiplier from one ``leakage_power_unit`` unit to nanowatts.
_LEAKAGE_UNIT_TO_NW: dict[str, float] = {
    "fw": 1e-6,
    "pw": 1e-3,
    "nw": 1.0,
    "uw": 1e3,
    "mw": 1e6,
    "w": 1e9,
}


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


def _report_path(path: str | None, *, repo_root: str | None) -> dict[str, Any]:
    """Normalise one output-path field for this module's own JSON response
    (issue #1844) -- a thin, module-local wrapper over
    :func:`~klayout_tools.env_provenance.repo_relative_path` rather than a
    second normalizer, so `klt synthesize`'s reports and `klt
    env-provenance`'s own emitter agree on exactly one `{path, scope}`
    shape, matching the precedent `klt pex`/`klt sim` set for issue #1261.

    Deliberately applied only at the point a path is inserted into the
    response dict -- every internal caller (`_run_yosys`, `_read_sta_timing`,
    the equivalence gate, `_run_timing_restructuring`, `_compute_baseline`,
    arithmetic candidate measurement) keeps using the real absolute path
    variable for actual file I/O; only the reported *value* changes.
    """
    return env_provenance.repo_relative_path(path, repo_root=repo_root)


def _script_path_text(path: str, *, repo_root: str | None) -> str:
    """The text :func:`_write_script` embeds for one filesystem path (issue
    #1844) -- ``path`` unchanged (absolute) when ``repo_root`` is ``None``
    or ``path`` does not resolve inside it, preserving :func:`_write_script`'s
    original cwd-independence invariant exactly for those cases; ``path``
    rewritten relative to ``repo_root`` (POSIX separators) when it does, via
    the same :func:`~klayout_tools.env_provenance.repo_relative_path` this
    module's own response fields use -- one normalizer, not two. A caller
    embedding any relative-form path this way must run the script with
    ``cwd=repo_root``; see :func:`_write_script`'s own docstring.
    """
    if repo_root is None:
        return path
    normalized = env_provenance.repo_relative_path(path, repo_root=repo_root)
    if normalized["scope"] == "repo":
        return normalized["path"]
    return path


#: The machine-independent placeholder the commit-safe top-level ``.ys``
#: writes in place of the resolved PDK install root (issue #1870), e.g.
#: ``$PDK_ROOT/sky130A/libs.ref/sky130_fd_sc_hd/lib/sky130_fd_sc_hd__tt_025C_1v80.lib``.
#: Deliberately the *same* spelling ``klt sim``'s ``request.models.lib``
#: already accepts for a PDK-anchored path (``sim.py``'s
#: ``_resolve_models_lib``) and that ``klt pdk env`` exports, so one
#: convention covers both directions rather than inventing a second token.
#:
#: Yosys does **not** expand environment variables in a script file, so a
#: script carrying this token is not directly runnable -- that is exactly why
#: :func:`_write_script` also emits the rehydrated
#: :data:`RUN_SCRIPT_SUFFIX` sibling it actually runs.
PDK_ROOT_TOKEN = "$PDK_ROOT"

#: Filename suffix of the rehydrated, *runnable* sibling of the commit-safe
#: top-level script (issue #1870): ``synth_<top>.ys`` -> ``synth_<top>.run.ys``.
RUN_SCRIPT_SUFFIX = ".run.ys"

#: Header prepended to the commit-safe script whenever it carries a
#: :data:`PDK_ROOT_TOKEN` -- the token is otherwise indistinguishable from a
#: path Yosys would try to open literally, and the reader needs to be told,
#: in the artifact itself, which sibling is the runnable one.
_COMMIT_SCRIPT_HEADER = (
    "# Commit-safe form (issue #1870): the liberty below is written relative\n"
    "# to $PDK_ROOT, which Yosys does NOT expand -- this file is the artifact\n"
    "# to commit, not the one to run. Run the rehydrated sibling\n"
    "# '{run_script}' (regenerated by every `klt synthesize` run), or\n"
    "# substitute your own PDK install root for $PDK_ROOT first.\n"
)

#: The mirror-image header on the rehydrated sibling. It carries the real
#: absolute liberty path, so it is machine-specific by construction and must
#: not be committed -- said in the file itself, where a reviewer looking at a
#: diff that adds it will actually see it.
#: Deliberately does not spell the token itself: a rehydrated script
#: containing no occurrence of :data:`PDK_ROOT_TOKEN` at all is a stronger
#: (and directly checkable) property than "no occurrence outside the header".
_RUN_SCRIPT_HEADER = (
    "# Machine-local rehydration of '{script}' (issue #1870): the PDK-root\n"
    "# placeholder has been substituted with this machine's resolved PDK\n"
    "# install root, so this file is runnable but NOT commit-safe. Commit\n"
    "# '{script}' instead.\n"
)


def run_script_path(script_path: str) -> str:
    """The rehydrated, runnable sibling of the commit-safe ``script_path``
    (issue #1870): ``.../synth_gcd.ys`` -> ``.../synth_gcd.run.ys``.

    One function so the writer (:func:`_write_script`), the response field
    (``run_script_path``), and any caller reconstructing the pair agree on
    the name by construction rather than by two copies of the same
    string-munging.
    """
    base, extension = os.path.splitext(script_path)
    return (
        f"{base}{RUN_SCRIPT_SUFFIX}" if extension else script_path + RUN_SCRIPT_SUFFIX
    )


def rehydrate_script_text(text: str, *, pdk_root: str) -> str:
    """``text`` with every :data:`PDK_ROOT_TOKEN` prefix replaced by
    ``pdk_root`` (issue #1870) -- the one documented, testable transformation
    that turns the commit-safe script into the runnable one.

    Deliberately a plain prefix substitution over the script *text* rather
    than a re-render from the original inputs: that is what makes "the
    committed script and the executed script differ in exactly the liberty
    path, nothing else" a checkable property (the two files are
    byte-identical after this substitution, modulo their one-line headers)
    instead of an assumption about two independent code paths agreeing.

    A caller on another machine rehydrates a committed script the same way
    -- substitute its own install root (``klt pdk find``'s ``root``, or
    ``$PDK_ROOT``) -- which is the whole point of writing the token.
    """
    return text.replace(f"{PDK_ROOT_TOKEN}/", pdk_root.rstrip("/") + "/")


def _script_liberty_text(liberty_path: str, *, pdk_root: str | None) -> str:
    """The text :func:`_write_script` embeds for the resolved liberty (issue
    #1870) -- ``$PDK_ROOT/<path relative to the install root>`` when
    ``liberty_path`` resolves inside ``pdk_root``, otherwise ``liberty_path``
    unchanged (absolute), exactly as before that issue.

    ``pdk_root`` is ``None`` for every script but :func:`run_synthesize`'s own
    top-level one (the arithmetic-candidate trial scripts, the ABC probe, and
    the baseline re-derivation script), which keeps those ephemeral internal
    artifacts fully absolute and directly runnable -- unchanged, and the same
    split ``repo_root`` already makes for issue #1844's path rewriting.

    Reuses :func:`~klayout_tools.env_provenance.repo_relative_path` as a
    generic "path relative to this root, or ``external``" helper (it takes
    the root as an argument and never consults git itself), so the
    inside/outside test here is the *same* realpath-and-boundary test the
    repo-relative rewriting uses -- one normalizer, not two. The fallback is
    the pre-#1870 absolute path: losing machine-independence for a liberty
    installed outside the resolved PDK root is strictly better than emitting
    a token that would not rehydrate.
    """
    if pdk_root is None:
        return liberty_path
    normalized = env_provenance.repo_relative_path(liberty_path, repo_root=pdk_root)
    if normalized["scope"] != "repo" or normalized["path"] in (None, "."):
        return liberty_path
    return f"{PDK_ROOT_TOKEN}/{normalized['path']}"


def _baseline_ref_fallback(resolved_path: str, *, repo_root: str | None) -> str:
    """`baseline.ref`'s default value (issue #1844) when
    `request.baseline.ref` is omitted -- previously the literal
    `response_path`/`netlist_path` request string, verbatim, which leaked an
    absolute path into the response whenever the caller's own request named
    one (e.g. a prior run's `netlist_path`, itself absolute before this
    issue's fix).

    `ref` stays a plain string either way (unlike `netlist_path`/
    `script_path`, it is a caller-facing label, not one of this issue's
    `{path, scope}` object fields) -- reuses
    :func:`~klayout_tools.env_provenance.render_path_field`, the same
    `{path, scope}` -> text projection `klt pex`/`klt sim`'s own `--format
    text` output already uses, so the fallback reads as the repo-relative
    path when it resolves inside the invocation's repo, or `<outside
    repo>` when it does not -- never the absolute path.
    """
    return env_provenance.render_path_field(
        env_provenance.repo_relative_path(resolved_path, repo_root=repo_root)
    )


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
    # Issue #1844: every output-path field this run's own JSON response
    # echoes (`netlist_path`/`script_path` and the nested occurrences --
    # `arithmetic.candidates[].measured.*`, `restructuring.
    # restructured_netlist_path`, `baseline.ref`'s literal-path fallback) is
    # normalised against this one repo root, resolved once from the request
    # file's own location -- the same "walk up from the input" default
    # `env_provenance.find_repo_root` uses for its own caller, and the same
    # convention `klt pex`/`klt sim` established for issue #1261.
    repo_root = env_provenance.find_repo_root(request_dir)

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

    engine_options = _EngineOptions(
        liberty_path=liberty_path,
        cell_library=cell_library,
        delay_target_ps=delay_target_ps,
        dont_use_globs=dont_use_globs,
        tie_cells=_TIE_CELLS.get(cell_library),
        constr_inputs=constr_inputs,
    )

    arithmetic = None
    adder_sources: tuple[str, ...] = ()
    adder_techmap_path: str | None = None
    arithmetic_config = _resolve_arithmetic(request)
    if arithmetic_config is not None:
        arithmetic, adder_sources, adder_techmap_path = _run_arithmetic_selection(
            config=arithmetic_config,
            output_dir=output_dir,
            resolved_sources=resolved_sources,
            hdl_toplevel=hdl_toplevel,
            engine_options=engine_options,
            equiv_timeout_s=equiv_timeout_s,
            repo_root=repo_root,
        )

    # Issue #1844: `repo_root` is threaded into this run's own script so
    # that inputs resolving inside the invocation's repo (RTL sources, and
    # the `.klt/synthesize/` output paths for `tee -o`/`write_verilog`) are
    # embedded as repo-relative text rather than an absolute path -- the
    # resolved liberty stays absolute regardless (see `_write_script`'s own
    # docstring for why). Trial/probe/baseline scripts elsewhere in this
    # module deliberately do **not** get this treatment (`repo_root` is left
    # at its default `None` for those `_write_script` calls) -- they are
    # ephemeral internal working artifacts, never the response's own
    # `script_path`, so keeping them fully absolute (and their own
    # `_run_yosys` calls `cwd`-independent, unchanged) is lower-risk than
    # extending the relative-path/explicit-`cwd` pairing to every script
    # this module writes.
    #
    # Issue #1870: `pdk_root` is threaded in for the same reason and with the
    # same scope -- the top-level script only. It is what lets the resolved
    # liberty be written as `$PDK_ROOT/...` instead of an absolute (usually
    # home-rooted) path, which is what finally makes this artifact
    # `klt env-provenance scan`-clean. `_write_script` then returns the
    # *runnable* script to hand to Yosys (the rehydrated `synth_<top>.run.ys`
    # sibling when a token was written, else `script_path` itself); the
    # response keeps reporting `script_path` -- the commit-safe artifact --
    # as `script_path`, and names the executed one in `run_script_path`.
    executed_script_path = _write_script(
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
        adder_sources=adder_sources,
        adder_techmap_path=adder_techmap_path,
        repo_root=repo_root,
        pdk_root=pdk_info["root"],
    )

    # `cwd=repo_root` is what makes the relative paths `_write_script` just
    # embedded (when `repo_root` is not `None`) resolve correctly -- see
    # `_write_script`'s docstring "Commit-safety vs. cwd-independence"
    # section. When `repo_root` is `None` (no repo resolved for this
    # request), `_write_script` wrote only absolute paths, so `cwd=None`
    # (subprocess's own default: the invoking process's cwd) is exactly as
    # correct as it always was. `executed_script_path` (issue #1870) is the
    # rehydrated sibling whenever the liberty was tokenized -- Yosys would
    # fail to open a literal `$PDK_ROOT/...` path, so the committed artifact
    # is never the one executed.
    yosys_log = _run_yosys(executed_script_path, cwd=repo_root)

    if not os.path.isfile(netlist_path):
        raise SynthesizeError(
            f"yosys exited successfully but did not produce '{netlist_path}'"
        )

    module_stats = _read_stats(stats_path, hdl_toplevel)
    engine_version = _yosys_version()
    structural = _compute_structural(module_stats, yosys_log, expected_latches)
    warnings_summary = _summarize_warnings(yosys_log)
    instance_counts_by_type = dict(
        sorted((module_stats.get("num_cells_by_type") or {}).items())
    )
    leakage_power_nw, leakage_by_type_nw = _compute_leakage(
        liberty_path, instance_counts_by_type
    )

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
            repo_root=repo_root,
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
        "instance_counts_by_type": instance_counts_by_type,
        # Static (leakage) power only -- issue #1626. `None` when the
        # resolved liberty reports no `cell_leakage_power` for any
        # instantiated cell type at all -- see `_compute_leakage`.
        "leakage_power_nw": leakage_power_nw,
        "leakage_by_type_nw": leakage_by_type_nw,
        "timing": _read_abc_timing(abc_log_path, delay_target_ps),
        "sta": sta,
        "structural": structural,
        "warnings": warnings_summary,
        "netlist_path": _report_path(netlist_path, repo_root=repo_root),
        "script_path": _report_path(script_path, repo_root=repo_root),
        # Issue #1870: the script Yosys was actually handed. Equal to
        # `script_path` whenever no `$PDK_ROOT` token was written (a liberty
        # outside the resolved install root), and the rehydrated
        # `synth_<top>.run.ys` sibling otherwise -- so "what ran" is always
        # nameable, and never has to be inferred from a filename convention.
        # Additive field, no `schema_version` bump (docs/json-contract.md).
        "run_script_path": _report_path(executed_script_path, repo_root=repo_root),
        "provenance": provenance,
        "equivalence": equivalence,
        "restructuring": restructuring,
        "arithmetic": arithmetic,
        "baseline": None,
    }
    if arithmetic is not None:
        arithmetic["selected_measured"] = _candidate_measurement(
            instance_count=module_stats["num_cells"],
            area_um2=module_stats["area"],
            sta=sta,
            abc_timing=response["timing"],
            target_period_ns=(
                None if delay_target_ps is None else delay_target_ps / 1000.0
            ),
        )
    response["baseline"] = _compute_baseline(
        request,
        request_dir=request_dir,
        response=response,
        liberty_path=liberty_path,
        hdl_toplevel=hdl_toplevel,
        output_dir=output_dir,
        repo_root=repo_root,
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
    repo_root: str | None = None,
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

    ``restructured_netlist_path`` is normalized to the ``{path, scope}``
    shape (issue #1844) exactly like ``netlist_path``/``script_path`` --
    but only when a resize was actually applied; the field stays the bare
    JSON ``null`` (never ``{"path": null, "scope": "absent"}``) when no
    resize happened, so "not applicable" stays distinguishable from "an
    unresolved path".
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

    # Issue #1844: `restructured_netlist_path` is `restructure_for_timing`'s
    # own real absolute path, useful for every internal caller above -- only
    # the value handed back to `run_synthesize`'s response is normalized,
    # and only when it is not already `None` (no resize applied).
    if report["restructured_netlist_path"] is not None:
        report["restructured_netlist_path"] = _report_path(
            report["restructured_netlist_path"], repo_root=repo_root
        )

    return report


# --------------------------------------------------------------------------
# Arithmetic architecture (issue #1722): substitute a generated prefix adder
# for Yosys's own `$add` expansion, and -- in `auto` mode -- pick the one
# that measures best.
# --------------------------------------------------------------------------


class _EngineOptions:
    """The per-library Yosys/ABC knobs a trial synthesis needs to reproduce
    the real run's engine configuration exactly.

    Bundled into one object purely so :func:`_measure_candidate` does not
    take nine positional look-alikes; every field is resolved once, in
    :func:`run_synthesize`, from the same tables the real run uses.
    """

    __slots__ = (
        "liberty_path",
        "cell_library",
        "delay_target_ps",
        "dont_use_globs",
        "tie_cells",
        "constr_inputs",
    )

    def __init__(
        self,
        *,
        liberty_path: str,
        cell_library: str,
        delay_target_ps: int | None,
        dont_use_globs: tuple[str, ...],
        tie_cells: tuple[tuple[str, str], tuple[str, str]] | None,
        constr_inputs: tuple[str, float] | None,
    ) -> None:
        self.liberty_path = liberty_path
        self.cell_library = cell_library
        self.delay_target_ps = delay_target_ps
        self.dont_use_globs = dont_use_globs
        self.tie_cells = tie_cells
        self.constr_inputs = constr_inputs

    @property
    def target_period_ns(self) -> float | None:
        if self.delay_target_ps is None:
            return None
        return self.delay_target_ps / 1000.0


def _resolve_arithmetic(request: dict[str, Any]) -> dict[str, Any] | None:
    """Validate and normalise ``request.arithmetic`` (issue #1722).

    Returns ``None`` when the field is absent -- the unchanged, pre-#1722
    behaviour for every existing request document. Otherwise returns
    ``{"adders", "min_width", "candidates", "verify_adders"}`` with defaults
    filled in. Raises :class:`SynthesizeError` naming the offending field for
    anything malformed; an unknown architecture name is an error, never a
    silent fallback to Yosys's own expansion.
    """
    arithmetic = request.get("arithmetic")
    if arithmetic is None:
        return None
    if not isinstance(arithmetic, dict):
        raise SynthesizeError("request.arithmetic must be a JSON object")

    adders = arithmetic.get("adders")
    if adders is None:
        raise SynthesizeError(
            "request.arithmetic.adders is required when request.arithmetic "
            f"is given (one of: {', '.join(_ADDER_MODES + ADDER_ARCHITECTURES)})"
        )
    if not isinstance(adders, str) or not adders:
        raise SynthesizeError("request.arithmetic.adders must be a non-empty string")
    if adders not in _ADDER_MODES:
        try:
            adders = normalize_architecture(adders)
        except ArithGenError as exc:
            raise SynthesizeError(f"request.arithmetic.adders: {exc}") from exc

    min_width = arithmetic.get("min_width", DEFAULT_ADDER_MIN_WIDTH)
    if isinstance(min_width, bool) or not isinstance(min_width, int) or min_width < 2:
        raise SynthesizeError(
            "request.arithmetic.min_width must be an integer >= 2 "
            f"(default {DEFAULT_ADDER_MIN_WIDTH})"
        )

    candidates = arithmetic.get("candidates")
    if candidates is None:
        resolved_candidates = list(ADDER_ARCHITECTURES)
    else:
        if not isinstance(candidates, list) or not candidates:
            raise SynthesizeError(
                "request.arithmetic.candidates must be a non-empty array of "
                "architecture names"
            )
        resolved_candidates = []
        for entry in candidates:
            try:
                name = normalize_architecture(entry)
            except ArithGenError as exc:
                raise SynthesizeError(f"request.arithmetic.candidates: {exc}") from exc
            if name not in resolved_candidates:
                resolved_candidates.append(name)
        if adders != "auto":
            raise SynthesizeError(
                "request.arithmetic.candidates only applies to "
                'arithmetic.adders: "auto" -- an explicit architecture is '
                "already the only candidate"
            )

    verify_adders = arithmetic.get("verify_adders", True)
    if not isinstance(verify_adders, bool):
        raise SynthesizeError("request.arithmetic.verify_adders must be a boolean")

    return {
        "adders": adders,
        "min_width": min_width,
        "candidates": resolved_candidates,
        "verify_adders": verify_adders,
    }


def _run_arithmetic_selection(
    *,
    config: dict[str, Any],
    output_dir: str,
    resolved_sources: list[str],
    hdl_toplevel: str,
    engine_options: _EngineOptions,
    equiv_timeout_s: float | None,
    repo_root: str | None = None,
) -> tuple[dict[str, Any], tuple[str, ...], str | None]:
    """Resolve ``request.arithmetic`` into a concrete adder substitution.

    Three stages, all of which can end in "substitute nothing" without
    failing the run:

    1. **Probe.** One extra Yosys pass (:func:`_probe_add_widths`) dumps the
       elaborated design as JSON and reads the ``Y_WIDTH`` of every ``$add``
       cell. Nothing at or above ``min_width`` means there is no wide adder
       to lever on -- reported as ``status: "no-wide-adders"``, with the
       run continuing exactly as it would have without the field.
    2. **Generate + prove.** Each candidate architecture is generated at
       every probed width and (unless ``verify_adders`` is ``false``) proven
       equivalent to a behavioural ``a + b + cin`` of the same width via
       ``klt equiv``. A candidate with an unproven adder is **disqualified**,
       never silently kept -- the issue's own equivalence gate.
    3. **Measure + select.** In ``"auto"`` mode each surviving candidate,
       plus Yosys's own default expansion, gets a full trial synthesis in its
       own artifacts directory, and the winner is the smallest one meeting
       ``constraints.clock_period_ns`` (see
       :func:`_select_arithmetic_candidate` for the complete rule, including
       what happens when none does). With an explicit architecture there is
       nothing to select, so no trials are run at all -- the one requested
       architecture is substituted directly and measured by the real run.

    Returns ``(report, adder_sources, techmap_path)``: the response's
    ``arithmetic`` field, the generated Verilog files the real synthesis
    script must ``read_verilog``, and the ``techmap -map`` rule file it must
    apply (both empty/``None`` when nothing is substituted).
    """
    mode = config["adders"]
    arith_dir = os.path.join(output_dir, "arith")
    report: dict[str, Any] = {
        "mode": "auto" if mode == "auto" else "explicit",
        "requested": mode,
        "min_width": config["min_width"],
        "status": "ok",
        "reason": None,
        "adder_widths": [],
        "target_period_ns": engine_options.target_period_ns,
        "selected_architecture": DEFAULT_ADDER_LABEL,
        "candidates": [],
        "selected_measured": None,
    }

    if mode == DEFAULT_ADDER_LABEL:
        report["status"] = "not-requested"
        report["reason"] = (
            'arithmetic.adders: "default" leaves Yosys\'s own $add expansion '
            "in place -- nothing was substituted or measured"
        )
        return report, (), None

    widths = _probe_add_widths(
        resolved_sources=resolved_sources,
        hdl_toplevel=hdl_toplevel,
        output_dir=arith_dir,
        min_width=config["min_width"],
    )
    report["adder_widths"] = widths
    if not widths:
        report["status"] = "no-wide-adders"
        report["reason"] = (
            f"no $add cell of width >= {config['min_width']} survives "
            f"elaboration of '{hdl_toplevel}' -- nothing was substituted"
        )
        return report, (), None

    architectures = list(config["candidates"]) if mode == "auto" else [mode]
    prepared: dict[str, dict[str, Any]] = {}
    for architecture in architectures:
        prepared[architecture] = _prepare_adder_candidate(
            architecture=architecture,
            widths=widths,
            arith_dir=arith_dir,
            verify_adders=config["verify_adders"],
            equiv_timeout_s=equiv_timeout_s,
        )

    if mode != "auto":
        entry = prepared[mode]
        if entry["disqualified_reason"] is not None:
            raise SynthesizeError(
                f"arithmetic.adders: '{mode}' could not be substituted: "
                f"{entry['disqualified_reason']}"
            )
        report["selected_architecture"] = mode
        report["candidates"] = [
            {
                "architecture": mode,
                "prefix_cells": entry["prefix_cells"],
                "logic_levels": entry["logic_levels"],
                "max_fanout": entry["max_fanout"],
                "adder_equivalence": entry["adder_equivalence"],
                "disqualified_reason": None,
                "measured": None,
            }
        ]
        return report, tuple(entry["sources"]), entry["techmap_path"]

    rows: list[dict[str, Any]] = []
    default_row = {
        "architecture": DEFAULT_ADDER_LABEL,
        "prefix_cells": None,
        "logic_levels": None,
        "max_fanout": None,
        "adder_equivalence": [],
        "disqualified_reason": None,
        "measured": _measure_candidate(
            label=DEFAULT_ADDER_LABEL,
            trial_dir=os.path.join(arith_dir, DEFAULT_ADDER_LABEL),
            resolved_sources=resolved_sources,
            hdl_toplevel=hdl_toplevel,
            engine_options=engine_options,
            adder_sources=(),
            adder_techmap_path=None,
            repo_root=repo_root,
        ),
    }
    rows.append(default_row)

    for architecture in architectures:
        entry = prepared[architecture]
        row: dict[str, Any] = {
            "architecture": architecture,
            "prefix_cells": entry["prefix_cells"],
            "logic_levels": entry["logic_levels"],
            "max_fanout": entry["max_fanout"],
            "adder_equivalence": entry["adder_equivalence"],
            "disqualified_reason": entry["disqualified_reason"],
            "measured": None,
        }
        if entry["disqualified_reason"] is None:
            row["measured"] = _measure_candidate(
                label=architecture,
                trial_dir=os.path.join(arith_dir, architecture),
                resolved_sources=resolved_sources,
                hdl_toplevel=hdl_toplevel,
                engine_options=engine_options,
                adder_sources=tuple(entry["sources"]),
                adder_techmap_path=entry["techmap_path"],
                repo_root=repo_root,
            )
        rows.append(row)

    report["candidates"] = rows
    winner, reason = _select_arithmetic_candidate(rows, engine_options.target_period_ns)
    report["reason"] = reason
    if winner is None:
        report["status"] = "no-candidate"
        report["selected_architecture"] = DEFAULT_ADDER_LABEL
        return report, (), None

    report["selected_architecture"] = winner["architecture"]
    if winner["architecture"] == DEFAULT_ADDER_LABEL:
        return report, (), None
    entry = prepared[winner["architecture"]]
    return report, tuple(entry["sources"]), entry["techmap_path"]


def _select_arithmetic_candidate(
    rows: list[dict[str, Any]], target_period_ns: float | None
) -> tuple[dict[str, Any] | None, str | None]:
    """Pick the winning candidate row, and explain the pick when it is not
    the straightforward one.

    The rule, in order:

    1. Candidates that **met** ``constraints.clock_period_ns`` -> the one
       with the smallest ``area_um2`` (the issue's "keeps the one meeting
       ``clock_period_ns`` at least area"), ties broken on delay then on the
       candidate order in :data:`ADDER_ARCHITECTURES`.
    2. No target given -> the fastest candidate, ties broken on area. A
       caller who stated no period asked for the best structure available,
       not for "whatever the default did".
    3. A target given but **nothing met it** -> the fastest candidate, with a
       ``reason`` naming the target and the best delay achieved. This is the
       acceptance criterion's "or the JSON says why none did": the run still
       returns a netlist and the caller can see exactly how far short every
       architecture fell.
    4. No candidate produced a delay number at all (no ``sta`` extension and
       no ABC ``stime`` line) -> the smallest area, with a ``reason`` saying
       the selection was made without timing data.

    Returns ``(None, reason)`` only when every candidate was disqualified by
    the equivalence gate or failed to synthesize.
    """
    eligible = [
        row
        for row in rows
        if row["disqualified_reason"] is None and row["measured"] is not None
    ]
    if not eligible:
        return None, (
            "every arithmetic candidate was disqualified -- see each "
            "candidate's disqualified_reason"
        )

    def _area(row: dict[str, Any]) -> float:
        area = row["measured"]["area_um2"]
        return float("inf") if area is None else area

    def _delay(row: dict[str, Any]) -> float | None:
        return row["measured"]["delay_ns"]

    timed = [row for row in eligible if _delay(row) is not None]

    if not timed:
        winner = min(eligible, key=_area)
        return winner, (
            "no candidate produced a delay measurement (neither the native "
            "sta stage nor ABC's stime report was available) -- selected the "
            "smallest area instead of the fastest structure"
        )

    if target_period_ns is not None:
        meeting = [row for row in timed if _delay(row) <= target_period_ns]
        if meeting:
            winner = min(meeting, key=lambda row: (_area(row), _delay(row)))
            return winner, None
        best = min(timed, key=lambda row: (_delay(row), _area(row)))
        return best, (
            f"no candidate met constraints.clock_period_ns="
            f"{target_period_ns} ns; the fastest was "
            f"'{best['architecture']}' at {_delay(best)} ns -- selected it "
            "anyway as the closest available structure"
        )

    winner = min(timed, key=lambda row: (_delay(row), _area(row)))
    return winner, None


def _probe_add_widths(
    *,
    resolved_sources: list[str],
    hdl_toplevel: str,
    output_dir: str,
    min_width: int,
) -> list[int]:
    """Return the sorted, distinct result widths of the ``$add`` cells the
    elaborated design contains, filtered to ``>= min_width``.

    Runs one extra, cheap Yosys pass (``read_verilog`` -> ``hierarchy`` ->
    ``proc`` -> ``opt_expr``/``opt_clean`` -> ``write_json``) and reads the
    cells straight out of the emitted JSON. This is deliberately **not**
    regexed out of the RTL: a ``+`` in the source can be constant-folded
    away, widened by context, or shared, and only the elaborated netlist
    knows the width Yosys will actually build -- which is the width the
    ``techmap`` rule has to match on.
    """
    try:
        os.makedirs(output_dir, exist_ok=True)
    except OSError as exc:
        raise SynthesizeError(
            f"could not create arithmetic output directory '{output_dir}': {exc}"
        ) from exc

    script_path = os.path.join(output_dir, f"probe_{hdl_toplevel}.ys")
    json_path = os.path.join(output_dir, f"probe_{hdl_toplevel}.json")
    lines = [f"read_verilog {path}" for path in resolved_sources]
    lines += [
        f"hierarchy -check -top {hdl_toplevel}",
        "proc",
        "opt_expr",
        "opt_clean",
        f"write_json {json_path}",
    ]
    try:
        with open(script_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError as exc:
        raise SynthesizeError(
            f"could not write arithmetic probe script '{script_path}': {exc}"
        ) from exc

    _run_yosys(script_path)

    try:
        with open(json_path, encoding="utf-8") as handle:
            document = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise SynthesizeError(
            f"could not read the arithmetic probe output '{json_path}': {exc}"
        ) from exc

    widths: set[int] = set()
    for module in (document.get("modules") or {}).values():
        for cell in (module.get("cells") or {}).values():
            if cell.get("type") != "$add":
                continue
            width = _parse_yosys_param_int(
                (cell.get("parameters") or {}).get("Y_WIDTH")
            )
            if width is not None and width >= min_width:
                widths.add(width)
    return sorted(widths)


def _parse_yosys_param_int(value: Any) -> int | None:
    """Decode a Yosys ``write_json`` cell parameter as an integer.

    Yosys emits parameters either as plain JSON integers or as MSB-first
    binary strings (``"00000000000000000000000000010010"`` for 18) depending
    on the parameter's declared type and width -- both spellings appear in
    the same file. Anything else (``"x"``/``"z"`` bits, a non-numeric string)
    is reported as ``None`` rather than guessed at.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text and set(text) <= {"0", "1"}:
            return int(text, 2)
    return None


def _prepare_adder_candidate(
    *,
    architecture: str,
    widths: list[int],
    arith_dir: str,
    verify_adders: bool,
    equiv_timeout_s: float | None,
) -> dict[str, Any]:
    """Generate one architecture's adder modules (one per probed width) plus
    the shared ``techmap`` rule file, and -- when ``verify_adders`` is set --
    prove each of them equivalent to a behavioural ``a + b + cin``.

    The proof is the issue's step 3 gate, deliberately scoped to the
    *generated adder*, not to the whole substituted design: it is what makes
    "this structure computes addition" a checked claim rather than an assumed
    one, it is cheap (an N-bit adder miter, far cheaper than the whole
    design), and -- unlike a whole-design check -- it works even when the
    design itself is sequential, which the fleet's adder-bound canaries are.

    Never raises for a failed proof: the candidate comes back with a
    ``disqualified_reason``, which the ``auto`` sweep drops from the table
    and an explicit request turns into a hard :class:`SynthesizeError` one
    level up.
    """
    candidate_dir = os.path.join(arith_dir, architecture, "rtl")
    try:
        os.makedirs(candidate_dir, exist_ok=True)
    except OSError as exc:
        raise SynthesizeError(
            f"could not create adder output directory '{candidate_dir}': {exc}"
        ) from exc

    sources: list[str] = []
    techmap_entries: dict[int, str] = {}
    equivalence: list[dict[str, Any]] = []
    disqualified: str | None = None
    prefix_cells = 0
    logic_levels = 0
    max_fanout = 0

    for width in widths:
        try:
            built = generate_adder(width=width, architecture=architecture)
        except ArithGenError as exc:
            raise SynthesizeError(
                f"could not generate a {width}-bit '{architecture}' adder: {exc}"
            ) from exc

        module = built["module_name"]
        adder_path = os.path.join(candidate_dir, f"{module}.v")
        reference_path = os.path.join(candidate_dir, f"{module}_ref.v")
        _write_text_file(adder_path, built["verilog"])
        _write_text_file(reference_path, built["reference_verilog"])
        sources.append(adder_path)
        techmap_entries[width] = module
        prefix_cells += built["metrics"]["prefix_cells"]
        logic_levels = max(logic_levels, built["metrics"]["logic_levels"])
        max_fanout = max(max_fanout, built["metrics"]["max_fanout"])

        if not verify_adders:
            continue
        verdict = _verify_generated_adder(
            candidate_dir=candidate_dir,
            module=module,
            reference_module=built["reference_name"],
            adder_path=adder_path,
            reference_path=reference_path,
            width=width,
            timeout_s=equiv_timeout_s,
        )
        equivalence.append(verdict)
        if verdict["status"] != "equivalent" and disqualified is None:
            disqualified = (
                f"the generated {width}-bit '{architecture}' adder was not "
                f"proven equivalent to `a + b + cin` (klt equiv reported "
                f"'{verdict['status']}'"
                + (f": {verdict['detail']}" if verdict["detail"] else "")
                + ")"
            )

    techmap_path = os.path.join(candidate_dir, f"{architecture}_add_techmap.v")
    try:
        _write_text_file(techmap_path, emit_techmap_verilog(techmap_entries))
    except ArithGenError as exc:  # pragma: no cover - widths is non-empty
        raise SynthesizeError(str(exc)) from exc

    return {
        "architecture": architecture,
        "sources": sources,
        "techmap_path": techmap_path,
        "adder_equivalence": equivalence,
        "disqualified_reason": disqualified,
        "prefix_cells": prefix_cells,
        "logic_levels": logic_levels,
        "max_fanout": max_fanout,
    }


def _verify_generated_adder(
    *,
    candidate_dir: str,
    module: str,
    reference_module: str,
    adder_path: str,
    reference_path: str,
    width: int,
    timeout_s: float | None,
) -> dict[str, Any]:
    """Prove one generated adder equivalent to its behavioural reference via
    :func:`klayout_tools.equiv.run_equiv`, returning
    ``{width, status, detail}``.

    A :class:`~klayout_tools.equiv.EquivError` (a missing Yosys, an
    unreadable source) is reported as ``status: "error"`` with the message as
    ``detail`` -- the caller decides whether that disqualifies the candidate
    or fails the run, exactly as it does for a ``"counterexample"``.
    """
    request_path = os.path.join(candidate_dir, f"{module}_equiv_request.json")
    request = {
        "gold": {"sources": [reference_path], "top": reference_module},
        "gate": {"sources": [adder_path], "top": module},
    }
    try:
        with open(request_path, "w", encoding="utf-8") as handle:
            json.dump(request, handle, indent=2)
    except OSError as exc:
        raise SynthesizeError(
            f"could not write adder equivalence request '{request_path}': {exc}"
        ) from exc

    try:
        report = run_equiv(request_path, timeout_s=timeout_s)
    except EquivError as exc:
        return {"width": width, "status": "error", "detail": str(exc)}

    detail = None
    if report["status"] == "counterexample":
        diverging = (report.get("counterexample") or {}).get("diverging_outputs")
        if diverging:
            detail = f"diverging outputs: {', '.join(diverging)}"
    elif report["status"] != "equivalent":
        diagnostics = report.get("diagnostics") or []
        if diagnostics:
            detail = diagnostics[0]["message"]
    return {"width": width, "status": report["status"], "detail": detail}


def _measure_candidate(
    *,
    label: str,
    trial_dir: str,
    resolved_sources: list[str],
    hdl_toplevel: str,
    engine_options: _EngineOptions,
    adder_sources: tuple[str, ...],
    adder_techmap_path: str | None,
    repo_root: str | None = None,
) -> dict[str, Any] | None:
    """Run one full trial synthesis for a candidate architecture and report
    what it measured.

    Uses exactly the engine configuration the real run will use (same
    liberty, same ``-constr``/``-D``/``-dont_use``/``hilomap`` knobs), into
    its own ``.klt/synthesize/arith/<label>/`` directory so every trial's
    script, netlist, stats and ABC log survive as debuggable artifacts --
    "measured, not guessed" is only a real claim if the measurement is
    reproducible afterwards.

    Returns ``None`` when this candidate's trial synthesis fails outright,
    which disqualifies it from selection without failing the whole run: one
    architecture Yosys cannot map is not a reason to abandon the other four.
    """
    try:
        os.makedirs(trial_dir, exist_ok=True)
    except OSError as exc:
        raise SynthesizeError(
            f"could not create arithmetic trial directory '{trial_dir}': {exc}"
        ) from exc

    script_path = os.path.join(trial_dir, f"synth_{hdl_toplevel}.ys")
    netlist_path = os.path.join(trial_dir, f"{hdl_toplevel}_synth.v")
    stats_path = os.path.join(trial_dir, f"{hdl_toplevel}_stats.json")
    constr_path: str | None = None
    abc_log_path: str | None = None
    if engine_options.constr_inputs is not None:
        constr_path = os.path.join(trial_dir, f"{hdl_toplevel}_abc.constr")
        abc_log_path = os.path.join(trial_dir, f"{hdl_toplevel}_abc.log")
        _write_constr(constr_path, *engine_options.constr_inputs)

    _write_script(
        script_path=script_path,
        sources=resolved_sources,
        hdl_toplevel=hdl_toplevel,
        liberty_path=engine_options.liberty_path,
        stats_path=stats_path,
        netlist_path=netlist_path,
        constr_path=constr_path,
        abc_log_path=abc_log_path,
        delay_target_ps=engine_options.delay_target_ps,
        dont_use_globs=engine_options.dont_use_globs,
        tie_cells=engine_options.tie_cells,
        adder_sources=adder_sources,
        adder_techmap_path=adder_techmap_path,
    )

    try:
        _run_yosys(script_path)
        module_stats = _read_stats(stats_path, hdl_toplevel)
    except SynthesizeError:
        return None

    measurement = _candidate_measurement(
        instance_count=module_stats["num_cells"],
        area_um2=module_stats["area"],
        sta=_read_sta_timing(netlist_path, engine_options.liberty_path, hdl_toplevel),
        abc_timing=_read_abc_timing(abc_log_path, engine_options.delay_target_ps),
        target_period_ns=engine_options.target_period_ns,
    )
    measurement["label"] = label
    # Issue #1844: only the values reported back in `candidates[].measured`
    # are normalized -- `netlist_path`/`script_path` above stay the real
    # absolute paths used for this trial's own I/O throughout this function.
    measurement["netlist_path"] = _report_path(netlist_path, repo_root=repo_root)
    measurement["script_path"] = _report_path(script_path, repo_root=repo_root)
    return measurement


def _candidate_measurement(
    *,
    instance_count: int,
    area_um2: float | None,
    sta: dict[str, Any] | None,
    abc_timing: dict[str, Any] | None,
    target_period_ns: float | None,
) -> dict[str, Any]:
    """Normalise one candidate's measured QoR into the comparable triple the
    selection rule uses.

    ``delay_ns`` prefers the native whole-netlist ``sta`` worst path and
    falls back to ABC's own ``stime`` estimate, recording which one it used
    in ``delay_source`` -- the two are **not** interchangeable numbers (see
    this module's docstring and ``docs/cli/synthesize.md``), so a caller
    comparing two runs must check they came from the same source. Every
    candidate in a single sweep is measured the same way, so the *ranking*
    within one report is always consistent even when the extension is
    missing.
    """
    delay_ns: float | None = None
    delay_source: str | None = None
    worst_path = (sta or {}).get("worst_path") if sta else None
    if worst_path and worst_path.get("delay_ns") is not None:
        delay_ns = float(worst_path["delay_ns"])
        delay_source = "sta"
    elif abc_timing and abc_timing.get("critical_path_ps") is not None:
        delay_ns = float(abc_timing["critical_path_ps"]) / 1000.0
        delay_source = "abc_stime"

    meets = None
    if target_period_ns is not None and delay_ns is not None:
        meets = delay_ns <= target_period_ns
    return {
        "instance_count": instance_count,
        "area_um2": area_um2,
        "delay_ns": delay_ns,
        "delay_source": delay_source,
        "meets_constraint": meets,
    }


def _write_text_file(path: str, text: str) -> None:
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
    except OSError as exc:
        raise SynthesizeError(f"could not write '{path}': {exc}") from exc


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

    Thin wrapper around :func:`klayout_tools.pdk.resolve_liberty_for_cell_library`
    (issue #1652) -- that shared implementation (including the IHP
    single-underscore liberty-filename fallback, issue #1790) is what
    ``place_and_route.py`` and ``post_route_sta.py``'s own
    ``_resolve_liberty`` wrappers call too, so all three modules stay in
    sync by construction; only the exception type raised on each failure
    differs per module.
    """
    return resolve_liberty_for_cell_library(
        cell_library, requested_corner, SynthesizeError, variant=variant, root=root
    )


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
    takes toward those builds (#560). Never raises -- including on a
    timeout (issue #1775): bounded by the same
    :data:`DEFAULT_YOSYS_TIMEOUT_S` :func:`_run_yosys` uses, since this is
    still a Yosys invocation that could in principle hang, but a stuck
    capability probe degrades to "assume unsupported" rather than raising,
    exactly like the missing-binary (``OSError``) and nonzero-exit cases
    right below.
    """
    try:
        completed = subprocess.run(
            ["yosys", "-p", "help abc"],
            capture_output=True,
            text=True,
            timeout=DEFAULT_YOSYS_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
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
    adder_sources: tuple[str, ...] = (),
    adder_techmap_path: str | None = None,
    repo_root: str | None = None,
    pdk_root: str | None = None,
) -> str:
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

    ``adder_techmap_path`` (issue #1722, the request's ``arithmetic`` field)
    inserts the arithmetic-architecture substitution **between**
    ``hierarchy`` and ``synth``::

        proc
        opt_expr
        opt_clean
        read_verilog <each adder_sources entry>
        techmap -map <adder_techmap_path>

    Its position is load-bearing in both directions. It must run **after**
    ``hierarchy -check -top`` so the freshly-read adder modules are not
    pruned as unused before anything instantiates them (``hierarchy`` removes
    unreferenced modules), and **before** ``synth``, whose own ``alumacc``
    step rewrites every surviving ``$add`` into ``$alu``/``$lcu`` -- after
    which there is no ``$add`` left for a ``techmap`` rule to match. ``proc``
    is run first so the ``$add`` cells inside always-blocks are visible as
    plain cells; ``synth`` runs ``proc`` again itself, which is idempotent.
    Any ``$add`` the rule file declines (via ``_TECHMAP_FAIL_`` -- a width
    that was not generated) is left untouched for Yosys's own expansion, so
    this is always a subset substitution.

    Commit-safety vs. cwd-independence (issue #1844): every path embedded in
    the script used to be absolute unconditionally, so the script ran
    correctly regardless of the invoking process's own working directory --
    :func:`_run_yosys` never set ``cwd=``. A generated ``.ys`` is exactly
    the kind of artifact a harness wants to commit as reproducible evidence
    (``docs/design/sim-evidence-discipline-spike.md``), and an absolute path
    in it leaks the author's home directory / Loom worktree layout the same
    way an unnormalized response field does.

    ``repo_root``, when given (:func:`~klayout_tools.env_provenance.
    find_repo_root`'s answer for the request that produced this script),
    resolves that tension by rewriting -- only for a path that resolves
    *inside* ``repo_root`` -- the absolute form to a path relative to
    ``repo_root`` instead: every ``sources``/``adder_sources`` entry,
    ``stats_path``, ``netlist_path``, ``constr_path``, and ``abc_log_path``.
    Any path that does not resolve inside ``repo_root`` (or when
    ``repo_root`` is ``None`` -- no repo found for this request at all)
    keeps the original absolute form unchanged, exactly as before this
    issue.

    A script written with a relative form for any path **must** be run with
    ``cwd=repo_root`` (:func:`_run_yosys`'s own ``cwd`` keyword) -- passing
    ``repo_root`` here and then invoking with no explicit ``cwd=`` (or a
    different one) would send a relative ``read_verilog``/``write_verilog``
    argument to the wrong directory. ``repo_root=None`` (every call site in
    this module except :func:`run_synthesize`'s own top-level script -- the
    trial/probe/baseline scripts stay fully absolute and fully
    cwd-independent, unchanged) reproduces the original invariant exactly:
    no relative path is ever written, so no ``cwd=`` is ever required.

    The resolved liberty (issue #1870, the half #1844 scoped out) is the one
    path ``repo_root`` cannot help with: a PDK install essentially never
    lives inside the repo, so repo-relative rewriting leaves it absolute --
    and on the common install layouts (``~/.ciel``, ``~/.volare``) that
    absolute path is home-directory-shaped, so a freshly generated ``.ys``
    still failed ``klt env-provenance scan``. ``pdk_root`` (the resolved
    install root :func:`_resolve_liberty` already returns as
    ``pdk_info["root"]``) fixes that by writing the liberty as
    ``$PDK_ROOT/<path relative to that root>`` -- machine-independent, and
    therefore commit-safe -- via :func:`_script_liberty_text`.

    Yosys does not expand environment variables in a script file, so the
    committed form and the executed form necessarily differ. That difference
    is made **explicit and testable** rather than implicit: whenever a
    :data:`PDK_ROOT_TOKEN` is written, this function also writes the
    rehydrated sibling :func:`run_script_path` names
    (``synth_<top>.run.ys``), which is byte-identical apart from its
    one-line header and the substituted liberty, and **returns that
    sibling's path** -- the script the caller must hand to
    :func:`_run_yosys`. With ``pdk_root=None``, or a liberty installed
    outside ``pdk_root``, no token is written, no sibling is emitted, and the
    return value is ``script_path`` itself: the pre-#1870 behaviour exactly.

    Returns the path of the script to execute (``script_path``, or its
    rehydrated sibling) -- never the artifact to commit, which is always
    ``script_path``.
    """
    liberty_text = _script_liberty_text(liberty_path, pdk_root=pdk_root)
    stats_text = _script_path_text(stats_path, repo_root=repo_root)
    netlist_text = _script_path_text(netlist_path, repo_root=repo_root)
    constr_text = (
        None
        if constr_path is None
        else _script_path_text(constr_path, repo_root=repo_root)
    )
    abc_log_text = (
        None
        if abc_log_path is None
        else _script_path_text(abc_log_path, repo_root=repo_root)
    )

    abc_command = f"abc -liberty {liberty_text}"
    if constr_text is not None:
        abc_command += f" -constr {constr_text}"
    if delay_target_ps is not None:
        abc_command += f" -D {delay_target_ps}"
    for glob in dont_use_globs:
        abc_command += f" -dont_use {glob}"
    if constr_text is not None and abc_log_text is not None:
        abc_command = f"tee -q -o {abc_log_text} {abc_command}"

    lines = [
        f"read_verilog {_script_path_text(path, repo_root=repo_root)}"
        for path in sources
    ]
    lines.append(f"hierarchy -check -top {hdl_toplevel}")
    if adder_techmap_path is not None:
        lines += ["proc", "opt_expr", "opt_clean"]
        lines += [
            f"read_verilog {_script_path_text(path, repo_root=repo_root)}"
            for path in adder_sources
        ]
        lines.append(
            f"techmap -map {_script_path_text(adder_techmap_path, repo_root=repo_root)}"
        )
    lines += [
        f"synth -top {hdl_toplevel}",
        f"dfflibmap -liberty {liberty_text}",
        abc_command,
        "clean",
    ]
    if tie_cells is not None:
        (hi_cell, hi_port), (lo_cell, lo_port) = tie_cells
        lines.append(f"hilomap -hicell {hi_cell} {hi_port} -locell {lo_cell} {lo_port}")
    lines += [
        f"tee -q -o {stats_text} "
        f"stat -liberty {liberty_text} -json -top {hdl_toplevel}",
        f"write_verilog -noattr {netlist_text}",
    ]
    script_text = "\n".join(lines) + "\n"

    if pdk_root is None or PDK_ROOT_TOKEN not in script_text:
        _write_script_text(script_path, script_text)
        return script_path

    run_path = run_script_path(script_path)
    _write_script_text(
        script_path,
        _COMMIT_SCRIPT_HEADER.format(run_script=os.path.basename(run_path))
        + script_text,
    )
    _write_script_text(
        run_path,
        _RUN_SCRIPT_HEADER.format(script=os.path.basename(script_path))
        + rehydrate_script_text(script_text, pdk_root=pdk_root),
    )
    return run_path


def _write_script_text(script_path: str, text: str) -> None:
    """Write one generated ``.ys`` file, raising :class:`SynthesizeError` on
    any I/O failure -- shared by the commit-safe script and its rehydrated
    sibling (issue #1870) so both report a write failure identically."""
    try:
        with open(script_path, "w", encoding="utf-8") as handle:
            handle.write(text)
    except OSError as exc:
        raise SynthesizeError(
            f"could not write synthesis script '{script_path}': {exc}"
        ) from exc


#: Default per-invocation wall-clock timeout for a single ``yosys -s
#: <script>`` run (:func:`_run_yosys`), overridable per call via its own
#: ``timeout_s`` keyword. Mirrors ``klayout_tools.equiv.DEFAULT_TIMEOUT_S``'s
#: role (bound a hang, not a slow-but-legitimate run) but is deliberately
#: more generous: a full ``synth``/``dfflibmap``/``abc`` mapping pass over a
#: real design does substantially more work than ``equiv``'s SAT proof, and
#: in ``"auto"`` arithmetic-architecture mode (issue #1775, following
#: #1772) this same budget is paid up to ~10+ times per ``klt synthesize``
#: request, so it must not be so tight that a legitimately-slow-but-healthy
#: trial synthesis is mistaken for a hang. A fixed module constant (no
#: request-level override/CLI flag) was chosen over threading a new
#: ``yosys_timeout_s`` end-to-end alongside the existing ``equiv_timeout_s``
#: precedent -- lower churn, and every call site already goes through this
#: one function.
DEFAULT_YOSYS_TIMEOUT_S = 300.0


def _run_yosys(
    script_path: str,
    *,
    timeout_s: float | None = DEFAULT_YOSYS_TIMEOUT_S,
    cwd: str | None = None,
) -> str:
    """Invoke ``yosys -s <script_path>`` and raise :class:`SynthesizeError`
    on any failure to run (missing binary, non-zero exit, or a run that
    exceeds ``timeout_s`` -- default :data:`DEFAULT_YOSYS_TIMEOUT_S`, pass
    ``None`` for no timeout).

    Never raises on a *successful* (exit 0) run -- the caller is responsible
    for validating the declared output files actually appeared. Returns the
    captured ``stdout`` log text on success -- Yosys's own ``Warning: ``
    lines land on stdout, never stderr (verified live; matches
    :func:`_synthesis_error_message`'s own stream-preference note for
    ``ERROR:`` lines) -- so :func:`_compute_structural`/
    :func:`_summarize_warnings` (issue #1588) can parse it without a second
    Yosys invocation.

    A timed-out run raises :class:`SynthesizeError` exactly like any other
    Yosys failure (never a bare ``subprocess.TimeoutExpired``) -- this is
    what lets :func:`_measure_candidate` (issue #1775) treat one hung
    arithmetic-architecture candidate's trial synthesis the same way it
    already treats any other failed trial: disqualified, not fatal to the
    rest of the ``"auto"`` sweep.

    ``cwd`` (issue #1844) is passed straight through to
    ``subprocess.run``'s own ``cwd``; ``script_path`` itself is always
    given as an absolute path, so ``cwd`` never affects locating the script
    -- it only matters for a script :func:`_write_script` wrote with any
    relative-form embedded path (``repo_root`` given), which must be run
    with ``cwd=<that same repo_root>`` to resolve correctly. The default
    ``None`` reproduces the pre-#1844 behaviour exactly (subprocess's own
    default: the invoking process's cwd), correct for every script that
    embeds only absolute paths.
    """
    try:
        completed = subprocess.run(
            ["yosys", "-s", script_path],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=cwd,
        )
    except OSError as exc:
        raise SynthesizeError(f"could not launch yosys: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise SynthesizeError(
            f"yosys did not complete within {timeout_s}s (script "
            f"'{script_path}') -- process killed"
        ) from exc

    if completed.returncode != 0:
        raise SynthesizeError(_synthesis_error_message(completed))

    return completed.stdout or ""


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
            message += wasi_sandbox_hint_if_applicable(error_lines[-1])
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


def _leakage_unit_scale_to_nw(liberty_text: str) -> float:
    """The multiplier from this liberty's own ``leakage_power_unit`` to
    nanowatts (see :data:`_LEAKAGE_POWER_UNIT_RE`). Defaults to ``1.0`` --
    i.e. assumes the file's own ``cell_leakage_power`` numbers are already
    nanowatts, Liberty's conventional default unit -- when the file declares
    no ``leakage_power_unit`` at all, or names a unit :data:`_LEAKAGE_UNIT_TO_NW`
    does not recognise. Never raises, matching
    :func:`_abc_supports_dont_use`'s own "never break a run over an
    optional/best-effort probe" posture.
    """
    match = _LEAKAGE_POWER_UNIT_RE.search(liberty_text)
    if match is None:
        return 1.0
    try:
        magnitude = float(match.group("magnitude"))
    except ValueError:  # pragma: no cover - regex only matches numbers
        return 1.0
    per_unit_nw = _LEAKAGE_UNIT_TO_NW.get(match.group("unit").lower())
    if per_unit_nw is None:
        return 1.0
    return magnitude * per_unit_nw


def _parse_liberty_leakage_nw(liberty_text: str) -> dict[str, float]:
    """Read every ``cell (name) { ... cell_leakage_power : <value>; ... }``
    scalar out of ``liberty_text``, converted to nanowatts via that
    liberty's own ``leakage_power_unit`` (:func:`_leakage_unit_scale_to_nw`).

    Deliberately minimal, matching ``restructure.py``'s
    :func:`~klayout_tools.restructure._parse_liberty_pins`'s own scope
    discipline: this is not a general liberty parser, it extracts only the
    one scalar :func:`_compute_leakage` needs. A cell with no
    ``cell_leakage_power`` line (see :data:`_CELL_LEAKAGE_POWER_RE`'s
    docstring -- some libraries, e.g. gf180mcu_fd_sc_mcu9t5v0, report
    leakage only via per-input-state groups instead) is simply absent from
    the result, never assigned a guessed value.
    """
    unit_scale = _leakage_unit_scale_to_nw(liberty_text)
    headers = list(_LIBERTY_CELL_HEADER_RE.finditer(liberty_text))
    leakage_by_cell: dict[str, float] = {}
    for index, header in enumerate(headers):
        body_start = header.end()
        body_end = (
            headers[index + 1].start()
            if index + 1 < len(headers)
            else len(liberty_text)
        )
        match = _CELL_LEAKAGE_POWER_RE.search(liberty_text, body_start, body_end)
        if match is None:
            continue
        try:
            value = float(match.group("value"))
        except ValueError:  # pragma: no cover - regex only matches numbers
            continue
        # A later block for the same cell name (should not happen in a
        # well-formed liberty) overwrites, matching `_parse_liberty_pins`'s
        # own "last one wins" convention.
        leakage_by_cell[header.group("name")] = value * unit_scale
    return leakage_by_cell


def _compute_leakage(
    liberty_path: str, instance_counts_by_type: dict[str, int]
) -> tuple[float | None, dict[str, float] | None]:
    """``(leakage_power_nw, leakage_by_type_nw)`` for the response -- issue
    #1626. Static-leakage-only: no switching/dynamic power, which needs an
    activity factor this command has no vectors to supply and is explicitly
    out of scope. Computed as ``sum(cell_leakage_power[cell_type] *
    instance_count[cell_type])`` over the response's own
    ``instance_counts_by_type``, read from the same resolved liberty this
    module already loaded for ``dfflibmap``/``abc -liberty`` -- no second
    liberty fetch, no new PDK lookup.

    Returns ``(0.0, {})`` -- a real, not-fabricated zero, matching
    ``area_um2``'s own "``0.0`` for a design with no relevant cells" posture
    -- when ``instance_counts_by_type`` is itself empty (nothing to sum in
    the first place). Returns ``(None, None)`` -- never a fabricated/partial
    number silently passed off as the whole design's leakage -- when the
    liberty cannot be read, or when the design *does* instantiate cells but
    *none* of those types have a ``cell_leakage_power`` entry at all (e.g.
    gf180mcu_fd_sc_mcu9t5v0, which reports leakage only via per-input-state
    ``leakage_power()`` groups this module deliberately does not average --
    see :data:`_CELL_LEAKAGE_POWER_RE`'s docstring). When *some* (not all)
    instantiated types are covered, the total sums exactly those --
    ``leakage_by_type_nw`` (the bonus per-type breakdown the issue asks for)
    names precisely which types were covered, so a caller comparing its own
    keys against ``instance_counts_by_type`` gets a free check for a
    ``-dont_use``d or otherwise-unmapped cell type with no leakage data.
    """
    if not instance_counts_by_type:
        return 0.0, {}

    try:
        with open(liberty_path, encoding="utf-8") as handle:
            liberty_text = handle.read()
    except OSError:
        return None, None

    leakage_by_cell = _parse_liberty_leakage_nw(liberty_text)
    if not leakage_by_cell:
        return None, None

    leakage_by_type: dict[str, float] = {}
    total_nw = 0.0
    for cell_type, count in instance_counts_by_type.items():
        per_instance_nw = leakage_by_cell.get(cell_type)
        if per_instance_nw is None:
            continue
        leakage_by_type[cell_type] = per_instance_nw
        total_nw += per_instance_nw * count

    if not leakage_by_type:
        return None, None
    return total_nw, dict(sorted(leakage_by_type.items()))


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
    repo_root: str | None = None,
) -> dict[str, Any] | None:
    """Build the response's optional ``baseline`` field (issue #1588) --
    ``None`` unless ``request.baseline`` names a prior run to compare
    against, via exactly one of ``response_path`` (a previously captured
    ``klt synthesize --format json`` response file) or ``netlist_path`` (a
    bare mapped netlist, re-``stat``/re-timed against this run's own
    resolved liberty).

    ``ref`` identifies what was compared against: ``request.baseline.ref``
    when given, else -- since issue #1844 -- the resolved ``response_path``/
    ``netlist_path``'s repo-relative form (via :func:`_baseline_ref_fallback`,
    never the absolute path). Always present, never ``null``, so a caller
    can always tell what a ``baseline`` object was measured against even
    without an explicit label.

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
        ref = ref or _baseline_ref_fallback(resolved, repo_root=repo_root)
    else:
        resolved = (
            netlist_path
            if os.path.isabs(netlist_path)
            else os.path.join(request_dir, netlist_path)
        )
        baseline_metrics = _baseline_metrics_from_netlist(
            resolved, liberty_path, hdl_toplevel, output_dir
        )
        ref = ref or _baseline_ref_fallback(resolved, repo_root=repo_root)

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

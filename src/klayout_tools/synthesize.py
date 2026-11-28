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
``klayout_tools.pdk_cells.list_cell_libraries`` -- the same ``libs_ref``
discovery ``klt pdk``/``klt cells`` already use (Yosys survey section 0) --
**no new PDK-fetch mechanism**. ``request.pdk`` carries only
``cell_library``/``corner`` (no
``variant``/``root``): the resolved PDK install is whatever ``find_pdk()``'s
own default search order (``$PDK_ROOT``/``$PDK``, then the ciel/volare
stores, then the conventional prefixes) finds, exactly as ``klt pdk find``
with no flags would (falling back to the first installed variant that ships
``cell_library`` when the default pick lacks it and no ``--pdk``/``$PDK``
was given, issue #4204) -- unless the CLI's own ``--pdk``/``--pdk-root`` flags
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
already picks is used, via :func:`klayout_tools.pdk_cells.list_cell_libraries`.

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
not analyze this particular netlist/liberty pair, and the sibling
``sta_unavailable_reason`` field (issue #2628) says which of the two it was;
see ``docs/cli/synthesize.md``'s ``sta`` section for the full caveat this
inherits from the spike unresolved (no wire delay/parasitics, no SDC).

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

``macros`` (issue #2635, optional) is the shared hard-macro declaration --
one shape, documented once in :mod:`klayout_tools.macros`, accepted
identically by ``klt sta`` and (plus its own placement fields) by ``klt
place-and-route``. Each entry names a pre-characterized block the design
instantiates but carries no RTL for, and this module's generated ``.ys``
loads its liberty (``read_liberty -lib``) or a blackbox declaration
(``read_verilog -lib``, generated from the LEF when the caller supplies
neither) **before** ``hierarchy``, so the macro survives as one opaque
instance instead of being implemented out of standard cells. Without it,
Yosys aborts with ``Module `\\<cell>' referenced in module `\\<top>' in
cell `\\<inst>' is not part of the design``. The response reports macro
instances separately (``macros``, ``macro_instance_counts_by_type``,
``macro_instance_count``) and names every instantiated cell type whose area
``area_um2`` does *not* account for (``cells_without_area``, plus a
``warnings`` entry) -- a blackboxed macro contributes ``0`` to ``area_um2``
by construction, and that used to be silent. See
:func:`_macro_blackbox_inputs` / :func:`_cells_without_area`.
"""

from __future__ import annotations

import fnmatch
import functools
import importlib
import json
import os
import re
import subprocess
import tempfile
import uuid
from typing import Any

from . import env_provenance, synthesize_abc
from . import macros as macro_spec
from . import synthesize_abc_stages as abc_stages
from . import synthesize_timeout as ytimeout
from ._digest import netlist_digest_fields
from ._engine_proc import run_in_group
from ._jsonnum import is_finite_number, is_int, is_number
from ._paths import _load_request_json, _reject_unknown_keys, validate_request_shape
from ._provenance import (
    INPUT_ROLE_SOURCE,
    _combined_content_hash,
    _yosys_version,
    build_provenance,
    content_hash,
    sha256_file,
)
from ._report_verify import (
    build_check_result,
    get_path,
    hash_check,
    load_committed_report,
    resolve_committed_path,
    strip_keys,
)
from ._sv2v import convert_sources, validate_sv_frontend
from ._verilog_params import (
    validate_defines,
    validate_parameters,
)
from ._yosys_error_messages import synthesis_error_message
from .equiv import EquivError, run_equiv
from .equiv_verdict import require_equivalence
from .evidence_canonicalize import rerun_result_for
from .liberty_compare import LibertyCompareError, parse_liberty_text
from .pdk_cells import resolve_liberty_for_cell_library
from .request_paths import RequestPaths, resolve_readable_files
from .restructure import RestructureError, restructure_for_timing
from .sta import StaError, compute_critical_path

# The arithmetic-architecture-selection subsystem lives in its own module
# (issue #2532, the first split of this one at 4166 lines -- the same shape
# as `extract_parasitics.py`/`lvs_netgen.py`). `run_synthesize` reaches it
# through exactly four of these names (`_EngineOptions`,
# `_resolve_arithmetic`/`_run_arithmetic_selection`, plus
# `_candidate_measurement` for the winner's real, non-trial measurement);
# the rest are re-exported (`X as X`) because the test suite imports them
# from `klayout_tools.synthesize` by name -- including
# `ADDER_ARCHITECTURES`, which moved there with its only callers.
# `synthesize_arithmetic.py` imports this module's own helpers back at call
# time, never at load time, so this import direction is the only one.
from .synthesize_abc_stages import AbcTimeout, SynthesizeError
from .synthesize_abc_timing import (
    AbcStatsCapture,
    parse_abc_regions,
    plan_abc_stats_capture,
    read_abc_stats_timing,
)
from .synthesize_arithmetic import ADDER_ARCHITECTURES as ADDER_ARCHITECTURES
from .synthesize_arithmetic import (
    _candidate_measurement,
    _EngineOptions,
    _resolve_arithmetic,
    _run_arithmetic_selection,
)
from .synthesize_arithmetic import _measure_candidate as _measure_candidate
from .synthesize_arithmetic import _parse_yosys_param_int as _parse_yosys_param_int
from .synthesize_arithmetic import _prepare_adder_candidate as _prepare_adder_candidate
from .synthesize_arithmetic import _probe_add_widths as _probe_add_widths
from .synthesize_arithmetic import (
    _select_arithmetic_candidate as _select_arithmetic_candidate,
)
from .synthesize_arithmetic import _verify_generated_adder as _verify_generated_adder
from .synthesize_constrained_timing import (
    _resolve_clocks as _resolve_clocks,
)
from .synthesize_constrained_timing import (
    _validate_clock_entry as _validate_clock_entry,
)
from .synthesize_constrained_timing import (
    constrained_timing_fields,
    mapped_sta_constraints,
    mapped_sta_macros,
)
from .synthesize_driving_cell import (
    ABC_DONT_USE_GLOBS as _ABC_DONT_USE_GLOBS,
)
from .synthesize_driving_cell import (
    DONT_USE_PATTERN_RE as _DONT_USE_PATTERN_RE,
)
from .synthesize_driving_cell import (
    first_matching_glob,
    resolve_dont_use_globs,
)
from .synthesize_driving_cell import (
    liberty_cell_names as _liberty_cell_names,
)
from .synthesize_library_hint import (
    library_capability_warnings as _library_capability_warnings,
)

# Issue #2745: same one-directional split as `synthesize_arithmetic.py` --
# `synthesize_memory.py` imports this module's helpers at call time only.
from .synthesize_memory import collect_memory_evidence
from .synthesize_netlist_normalize import rewrite_netlist

# Re-exported from `synthesize_script` (issue #7010's split): every name
# below stays importable from this module for its existing callers -- the
# lazy `from .synthesize import ...` sites in `synthesize_abc_stages`/
# `synthesize_memory` and the test suite's `synthesize.<name>` references.
from .synthesize_script import (
    _COMMIT_RUNNABLE_HEADER,
    _COMMIT_SCRIPT_HEADER,
    _RUN_ABSOLUTE_HEADER,
    _RUN_SCRIPT_HEADER,
    PDK_ROOT_TOKEN,
    _script_liberty_text,
    _write_script_lines,
    rehydrate_script_text,
    run_script_path,
)
from .synthesize_script import RUN_SCRIPT_SUFFIX as RUN_SCRIPT_SUFFIX
from .synthesize_script import (
    _abc_pass_command as _abc_pass_command,
)
from .synthesize_script import (
    _script_input_text as _script_input_text,
)
from .synthesize_script import (
    _script_path_text as _script_path_text,
)
from .synthesize_x_scan import netlist_warnings

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
#: layout into every such record. See `env_provenance.repo_relative_path`.
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
#: - ``gf180mcu_fd_sc_mcu7t5v0`` (issue #2088) ->
#:   ``gf180mcu_fd_sc_mcu7t5v0__buf_4`` / ``13.43`` fF. Same platform
#:   policy and pF-to-fF conversion as 9t: ORFS's ``config.mk`` above
#:   resolves the driver with ``TRACK_OPTION=7t`` but keeps its load
#:   independent of track height. Verified against ORFS commit
#:   ``95ebc50a258390f4c7896e5f04db743f62279c2d`` (2026-09-19), including
#:   its ``flow/platforms/gf180/lib/`` 7t ``tt_025C_5v00`` liberty. That
#:   file gives ``buf_4`` input ``I`` a capacitance of 0.009315 pF
#:   (9.315 fF); this is a cross-check, not a replacement for the sourced
#:   platform output-load policy. No claim of optimal QoR is implied.
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
#: ``timing``), with an explicit capability warning rather than guessing
#: a driving cell for it.
_ABC_CONSTR_INPUTS: dict[str, tuple[str, float]] = {
    "sky130_fd_sc_hd": ("sky130_fd_sc_hd__buf_1", 5.0),
    "gf180mcu_fd_sc_mcu9t5v0": ("gf180mcu_fd_sc_mcu9t5v0__buf_4", 13.43),
    "gf180mcu_fd_sc_mcu7t5v0": ("gf180mcu_fd_sc_mcu7t5v0__buf_4", 13.43),
    "sg13g2_stdcell": ("sg13g2_buf_4", 6.0),
}

#: How a request's own ``constraints.dont_use`` list combines with the
#: built-in per-library :data:`_ABC_DONT_USE_GLOBS` table (issue #2382).
#:
#: **Why the default is additive rather than replacement.** The two lists
#: exist for different reasons. The built-in table is *library*-scoped and
#: corner-independent -- "keep non-logic cells (power-domain isolation, test
#: probe, scan/clock-gate) out of a mapped netlist" -- so it is correct for
#: every run against that library and a request adding an exclusion of its
#: own almost never means "and stop excluding the probe cells". A request's
#: list, by contrast, is caller-scoped and typically *corner*-dependent: a
#: cell family whose per-transition-polarity delay floor at a slow corner
#: eats a double-digit percentage of the clock budget before any load is
#: entirely reasonable at the nominal corner, so no per-library constant can
#: express it, and ``constraints.clock_period_ns`` alone does not avoid it
#: (ABC optimises area against its own model subject to the target and
#: measurably still selects such a family). Suppressing the built-in table is
#: therefore always an explicit request value -- never an implicit side
#: effect of supplying a list of one's own.
#:
#: - ``"additive"`` (default) -- the built-in table's globs, then the
#:   request's, deduplicated, order preserved.
#: - ``"replace"`` -- only the request's globs; the built-in table is
#:   suppressed for this run. Requires a non-empty ``dont_use``.
#: - ``"none"`` -- no ``-dont_use`` flag at all, not even the built-in
#:   table's (the pre-#807 mapping). Rejects a non-empty ``dont_use``, so
#:   "exclude nothing" can never be spelled the same way as "exclude only
#:   these".
#:
_DONT_USE_MODES: tuple[str, ...] = ("additive", "replace", "none")

#: The :data:`_DONT_USE_MODES` value used when a request supplies
#: ``constraints.dont_use`` without an explicit ``constraints.dont_use_mode``
#: (and, trivially, when it supplies neither).
_DEFAULT_DONT_USE_MODE = "additive"

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
#: - ``gf180mcu_fd_sc_mcu7t5v0`` (issue #2088) -> ``__tieh`` port ``Z``
#:   and ``__tiel`` port ``ZN``. ORFS's same platform config resolves
#:   these with ``TRACK_OPTION=7t``; its pinned 7t liberty cited above
#:   confirms both cells and their respective ``"1"``/``"0"`` functions.
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
#: - ``asap7sc7p5t_rvt`` and ``asap7sc7p5t_28_R`` (issue #4273) -> two
#:   distinct cells, ``TIEHIx1_ASAP7_75t_R`` port ``H`` and
#:   ``TIELOx1_ASAP7_75t_R`` port ``L``. Both keys name the same RVT
#:   library: the first is its directory under a structurally discovered
#:   lambdapdk tree (``libs/asap7sc7p5t_rvt``, which
#:   :func:`~klayout_tools.pdk_cells.resolve_liberty_for_cell_library`
#:   joins onto ``libs_ref``), the second is the key ``klt-pdk.json``
#:   descriptors use for its liberty mapping (``docs/cli/pdk.md``).
#:   Checked 2026-10-27 against lambdapdk 0.2.17: in
#:   ``asap7sc7p5t_RVT_TT_merged.lib`` ``TIEHIx1_ASAP7_75t_R``'s only
#:   output is ``H`` (``function "1"``) and ``TIELOx1_ASAP7_75t_R``'s is
#:   ``L`` (``function "0"``); both are ``CLASS CORE`` in
#:   ``asap7sc7p5t_28_R.lef``. Same two-distinct-cells shape as gf180mcu.
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
#: :func:`_library_capability_warnings` discloses that both ``setundef``
#: and ``hilomap`` were skipped, so bare constants can remain unroutable.
_TIE_CELLS: dict[str, tuple[tuple[str, str], tuple[str, str]]] = {
    "sky130_fd_sc_hd": (
        ("sky130_fd_sc_hd__conb_1", "HI"),
        ("sky130_fd_sc_hd__conb_1", "LO"),
    ),
    "gf180mcu_fd_sc_mcu9t5v0": (
        ("gf180mcu_fd_sc_mcu9t5v0__tieh", "Z"),
        ("gf180mcu_fd_sc_mcu9t5v0__tiel", "ZN"),
    ),
    "gf180mcu_fd_sc_mcu7t5v0": (
        ("gf180mcu_fd_sc_mcu7t5v0__tieh", "Z"),
        ("gf180mcu_fd_sc_mcu7t5v0__tiel", "ZN"),
    ),
    "sg13g2_stdcell": (
        ("sg13g2_tiehi", "L_HI"),
        ("sg13g2_tielo", "L_LO"),
    ),
    "asap7sc7p5t_rvt": (
        ("TIEHIx1_ASAP7_75t_R", "H"),
        ("TIELOx1_ASAP7_75t_R", "L"),
    ),
    "asap7sc7p5t_28_R": (
        ("TIEHIx1_ASAP7_75t_R", "H"),
        ("TIELOx1_ASAP7_75t_R", "L"),
    ),
}

#: Matches a Verilog ``signed`` qualifier immediately following a
#: declaration keyword (``input``/``output``/``inout``/``wire``/``reg``/
#: ``logic``) -- ``output signed [15:0] sample;``, ``wire signed [7:0]
#: mid;``, ``input wire signed [3:0] a;`` -- issue #1973's Failure 1.
#:
#: Yosys's own ``write_verilog`` (:func:`_write_script`'s final line)
#: preserves every port/wire's ``signed`` attribute verbatim and has no flag
#: to suppress it, but OpenSTA's Verilog reader (used by ``klt
#: place-and-route`` at the floorplan stage) rejects the keyword outright
#: with a bare syntax error. :func:`_strip_signed_qualifiers` runs a
#: post-write text rewrite -- the cleanest option Yosys's own pass set
#: offers (there is no ``nosigned``-style ``write_verilog`` flag, and no
#: separate pass clears a wire's ``is_signed`` attribute) -- so the emitted
#: netlist never carries the keyword at all.
#:
#: Anchored to a declaration keyword (never a bare ``\bsigned\b``) so a
#: ``$signed(...)`` cast used in an expression -- fully supported by every
#: downstream reader, and RTL's own recommended replacement for a ``signed``
#: port/wire (see ``docs/guides/digital-review/rtl-style-guide.md``) -- is
#: never touched: `$signed(` is never immediately preceded by one of these
#: keywords plus whitespace.
_SIGNED_DECL_RE = re.compile(r"\b(input|output|inout|wire|reg|logic)(\s+)signed(\s+)")


def _strip_signed_qualifiers(netlist_text: str) -> str:
    """Rewrite every ``<decl-keyword> signed`` in ``netlist_text`` to just
    ``<decl-keyword>``, dropping the ``signed`` qualifier and the single run
    of whitespace that followed it while keeping the whitespace that
    preceded it -- ``"output signed [15:0] sample;"`` becomes ``"output
    [15:0] sample;"`` (issue #1973's Failure 1; see :data:`_SIGNED_DECL_RE`).

    A pure text transform (no dependency on the writing Yosys version) so it
    is safe to run over anything :func:`_run_yosys` produced -- and cheap
    enough to always run, matching :func:`run_synthesize`'s existing
    unconditional netlist-normalization posture (``-noattr`` itself is
    unconditional).
    """
    return _SIGNED_DECL_RE.sub(r"\1\2", netlist_text)


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

#: ``cell_leakage_power`` is the scalar attribute Liberty defines for a
#: cell's state-independent leakage figure -- what issue #1626 asks this
#: module to sum. Not every cell library populates it: some (verified live,
#: ``gf180mcu_fd_sc_mcu9t5v0``) report leakage only via per-input-state
#: ``leakage_power () { when : "..."; value : "..."; }`` groups instead, with
#: no way to reduce those to one number without assuming a state probability
#: this command has no vector data to supply -- exactly the same "no
#: activity factor available" gap the issue explicitly carves switching
#: power out for. :func:`_parse_liberty_leakage_nw` therefore reads *only*
#: this scalar attribute and leaves a cell with no such entry absent from its
#: result, rather than guessing from the conditional groups.

#: The value of a library's ``leakage_power_unit`` attribute --
#: ``"1nW"`` (sky130_fd_sc_hd) / ``1uW`` (gf180mcu_fd_sc_mcu9t5v0, unquoted) --
#: the multiplier a bare ``cell_leakage_power`` number in this liberty is
#: expressed in. Verified live against both installed liberty families
#: (issue #1626).
_LEAKAGE_POWER_UNIT_RE = re.compile(
    r"^(?P<magnitude>[-+0-9.eE]+)\s*(?P<unit>[a-zA-Z]+)$"
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


_rpath = RequestPaths(SynthesizeError)


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


#: Top-level request keys ``klt synthesize`` reads (issue #3466). ``schema``
#: is an inert, unvalidated tag user-authored requests commonly carry.
_ALLOWED_REQUEST_KEYS = frozenset(
    {
        "schema",
        "engine",
        "sources",
        "hdl_toplevel",
        "pdk",
        "constraints",
        "structural",
        "baseline",
        "arithmetic",
        "latch_map",
        "run_id",
        "outputs",
        "parameters",
        "sv_frontend",
        "defines",
        "yosys_timeout_s",
        "synthesis",
        "macros",
    }
)

#: Keys allowed inside the optional request ``outputs`` object (issue #3795).
_ALLOWED_OUTPUTS_KEYS = frozenset({"dir"})


def load_request(request_path: str) -> dict[str, Any]:
    """Read and minimally validate a ``klt synthesize`` request JSON file.

    Raises :class:`SynthesizeError` if the file is missing/unreadable, not
    valid JSON, or missing a required top-level field (``sources``,
    ``hdl_toplevel``, ``pdk``). Does not require a ``schema`` field,
    matching ``klt lvs``/``klt sim``'s ``load_request`` (user-authored
    input, never emitted by this tool).
    """
    request = _load_request_json(request_path, SynthesizeError)
    request = validate_request_shape(
        request,
        "request file",
        error_cls=SynthesizeError,
        required_fields=("sources", "hdl_toplevel", "pdk"),
    )
    # Issue #3466: an unrecognised top-level key used to be dropped in
    # silence, so a misspelt or unsupported field produced a plausible
    # default-configuration result. Reject it instead (`_comment` exempt).
    _reject_unknown_keys(
        request, _ALLOWED_REQUEST_KEYS, request_path, "", SynthesizeError
    )
    _validate_outputs(request, request_path)
    return request


def _validate_outputs(request: dict[str, Any], request_path: str) -> None:
    """Reject a malformed ``outputs`` object before any engine work (issue #3795)."""
    if "outputs" not in request:
        return
    outputs = request["outputs"]
    if not isinstance(outputs, dict):
        raise SynthesizeError("request.outputs must be an object")
    _reject_unknown_keys(
        outputs, _ALLOWED_OUTPUTS_KEYS, request_path, "outputs", SynthesizeError
    )
    if "dir" in outputs:
        out_dir = outputs["dir"]
        if not isinstance(out_dir, str) or not out_dir.strip():
            raise SynthesizeError("request.outputs.dir must be a non-empty string")
        if "run_id" in request:
            raise SynthesizeError(
                "request.outputs.dir and request.run_id both name the run "
                "directory; give only one"
            )


@ytimeout.scoped_yosys_timeout
def run_synthesize(
    request_path: str,
    *,
    pdk_variant: str | None = None,
    pdk_root: str | None = None,
    verify_equivalence: bool = False,
    equiv_timeout_s: float | None = None,
    equiv_engine: str | None = None,
    yosys_timeout_s: str | float | None = None,
    restructure_timing: bool = False,
    restructure_max_iterations: int | None = None,
    _scratch_run_dir: bool = False,
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
    (the default ``yosys`` engine is combinational-only -- see
    ``docs/cli/equiv.md``'s "Scope" section; a design containing flip-flops/
    latches/memories needs ``equiv_engine="yosys-sequential"``) -- is a hard
    :class:`SynthesizeError`, never a silent warning: a synthesized netlist
    this gate cannot prove faithful to its own source RTL is not "done".
    ``equiv_timeout_s`` (the CLI's ``--equiv-timeout-s``) overrides ``klt
    equiv``'s own default proof timeout; ``None`` leaves
    :data:`klayout_tools.equiv.DEFAULT_TIMEOUT_S` in effect. It also bounds
    the ``klt equiv`` check ``restructure_timing`` runs, below.
    ``equiv_engine`` (the CLI's ``--equiv-engine``) selects the ``klt equiv``
    proof engine for the ``verify_equivalence`` gate only (``None`` keeps the
    combinational ``yosys`` default; ``"yosys-sequential"`` handles
    flip-flops); the ``restructure_timing`` check stays combinational.

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
    ``yosys_timeout_s`` (``--yosys-timeout-s``, #5773): see ``bind_yosys_timeout``.

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
    file and captured ``<top>_abc.log`` are written to
    ``.klt/synthesize/<run_id>/``
    next to the request file (the same "next to the input" default ``klt
    sim``'s ``.klt/sim/`` artifacts directory already uses) and kept as
    debuggable artifacts, never deleted. ``run_id`` is returned in the response;
    an optional request ``run_id`` exclusively reserves that directory and
    collisions are errors. Declared inputs are checked for mutation before
    returning; transitive Verilog includes are not snapshotted.

    ``_scratch_run_dir`` is private to :func:`rerun_synthesize_report`: it
    ignores the request's ``outputs.dir`` (still validated) and uses a fresh
    ``.klt/synthesize/run-<uuid>/`` directory, so a re-run never collides
    with the populated stable directory the committed report came from.
    """
    request_state = _input_file_state(request_path)
    request = load_request(request_path)
    mapping = synthesize_abc.resolve_mapping(request, request_path, SynthesizeError)
    request_dir = os.path.dirname(os.path.abspath(request_path))
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
    if re.search(r"[/\\\x00]", hdl_toplevel):
        raise SynthesizeError("request.hdl_toplevel must not contain path separators")
    parameters = validate_parameters(
        request.get("parameters"), SynthesizeError, "request.parameters"
    )
    defines = validate_defines(
        request.get("defines"), SynthesizeError, "request.defines"
    )

    sv_frontend = validate_sv_frontend(request.get("sv_frontend"), SynthesizeError)
    budget_s = ytimeout.bind_yosys_timeout(request, SynthesizeError, yosys_timeout_s)

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
    sta_constraints = mapped_sta_constraints(constraints)
    # Issue #2382: request shape is validated here -- before any PDK
    # resolution or run-directory creation -- so a malformed exclusion list
    # fails as cheaply as a malformed `clock_period_ns` does. The
    # liberty-dependent half (zero-match rejection) necessarily waits for the
    # resolved liberty, just below.
    dont_use_requested, dont_use_mode = _resolve_dont_use_request(constraints)
    expected_latches = _resolve_expected_latches(request)
    latch_map_request = _resolve_latch_map(request)
    # Issue #2635: the shared hard-macro declaration. Shape-validated here,
    # with everything else that needs no PDK resolution; the one
    # corner-dependent half waits for `_resolve_liberty` below.
    macros = _validate_macros(request.get("macros"), request_dir)
    sta_macros = mapped_sta_macros(sta_constraints, macros)

    liberty_path, corner, pdk_info = _resolve_liberty(
        cell_library, requested_corner, variant=pdk_variant, root=pdk_root
    )
    dont_use_validated = _validate_dont_use_against_liberty(
        dont_use_requested, liberty_path
    )
    library_defaults = _ABC_DONT_USE_GLOBS.get(cell_library, ())
    resolved_globs = resolve_dont_use_globs(
        library_defaults, dont_use_requested, dont_use_mode
    )
    abc_constr_request = _resolve_abc_constr_request(
        constraints, liberty_path, resolved_globs=resolved_globs
    )

    # Issue #2635: a declared macro's LEF/liberty/blackbox/GDS are *declared
    # inputs* to this run exactly as the RTL sources and the standard-cell
    # liberty are, so they get the same "was this mutated under us?" check
    # `_assert_inputs_unchanged` applies to everything else. The *generated*
    # blackbox (when a macro supplies neither a `lib` nor a
    # `verilog_blackbox`) is an output of this run, not an input, and is
    # deliberately absent.
    macro_input_paths = [
        path
        for macro in macros
        for path in (
            macro["lef"],
            macro["gds"],
            macro["verilog_blackbox"],
            *(macro["lib"] or {}).values(),
        )
        if path is not None
    ]
    input_state = {
        path: _input_file_state(path)
        for path in [*resolved_sources, liberty_path, *macro_input_paths]
    }
    input_state[request_path] = request_state
    # Hash before any engine invocation. The final state check also covers
    # arithmetic trials, equivalence, restructuring, and baseline analysis.
    provenance = build_provenance(
        deck_name=f"{cell_library}__{corner}",
        deck_path=liberty_path,
        pdk=pdk_info,
        input_path=resolved_sources[0] if len(resolved_sources) == 1 else None,
        # Issue #2027: what `klt synthesize` pins is its HDL *source*, not a
        # layout stream or a netlist -- see `docs/json-contract.md`'s
        # `provenance.input.role`.
        input_role=INPUT_ROLE_SOURCE,
    )
    if len(resolved_sources) > 1:
        provenance["input"] = {
            "content_hash": _combined_content_hash(resolved_sources),
            "role": INPUT_ROLE_SOURCE,
        }
    # Issue #3466: record which elaboration this is, so two parameterisations
    # of the same sources are distinguishable in provenance.
    provenance["parameters"] = dict(parameters)
    # Issue #4196: likewise for preprocessor defines (a separate field, so the
    # input content hash of existing committed reports is unchanged).
    provenance["defines"] = dict(defines)
    provenance.update(ytimeout.timeout_fields(budget_s))
    output_dir = _create_run_directory(
        request_dir, request, ignore_outputs_dir=_scratch_run_dir
    )

    # Issue #4198: opt-in sv2v pre-conversion. Converted once; every pass
    # that reads sources below gets the converted path, while the provenance
    # hashes above stay over the user's original sources.
    elab_sources = resolved_sources
    if sv_frontend == "sv2v":
        converted_path, sv2v_version_text = convert_sources(
            resolved_sources,
            os.path.join(output_dir, f"{hdl_toplevel}_sv2v.v"),
            SynthesizeError,
            defines,
        )
        elab_sources = [converted_path]
        provenance["sv_frontend"] = {
            "route": "sv2v",
            "sv2v_version": sv2v_version_text,
        }

    script_path = os.path.join(output_dir, f"synth_{hdl_toplevel}.ys")
    netlist_path = os.path.join(output_dir, f"{hdl_toplevel}_synth.v")
    stats_path = os.path.join(output_dir, f"{hdl_toplevel}_stats.json")

    # Issue #4662: a request-supplied `constraints.abc_constr` wins over the
    # built-in per-library entry and is the only way a library without one
    # gets `-constr` (sizing/buffering/`stime`).
    constr_inputs = abc_constr_request or _ABC_CONSTR_INPUTS.get(cell_library)
    constr_source = (
        "request"
        if abc_constr_request is not None
        else "builtin"
        if constr_inputs is not None
        else None
    )
    constr_path: str | None = None
    abc_log_path: str | None = None
    if constr_inputs is not None:
        constr_path = os.path.join(output_dir, f"{hdl_toplevel}_abc.constr")
        abc_log_path = os.path.join(output_dir, f"{hdl_toplevel}_abc.log")
        _write_constr(constr_path, *constr_inputs)

    # Issue #2635: resolved after the run directory exists, because a macro
    # that supplies neither a `lib` nor a `verilog_blackbox` gets one
    # generated from its own LEF *into* that directory.
    macro_inputs = _macro_blackbox_inputs(macros, corner=corner, output_dir=output_dir)

    dont_use_globs, cell_exclusions = _resolve_cell_exclusions(
        requested=dont_use_requested,
        mode=dont_use_mode,
        validated=dont_use_validated,
        library_defaults=library_defaults,
        resolved=resolved_globs,
    )

    abc = mapping or synthesize_abc.DEFAULT_MAPPING
    # Issue #7010: computed once here so the same answer feeds the engine
    # options, the statistics-dump capture plan, and the executed script.
    abc_fast = abc.fast_mode(_abc_supports_option)
    engine_options = _EngineOptions(
        abc_fast=abc_fast,
        abc_script_path=abc.stage_script(output_dir, SynthesizeError),
        abc_timeout_s=abc.timeout_s,
        liberty_path=liberty_path,
        cell_library=cell_library,
        delay_target_ps=delay_target_ps,
        dont_use_globs=dont_use_globs,
        tie_cells=_TIE_CELLS.get(cell_library),
        constr_inputs=constr_inputs,
    )

    latch_map_report, latch_techmap_path = _prepare_latch_map(
        latch_map_request, liberty_path, output_dir, hdl_toplevel
    )

    arithmetic = None
    adder_sources: tuple[str, ...] = ()
    adder_techmap_path: str | None = None
    arithmetic_config = _resolve_arithmetic(request)
    if arithmetic_config is not None:
        arithmetic, adder_sources, adder_techmap_path = _run_arithmetic_selection(
            config=arithmetic_config,
            output_dir=output_dir,
            resolved_sources=elab_sources,
            hdl_toplevel=hdl_toplevel,
            engine_options=engine_options,
            equiv_timeout_s=equiv_timeout_s,
            repo_root=repo_root,
            parameters=parameters,
            defines=defines,
        )

    # Issue #7010: on engines that lose ABC's redirected stdout (the WASI
    # builds), stage the engine's own constrained script with `stime -pd`
    # so its statistics dump lands in the run directory and is recoverable.
    stats_capture = plan_abc_stats_capture(
        output_dir,
        constrained=constr_inputs is not None,
        user_script=engine_options.abc_script_path,
        abc_fast=abc_fast,
        delay_target_ps=delay_target_ps,
    )

    # Publish a repo-relative, PDK-tokenized script and run its rehydrated copy.
    scripts = _write_script(
        abc_timeout_s=engine_options.abc_timeout_s,
        abc_fast=engine_options.abc_fast,
        abc_script_path=engine_options.abc_script_path,
        script_path=script_path,
        sources=elab_sources,
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
        latch_techmap_path=latch_techmap_path,
        macro_inputs=tuple(macro_inputs),
        repo_root=repo_root,
        pdk_root=pdk_info["root"],
        parameters=parameters,
        defines=defines,
        stats_capture=stats_capture,
    )

    try:
        # Issue #7010: with the statistics-dump capture staged, Yosys runs
        # from this run's own directory (the run sibling carries absolute
        # paths for exactly this) so ABC's `stats.txt` cannot land anywhere
        # shared. Without it, the pre-#7010 cwd rule stands: `repo_root`
        # whenever relative-form paths were embedded.
        executed_script_path, yosys_log = abc_stages.run_mapping(
            scripts,
            cwd=output_dir if stats_capture is not None else repo_root,
        )
    except AbcTimeout as exc:
        _assert_inputs_unchanged(input_state)
        return exc.report(
            hdl_toplevel,
            repo_root,
            provenance,
            mapping,
            arithmetic=arithmetic,
            verify_equivalence=verify_equivalence,
            restructure_timing=restructure_timing,
        )

    # Issues #1973, #4872, #5594: drop `signed` qualifiers and host source dirs
    # (absolute and repo-relative); rewritten only if changed (mtime kept).
    netlist_text = _read_produced_netlist(netlist_path)
    stripped = _strip_signed_qualifiers(netlist_text)
    rewrite_netlist(
        netlist_path, netlist_text, stripped, elab_sources, repo_root=repo_root
    )

    module_stats = _read_stats(
        stats_path, hdl_toplevel, abc_script_path=engine_options.abc_script_path
    )
    timing = _read_produced_abc_timing(
        abc_log_path,
        delay_target_ps,
        stats_path=stats_capture.stats_path if stats_capture is not None else None,
    )
    engine_version = _yosys_version()
    structural = _compute_structural(module_stats, yosys_log, expected_latches)
    instance_counts_by_type = dict(
        sorted((module_stats.get("num_cells_by_type") or {}).items())
    )
    leakage_power_nw, leakage_by_type_nw = _compute_leakage(
        liberty_path, instance_counts_by_type
    )
    # Issue #2635: macro instances reported *separately* from standard
    # cells. `instance_counts_by_type` deliberately keeps counting every
    # type (removing the macro from it would be a breaking change, and
    # "how many cells of every kind" is still the right answer for it);
    # these two fields are the additive split a caller needs to compare
    # standard-cell counts against a standard-cell budget.
    macro_cells = frozenset(macro["cell"] for macro in macros)
    macro_instance_counts_by_type = {
        cell_type: count
        for cell_type, count in instance_counts_by_type.items()
        if cell_type in macro_cells
    }
    macro_instance_count = sum(macro_instance_counts_by_type.values())
    cells_without_area = _cells_without_area(liberty_path, instance_counts_by_type)
    # Computed after the instance counts (issue #2635) so the missing-area
    # disclosure can be one more entry in the same `warnings` block every
    # other disclosed-but-skipped capability already reports through.
    warnings_summary = _summarize_warnings(
        yosys_log,
        capability_warnings={
            **_library_capability_warnings(
                cell_library,
                constr_inputs is not None,
                tie_cells=_TIE_CELLS,
                constr_inputs=_ABC_CONSTR_INPUTS,
            ),
            **_missing_timing_warning(abc_log_path, timing, stats_capture),
            **netlist_warnings(
                _read_produced_netlist(netlist_path),
                cell_library=cell_library,
                has_tie_cells=cell_library in _TIE_CELLS,
            ),
            **_dont_use_unsupported_warning(cell_exclusions),
            **_macro_area_warning(
                cells_without_area,
                macro_cells,
                macros_with_lib=frozenset(
                    macro_input["cell"]
                    for macro_input in macro_inputs
                    if macro_input["source"] == "liberty"
                ),
            ),
        },
    )
    # Issue #2745: memory-mapping evidence from a *separate* Yosys
    # invocation run only after the production run above succeeded -- the
    # production script, netlist and stats are never touched, and this call
    # never raises (an evidence failure is reported as `indeterminate`).
    memory_mapping, blackbox_modules = collect_memory_evidence(
        output_dir=output_dir,
        resolved_sources=elab_sources,
        hdl_toplevel=hdl_toplevel,
        final_stats_path=stats_path,
        adder_sources=adder_sources,
        adder_techmap_path=adder_techmap_path,
        repo_root=repo_root,
        parameters=parameters,
        liberty_path=liberty_path,
        defines=defines,
        macro_inputs=tuple(macro_inputs),
    )

    equivalence = None
    if verify_equivalence:
        equivalence = _verify_synthesis_equivalence(
            synthesize_output_dir=output_dir,
            resolved_sources=elab_sources,
            hdl_toplevel=hdl_toplevel,
            netlist_path=netlist_path,
            liberty_path=liberty_path,
            timeout_s=equiv_timeout_s,
            engine=equiv_engine,
            parameters=parameters,
            defines=defines,
        )

    sta, sta_unavailable_reason = _read_sta_timing(
        netlist_path, liberty_path, hdl_toplevel
    )

    restructuring = None
    if restructure_timing:
        restructuring = _run_timing_restructuring(
            netlist_path=netlist_path,
            liberty_path=liberty_path,
            hdl_toplevel=hdl_toplevel,
            delay_target_ps=delay_target_ps,
            sta=sta,
            output_dir=output_dir,
            resolved_sources=elab_sources,
            max_iterations=restructure_max_iterations,
            equiv_timeout_s=equiv_timeout_s,
            repo_root=repo_root,
            parameters=parameters,
            defines=defines,
        )

    response: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "engine": engine,
        "engine_version": engine_version,
        "hdl_toplevel": hdl_toplevel,
        # Issue #3466: the top-level parameter overrides this run elaborated
        # with (`{}` when none). Additive field, no `schema_version` bump
        # (docs/json-contract.md).
        "parameters": dict(parameters),
        # Issue #4196: preprocessor defines (`read_verilog -D`), `{}` when none.
        "defines": dict(defines),
        "run_id": os.path.basename(output_dir),
        "status": "ok",
        "instance_count": module_stats["num_cells"],
        "area_um2": module_stats["area"],
        # ``sequential_area`` is only emitted by newer Yosys builds (0.67+);
        # distro-packaged Yosys (e.g. Ubuntu 24.04's 0.33) omits it from
        # `stat -json` entirely. Degrade to `None` rather than a raw
        # `KeyError` -- see #560.
        "sequential_area_um2": module_stats.get("sequential_area"),
        "instance_counts_by_type": instance_counts_by_type,
        # Issue #2635: the shared hard-macro block. `macros` echoes the
        # declaration resolved to absolute paths (identical shape to `klt
        # sta`'s own echo -- `klayout_tools.macros.macros_response`);
        # `macro_instance_counts_by_type`/`macro_instance_count` split the
        # macro instances back out of `instance_counts_by_type` (which still
        # counts every type, unchanged); `cells_without_area` names every
        # instantiated type whose area `area_um2` does *not* account for --
        # `null` when that cannot be established from the resolved liberty.
        # Always present; additive fields, no `schema_version` bump
        # (docs/json-contract.md).
        # `blackbox_source`/`blackbox_path` additionally record *how* each
        # macro was made visible to Yosys (`"liberty"` /
        # `"verilog_blackbox"` / `"lef"`, the last being a stub this run
        # generated into the run directory) -- see
        # `_macro_blackbox_inputs`.
        "macros": [
            {
                **entry,
                "blackbox_source": macro_input["source"],
                "blackbox_path": env_provenance.repo_relative_path(
                    macro_input["path"], repo_root=repo_root
                ),
            }
            for entry, macro_input in zip(
                macro_spec.macros_response(
                    macros,
                    # Every path in a `klt synthesize` response is the
                    # `{path, scope}` shape (issue #1844, the reason this
                    # verb's `schema_version` is 2) -- a committed report is
                    # evidence, and an absolute path in one leaks the
                    # author's home directory. `macros[]`'s paths are no
                    # exception; `find_absolute_path_fields` enforces it.
                    normalize_path=lambda path: env_provenance.repo_relative_path(
                        path, repo_root=repo_root
                    ),
                ),
                macro_inputs,
                strict=True,
            )
        ],
        "macro_instance_counts_by_type": macro_instance_counts_by_type,
        "macro_instance_count": macro_instance_count,
        "cells_without_area": cells_without_area,
        # Static (leakage) power only -- issue #1626. `None` when the
        # resolved liberty reports no `cell_leakage_power` for any
        # instantiated cell type at all -- see `_compute_leakage`.
        "leakage_power_nw": leakage_power_nw,
        "leakage_by_type_nw": leakage_by_type_nw,
        "timing": timing,
        # Issue #2883: which clock constraints the request carried and that
        # `timing.delay_target_ps` is ONE global ABC target (never per-clock).
        # Additive field, no `schema_version` bump (docs/json-contract.md).
        "clock_targets": _describe_clock_targets(constraints, delay_target_ps),
        "sta": sta,
        **constrained_timing_fields(
            sta_constraints,
            netlist_path=netlist_path,
            hdl_toplevel=hdl_toplevel,
            cell_library=cell_library,
            corner=corner,
            sta_macros=sta_macros,
            pdk_variant=pdk_variant,
            pdk_root=pdk_root,
        ),
        # Issue #2628: *why* `sta` is `null`, so a caller can tell "STA
        # unavailable here -- install the optional extension and retry" from
        # "STA not applicable to this netlist/liberty pair". `null` exactly
        # when `sta` is non-`null`. Free-text, matching the
        # `unavailable_reason` precedent in
        # `place_and_route_power_audit.py`. Additive field, no
        # `schema_version` bump (docs/json-contract.md).
        "sta_unavailable_reason": sta_unavailable_reason,
        "structural": structural,
        # Issue #2382: the fully-resolved standard-cell exclusion list this
        # run actually handed ABC, plus the two inputs it was merged from and
        # the mode that merged them -- so a committed report records what was
        # excluded rather than leaving it implicit in the tool version.
        # Always present (like `structural`/`warnings`); additive field, no
        # `schema_version` bump (docs/json-contract.md).
        "cell_exclusions": cell_exclusions,
        # Issue #4662: where the `abc -constr` inputs came from -- `request`
        # (`constraints.abc_constr`), `builtin` (`_ABC_CONSTR_INPUTS`), or
        # `null` when no `-constr` was passed. Additive field, no
        # `schema_version` bump.
        "abc_constr": {
            "source": constr_source,
            "driving_cell": constr_inputs[0] if constr_inputs else None,
            "load_ff": constr_inputs[1] if constr_inputs else None,
        },
        "warnings": warnings_summary,
        # Issue #2745: whether inferred memories were lowered to logic,
        # optimized away before lowering, or survived mapping -- per module
        # definition, bounded to what Yosys's own `stat` counts establish
        # (`indeterminate` otherwise). `blackbox_modules` lists modules
        # declared `blackbox`/`whitebox` that still have instances after
        # mapping; their area is not in `area_um2`. Both additive, no
        # `schema_version` bump (docs/json-contract.md).
        "memory_mapping": memory_mapping,
        "blackbox_modules": blackbox_modules,
        "netlist_path": env_provenance.repo_relative_path(
            netlist_path, repo_root=repo_root
        ),
        # Issues #2452/#7277: the exact-file digest and its banner-normalised
        # companion, both from the internal absolute path after final
        # processing — see `_digest.netlist_digest_fields`.
        **netlist_digest_fields(netlist_path),
        "script_path": env_provenance.repo_relative_path(
            script_path, repo_root=repo_root
        ),
        # Issue #1870: the script Yosys was actually handed. Equal to
        # `script_path` whenever no `$PDK_ROOT` token was written (a liberty
        # outside the resolved install root), and the rehydrated
        # `synth_<top>.run.ys` sibling otherwise -- so "what ran" is always
        # nameable, and never has to be inferred from a filename convention.
        # Additive field, no `schema_version` bump (docs/json-contract.md).
        "run_script_path": env_provenance.repo_relative_path(
            executed_script_path, repo_root=repo_root
        ),
        "provenance": provenance,
        **ytimeout.timeout_fields(budget_s),
        "equivalence": equivalence,
        "restructuring": restructuring,
        "arithmetic": arithmetic,
        "baseline": None,
    }
    synthesize_abc.record_mapping(mapping, response, provenance)
    _attach_latch_map_report(
        response, latch_map_report, latch_techmap_path, repo_root=repo_root
    )
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
    _assert_inputs_unchanged(input_state)
    return response


def _create_run_directory(
    request_dir: str, request: dict[str, Any], *, ignore_outputs_dir: bool = False
) -> str:
    """Exclusively claim a retained directory; an existing ID is never reused.

    ``outputs.dir`` (issue #3795) names the run directory itself, relative to
    the request file's directory; it may be absent or an existing *empty*
    directory, never a populated one. ``ignore_outputs_dir`` (``--rerun``)
    skips that claim and allocates a fresh ``run-<uuid>`` directory instead.
    """
    _validate_outputs(request, "request")
    out_dir = None if ignore_outputs_dir else request.get("outputs", {}).get("dir")
    if out_dir is not None:
        output_dir = os.path.normpath(os.path.join(request_dir, out_dir))
        try:
            os.makedirs(os.path.dirname(output_dir), exist_ok=True)
            try:
                os.mkdir(output_dir)
            except FileExistsError:
                # Only an existing empty directory may be reused.
                if not os.path.isdir(output_dir) or os.listdir(output_dir):
                    raise
        except OSError as exc:
            raise SynthesizeError(
                f"could not claim outputs.dir '{output_dir}': {exc}; it must "
                "not exist or be an empty directory (prior evidence is retained)"
            ) from exc
        return output_dir
    run_id = request.get("run_id", f"run-{uuid.uuid4().hex}")
    if not isinstance(run_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", run_id
    ):
        raise SynthesizeError(
            "request.run_id must be a safe identifier: 1-64 ASCII letters, "
            "digits, underscores or hyphens, starting with a letter or digit"
        )
    parent = os.path.join(request_dir, ".klt", "synthesize")
    output_dir = os.path.join(parent, run_id)
    try:
        os.makedirs(parent, exist_ok=True)
        os.mkdir(output_dir)
    except OSError as exc:
        raise SynthesizeError(
            f"could not create output directory '{output_dir}': {exc}; "
            "each invocation needs a new run_id (prior evidence is retained)"
        ) from exc
    return output_dir


def _input_file_state(path: str) -> tuple[int, ...]:
    """Track declared inputs without copying RTL trees or PDK installations.

    ctime also catches write-then-restore and replacement with equal bytes;
    atime is deliberately excluded since reading the file may update it.
    This is an execution-time mutation guard, not a transitive-include snapshot.
    """
    try:
        stat = os.stat(path)
    except OSError as exc:
        raise SynthesizeError(
            f"could not read synthesis input '{path}': {exc}"
        ) from exc
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _assert_inputs_unchanged(states: dict[str, tuple[int, ...]]) -> None:
    for path, before in states.items():
        try:
            unchanged = _input_file_state(path) == before
        except SynthesizeError:
            unchanged = False
        if not unchanged:
            raise SynthesizeError(
                f"synthesis input '{path}' changed during execution; "
                "refusing to attribute outputs to an inconsistent input state"
            )


def _read_produced_netlist(path: str) -> str:
    if not os.path.isfile(path):
        raise SynthesizeError(f"yosys exited successfully but did not produce '{path}'")
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise SynthesizeError(f"could not read netlist '{path}': {exc}") from exc
    if not text.strip():
        raise SynthesizeError(f"yosys produced an empty netlist '{path}'")
    return text


def _read_produced_abc_timing(
    path: str | None,
    delay_target_ps: int | None,
    stats_path: str | None = None,
) -> dict[str, Any] | None:
    if path is not None and not os.path.isfile(path):
        raise SynthesizeError(f"yosys did not produce the expected ABC log '{path}'")
    # Issue #7010: the captured log stays authoritative (full precision,
    # per-region). Only when it yields nothing -- the WASI engines lose
    # ABC's redirected stdout entirely -- does the run's own statistics
    # dump get a chance, and only in its unambiguous single-record form.
    timing = _read_abc_timing(path, delay_target_ps)
    if timing is not None or stats_path is None:
        return timing
    return read_abc_stats_timing(path, stats_path, delay_target_ps)


def _missing_timing_warning(
    abc_log_path: str | None,
    timing: dict[str, Any] | None,
    stats_capture: AbcStatsCapture | None = None,
) -> dict[str, str]:
    if abc_log_path is None or timing is not None:
        return {}
    if stats_capture is not None:
        return {
            "timing_unavailable": (
                "The current invocation's ABC log has no readable stime summary "
                "and its statistics dump cannot attribute a delay to this run; "
                "timing is null and any requested clock target remains unverified."
            )
        }
    return {
        "timing_unavailable": (
            "The current invocation's ABC log has no readable stime summary; "
            "timing is null and any requested clock target remains unverified."
        )
    }


def _verify_synthesis_equivalence(
    *,
    synthesize_output_dir: str,
    resolved_sources: list[str],
    hdl_toplevel: str,
    netlist_path: str,
    liberty_path: str,
    timeout_s: float | None,
    engine: str | None = None,
    request_filename: str | None = None,
    parameters: dict[str, int | str] | None = None,
    defines: dict[str, str | None] | None = None,
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
    directory as ``.klt/synthesize/<run_id>/.klt/equiv/``, right alongside this run's
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
    equiv_request: dict[str, Any] = {
        "gold": {"sources": resolved_sources, "top": hdl_toplevel},
        "gate": {
            "sources": [netlist_path],
            "top": hdl_toplevel,
            "liberty": liberty_path,
        },
    }
    if engine is not None:
        equiv_request["engine"] = engine
    if parameters:
        # Issue #3466: the gold RTL must be elaborated like the synthesis was.
        equiv_request["gold"]["parameters"] = dict(parameters)
    if defines:
        # Issue #4196: ... and preprocessed with the same defines.
        equiv_request["gold"]["defines"] = dict(defines)
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

    return require_equivalence(
        equiv_report,
        error_cls=SynthesizeError,
        counterexample_prefix=(
            "synthesized netlist is NOT equivalent to its source RTL"
        ),
        no_verdict_prefix="equivalence check against source RTL",
    )


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
    parameters: dict[str, int | str] | None = None,
    defines: dict[str, str | None] | None = None,
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
            parameters=parameters,
            defines=defines,
        )

    # Issue #1844: `restructured_netlist_path` is `restructure_for_timing`'s
    # own real absolute path, useful for every internal caller above -- only
    # the value handed back to `run_synthesize`'s response is normalized,
    # and only when it is not already `None` (no resize applied).
    if report["restructured_netlist_path"] is not None:
        report["restructured_netlist_path"] = env_provenance.repo_relative_path(
            report["restructured_netlist_path"], repo_root=repo_root
        )

    return report


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

    return resolve_readable_files(
        sources,
        request_dir,
        SynthesizeError,
        field="request.sources entry",
        noun="RTL source",
    )


def _validate_macros(macros: Any, request_dir: str) -> list[dict[str, Any]]:
    """Validate the optional ``request.macros`` array (issue #2635) -- the
    shared, placement-free hard-macro declaration documented once in
    :mod:`klayout_tools.macros` and accepted identically by ``klt sta``.

    Returns ``[]`` when the field is omitted, so a request written before
    this field existed synthesizes byte-for-byte as it did. A thin wrapper:
    the only per-verb differences are the error class and, here alone, the
    LEF-less relaxation (issue #5612).
    """
    return macro_spec.validate_macros(
        macros, request_dir, error_cls=SynthesizeError, allow_missing_lef=True
    )


def _macro_blackbox_inputs(
    macros: list[dict[str, Any]], *, corner: str, output_dir: str
) -> list[dict[str, str]]:
    """How each declared macro is made visible to Yosys, in request order::

        [{"cell": str, "source": "liberty"|"verilog_blackbox"|"lef",
          "path": str}, ...]

    Exactly one of three dispositions per macro, in precedence order:

    1. ``lib`` -- the corner-matching macro liberty
       (:func:`klayout_tools.macros.macro_lib_for_corner`), loaded with
       ``read_liberty -lib``. Preferred because it carries the macro's real
       timing *and* area, so a later ``klt sta``/``klt place-and-route`` run
       on the same declaration sees the same numbers.
    2. ``verilog_blackbox`` -- a caller-supplied ``(* blackbox *)`` module
       declaration, loaded with ``read_verilog -lib``.
    3. Neither -- a blackbox is **generated** from the macro's own LEF pin
       list (:func:`klayout_tools.macros.blackbox_verilog_text`) into
       ``<run dir>/<cell>_blackbox.v`` and loaded the same way. This is the
       path that means a caller with only a LEF never has to hand-write (and
       hand-maintain) a stub -- unreachable for a LEF-less macro (issue
       #5612), which has no LEF to generate from and always takes 1 or 2.

    Every disposition has the same effect on the netlist: the macro
    survives ``hierarchy``/``synth``/``abc`` as one opaque instance rather
    than being implemented out of standard cells. Live-verified against
    Yosys 0.69 for dispositions 1 and 2 (see
    :func:`~klayout_tools.macros.blackbox_verilog_text`); 3 produces the
    same file shape as 2 by construction.

    The generated file is written into the run directory (never next to the
    LEF, which may be a read-only PDK install) and retained as a debuggable
    artifact alongside the ``.ys`` script, matching every other generated
    input this module keeps.
    """
    inputs: list[dict[str, str]] = []
    for macro in macros:
        lib_path = macro_spec.macro_lib_for_corner(
            macro, corner, error_cls=SynthesizeError
        )
        if lib_path is not None:
            inputs.append(
                {"cell": macro["cell"], "source": "liberty", "path": lib_path}
            )
            continue
        blackbox_path = macro["verilog_blackbox"]
        if blackbox_path is not None:
            inputs.append(
                {
                    "cell": macro["cell"],
                    "source": "verilog_blackbox",
                    "path": blackbox_path,
                }
            )
            continue
        generated = os.path.join(output_dir, f"{macro['cell']}_blackbox.v")
        _write_text_file(
            generated,
            macro_spec.blackbox_verilog_text(macro, error_cls=SynthesizeError),
        )
        inputs.append({"cell": macro["cell"], "source": "lef", "path": generated})
    return inputs


def _cells_without_area(
    liberty_path: str, instance_counts_by_type: dict[str, int]
) -> list[str] | None:
    """The response's ``cells_without_area`` field (issue #2635): every cell
    type this run instantiated for which the resolved standard-cell liberty
    reports no ``area``, sorted.

    This is what makes ``area_um2`` non-silent. Yosys's ``stat -liberty``
    sums only the areas the *one* liberty it was handed declares, so a
    blackboxed hard macro contributes exactly ``0`` with nothing in the
    response marking it -- a caller comparing ``area_um2`` against an area
    budget is then wrong by the macro's whole footprint, and has no way to
    find out from the response. A non-empty list here says precisely which
    instantiated types are unaccounted for.

    ``[]`` -- a real, not-fabricated empty answer -- when the design
    instantiates nothing at all, or when every instantiated type does have
    an area. ``None`` -- "not establishable", never a fabricated empty list
    -- when the liberty cannot be read or parsed, or
    has no ``cell (...)`` group at all (a liberty whose cells all
    lack ``area`` is *not* that case: its cells are parseable, so every
    instantiated type is reported missing), exactly the posture
    :func:`_liberty_cell_names` takes for the same two cases.
    """
    if not instance_counts_by_type:
        return []
    try:
        with open(liberty_path, encoding="utf-8") as handle:
            liberty_text = handle.read()
        library = parse_liberty_text(liberty_text, source=liberty_path)
    except (OSError, UnicodeDecodeError, LibertyCompareError):
        return None
    cells = [cell for cell in library.children("cell") if cell.args]
    if not cells:
        return None
    areas: set[str] = set()
    for cell in cells:
        try:
            float(cell.attributes.get("area", ""))
        except (TypeError, ValueError):
            continue
        areas.add(cell.args[0])
    return sorted(
        cell_type for cell_type in instance_counts_by_type if cell_type not in areas
    )


def _macro_area_warning(
    cells_without_area: list[str] | None,
    macro_cells: frozenset[str],
    macros_with_lib: frozenset[str] | None = None,
) -> dict[str, str]:
    """A ``warnings`` entry (issue #2635) whenever ``area_um2`` demonstrably
    does not account for every instantiated cell type.

    A response *field* a caller must remember to read is not enough on its
    own -- the whole failure this closes is a naive reader trusting
    ``area_um2``. Surfacing it through the same ``warnings`` block every
    other disclosed-but-skipped capability uses means it shows up in any
    harness that already summarises warnings, with no new field to learn.
    Separate categories for "a declared hard macro" and "some other
    unmapped type" because the dispositions differ: the first is expected
    and the caller must add the macro's own area themselves, the second is
    usually a ``-dont_use``/mapping problem.

    ``macros_with_lib`` (issue #4368) names the macros that have a macro
    liberty (``response.macros[].lib`` non-null); a LEF-only or
    blackbox-only macro has no area there, so the text must not send the
    caller to it. ``None`` treats every macro as having one.
    """
    if not cells_without_area:
        return {}
    macros_missing = [cell for cell in cells_without_area if cell in macro_cells]
    others_missing = [cell for cell in cells_without_area if cell not in macro_cells]
    warnings: dict[str, str] = {}
    if macros_missing:
        with_lib = [
            cell
            for cell in macros_missing
            if macros_with_lib is None or cell in macros_with_lib
        ]
        without_lib = [cell for cell in macros_missing if cell not in with_lib]
        text = (
            "area_um2 excludes the hard macro(s) "
            + ", ".join(macros_missing)
            + ": the standard-cell liberty stat -liberty was handed has no "
            "area for them."
        )
        if with_lib:
            text += (
                f" Add the area of {', '.join(with_lib)}"
                " from its macro liberty (response.macros[].lib) before "
                "comparing area_um2 to an area budget."
            )
        if without_lib:
            text += (
                f" {', '.join(without_lib)}"
                " has no macro liberty (response.macros[].lib is null: "
                "LEF-only, verilog_blackbox-only or abstract-less), so no "
                "area is available for it here."
            )
        warnings["macro_area_excluded"] = text
    if others_missing:
        warnings["cells_without_liberty_area"] = (
            "area_um2 excludes instantiated cell type(s) with no liberty "
            "area: " + ", ".join(others_missing)
        )
    return warnings


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

    Thin wrapper around :func:`klayout_tools.pdk_cells.resolve_liberty_for_cell_library`
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
    """The integer picosecond ABC delay target (``abc -D``) derived from
    ``constraints``, or ``None`` when the request does not supply one.

    Precedence (issue #2883): an explicit ``constraints.clock_period_ns`` is
    the global target; otherwise, when ``constraints.clocks`` is given, the
    *tightest* (smallest) listed ``period_ns`` -- ABC takes a single global
    ``-D``, so the conservative choice is the one that cannot under-target
    any listed domain. See :func:`_describe_clock_targets` for the envelope
    echo of which one was used.

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
    clocks = _resolve_clocks(constraints)
    clock_period_ns = constraints.get("clock_period_ns")
    if clock_period_ns is None:
        if clocks:
            return int(round(min(c["period_ns"] for c in clocks) * 1000))
        return None
    if not is_number(clock_period_ns):
        raise SynthesizeError(
            "request.constraints.clock_period_ns must be a positive number "
            "(nanoseconds) or null"
        )
    if clock_period_ns <= 0:
        raise SynthesizeError(
            "request.constraints.clock_period_ns must be greater than zero"
        )
    return int(round(clock_period_ns * 1000))


def _describe_clock_targets(
    constraints: dict[str, Any] | None, delay_target_ps: int | None
) -> dict[str, Any]:
    """The response's ``clock_targets`` object (issue #2883): which clock
    constraints the request carried and how the single ABC delay target was
    derived from them.

    ``abc -D`` is one global number, so per-clock periods are *never*
    individually enforced; ``per_clock_enforced`` is always ``false`` and
    ``delay_target_basis`` names where ``delay_target_ps`` came from:
    ``"none"``, ``"clock_period_ns"`` or ``"min_clocks_period_ns"``.
    """
    clocks = _resolve_clocks(constraints)
    explicit = (constraints or {}).get("clock_period_ns") is not None
    if explicit:
        basis = "clock_period_ns"
    elif clocks:
        basis = "min_clocks_period_ns"
    else:
        basis = "none"
    return {
        "clocks": clocks,
        "delay_target_ps": delay_target_ps,
        "delay_target_basis": basis,
        "per_clock_enforced": False,
    }


def _resolve_abc_constr_request(
    constraints: dict[str, Any] | None,
    liberty_path: str,
    *,
    resolved_globs: tuple[str, ...],
) -> tuple[str, float] | None:
    """The validated ``constraints.abc_constr`` as ``(driving_cell, load_ff)``,
    or ``None`` when the request does not supply one (issue #4662).

    Supplies the ``abc -constr`` inputs for any ``cell_library`` and overrides
    the built-in :data:`_ABC_CONSTR_INPUTS` entry when both exist. Any shape
    problem, an unknown key, a ``driving_cell`` absent from the resolved
    liberty (skipped only when the liberty's cell list cannot be established,
    as for ``dont_use``), an excluded driving cell, or a non-positive/non-finite
    ``load_ff`` is a :class:`SynthesizeError` naming the field.
    """
    if not constraints or constraints.get("abc_constr") is None:
        return None
    raw, where = constraints["abc_constr"], "request.constraints.abc_constr"
    if not isinstance(raw, dict):
        raise SynthesizeError(f"{where} must be a JSON object")
    unknown = sorted(set(raw) - {"driving_cell", "load_ff"})
    if unknown:
        raise SynthesizeError(
            f"{where} has unknown key(s) {unknown}; allowed: driving_cell, load_ff"
        )
    for key in ("driving_cell", "load_ff"):
        if key not in raw:
            raise SynthesizeError(f"{where}.{key} is required")
    driving_cell = raw["driving_cell"]
    if not isinstance(driving_cell, str) or not driving_cell:
        raise SynthesizeError(f"{where}.driving_cell must be a non-empty string")
    load_ff = raw["load_ff"]
    if not is_finite_number(load_ff) or load_ff <= 0:
        raise SynthesizeError(
            f"{where}.load_ff must be a finite number greater than zero "
            f"(femtofarads), got {load_ff!r}"
        )
    cell_names = _liberty_cell_names(liberty_path)
    if cell_names is not None and driving_cell not in cell_names:
        raise SynthesizeError(
            f"{where}.driving_cell '{driving_cell}' is not a cell in the "
            f"resolved liberty '{os.path.basename(liberty_path)}' "
            f"({len(cell_names)} cells)"
        )
    pattern = first_matching_glob(driving_cell, resolved_globs)
    if pattern is not None:
        raise SynthesizeError(
            f"{where}.driving_cell '{driving_cell}' matches dont_use "
            f"pattern '{pattern}'. Choose another driving cell or change "
            "constraints.dont_use or constraints.dont_use_mode."
        )
    return driving_cell, float(load_ff)


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
    return _abc_supports_option("-dont_use")


def _abc_supports_option(option: str) -> bool:
    """Probe the installed ABC pass, including downstream-patched Yosys builds."""
    try:
        completed = run_in_group(
            ["yosys", "-p", "help abc"],
            capture_output=True,
            text=True,
            timeout=DEFAULT_YOSYS_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if completed.returncode != 0:
        return False
    return any(
        line.split()[:1] == [option] for line in (completed.stdout or "").splitlines()
    )


def _resolve_dont_use_mode(constraints: dict[str, Any]) -> str:
    """The validated ``constraints.dont_use_mode``, defaulting to
    :data:`_DEFAULT_DONT_USE_MODE` when absent (issue #2382).

    Split out of :func:`_resolve_dont_use_request` purely to keep that
    function under the repo's cyclomatic-complexity ratchet
    (``scripts/check_complexity_baseline.py``); it carries no behaviour of
    its own beyond the mode half of that function's shape validation.
    """
    mode = constraints.get("dont_use_mode")
    if mode is None:
        return _DEFAULT_DONT_USE_MODE
    if not isinstance(mode, str) or mode not in _DONT_USE_MODES:
        raise SynthesizeError(
            "request.constraints.dont_use_mode must be one of "
            + ", ".join(f"'{value}'" for value in _DONT_USE_MODES)
            + f" (got {mode!r})"
        )
    return mode


def _resolve_dont_use_patterns(constraints: dict[str, Any]) -> tuple[str, ...]:
    """The validated, de-duplicated ``constraints.dont_use`` pattern list
    (issue #2382), empty when the field is absent.

    The list half of :func:`_resolve_dont_use_request`'s shape validation,
    split out for the same complexity-ratchet reason as
    :func:`_resolve_dont_use_mode`. Order is preserved across the duplicate
    collapse, matching :func:`_resolve_arithmetic`.
    """
    raw = constraints.get("dont_use")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise SynthesizeError(
            "request.constraints.dont_use must be an array of cell-name/"
            "glob strings (a bare string is not accepted)"
        )
    collected: list[str] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry:
            raise SynthesizeError(
                "request.constraints.dont_use entries must be non-empty "
                f"strings (got {entry!r})"
            )
        if _DONT_USE_PATTERN_RE.match(entry) is None:
            raise SynthesizeError(
                f"request.constraints.dont_use entry '{entry}' contains a "
                "character outside a cell name or ABC glob pattern "
                "(allowed: letters, digits, '_', '.', '-', '!', '*', '?', "
                "'[', ']')"
            )
        if entry not in collected:
            collected.append(entry)
    return tuple(collected)


def _resolve_dont_use_request(
    constraints: dict[str, Any] | None,
) -> tuple[tuple[str, ...], str]:
    """``(patterns, mode)`` from ``constraints.dont_use`` and
    ``constraints.dont_use_mode`` -- the request-level standard-cell exclusion
    list (issue #2382), validated for *shape* only.

    Pure request validation: nothing here reads the liberty or probes the
    engine, so it is safe to call before any PDK resolution or run-directory
    creation. The liberty-dependent half (does each pattern actually match a
    cell?) is :func:`_validate_dont_use_against_liberty`; the merge with the
    built-in per-library table is :func:`resolve_dont_use_globs`.

    Raises :class:`SynthesizeError` naming the field for a non-array value, a
    non-string/empty entry, an entry containing a character that would corrupt
    the generated ``.ys`` script's ``abc`` line
    (:data:`_DONT_USE_PATTERN_RE`), an unknown ``dont_use_mode``, or a
    mode/list combination that contradicts itself -- the same "a request that
    means to constrain the run but expresses it wrongly must not be silently
    downgraded" posture :func:`_resolve_delay_target_ps` already takes.
    Duplicate patterns are collapsed (order preserved), matching
    :func:`_resolve_arithmetic`'s own treatment of a duplicated candidate.

    The per-field shape checks live in :func:`_resolve_dont_use_mode` and
    :func:`_resolve_dont_use_patterns`; what remains here is the
    cross-field consistency pair (a mode that contradicts the list).
    """
    if not constraints:
        return (), _DEFAULT_DONT_USE_MODE

    mode = _resolve_dont_use_mode(constraints)
    patterns = _resolve_dont_use_patterns(constraints)

    if mode == "none" and patterns:
        raise SynthesizeError(
            "request.constraints.dont_use_mode 'none' excludes nothing, so it "
            "cannot be combined with a non-empty request.constraints.dont_use "
            "list (use 'replace' to exclude only the listed patterns)"
        )
    if mode == "replace" and not patterns:
        raise SynthesizeError(
            "request.constraints.dont_use_mode 'replace' requires a non-empty "
            "request.constraints.dont_use list (use 'none' to drop the "
            "built-in per-library exclusions without adding any)"
        )
    return patterns, mode


def _validate_dont_use_against_liberty(
    patterns: tuple[str, ...], liberty_path: str
) -> bool | None:
    """Reject a ``constraints.dont_use`` pattern matching **zero** cells in
    the resolved liberty (issue #2382).

    A silent typo in an exclusion list is invisible: the run succeeds, the
    netlist differs from the one the author meant to produce, and nothing in
    the report says so. So a pattern that matches nothing is a
    :class:`SynthesizeError` naming the offending pattern, never a no-op.

    Returns whether validation was actually performed: ``True`` when every
    pattern matched at least one cell, ``None`` when there was nothing to
    check (no request-level patterns) or the liberty's cell list could not be
    established (:func:`_liberty_cell_names` returning ``None``), which the
    response discloses as ``cell_exclusions.validated`` rather than passing
    off an unchecked list as a checked one.

    Only *request-supplied* patterns are checked. The built-in
    :data:`_ABC_DONT_USE_GLOBS` entries deliberately are not: each was already
    cross-checked against its library's real installed liberty when it was
    added (see that table's docstring), and they are library-scoped by
    construction, so re-deriving them per run would only add a way for a
    partial liberty read to fail a run that is correct.

    Matching is :func:`fnmatch.fnmatchcase` -- case-sensitive glob, the same
    ``*``/``?``/``[...]`` shape ``abc -dont_use`` documents ("supports simple
    glob patterns in the cell name"). A pattern with no metacharacter
    therefore degrades to an exact cell-name match, exactly as ABC's own
    handling does.
    """
    if not patterns:
        return None
    cell_names = _liberty_cell_names(liberty_path)
    if cell_names is None:
        return None
    for pattern in patterns:
        if not any(fnmatch.fnmatchcase(name, pattern) for name in cell_names):
            raise SynthesizeError(
                f"request.constraints.dont_use pattern '{pattern}' matches no "
                f"cell in the resolved liberty "
                f"'{os.path.basename(liberty_path)}' ({len(cell_names)} cells) "
                "-- an exclusion that matches nothing silently changes nothing"
            )
    return True


def _resolve_cell_exclusions(
    *,
    requested: tuple[str, ...],
    mode: str,
    validated: bool | None,
    library_defaults: tuple[str, ...],
    resolved: tuple[str, ...],
) -> tuple[tuple[str, ...], dict[str, Any]]:
    """``(dont_use_globs, cell_exclusions)`` -- the ``-dont_use`` glob list
    actually passed to ABC, plus the response block disclosing how it was
    arrived at (issue #2382).

    The resolved patterns have already been checked against the request's
    driving cell. Probe the engine only if there is something to exclude.
    If Yosys lacks ``abc -dont_use``, report an empty ``effective`` list.
    A dropped request pattern also triggers :func:`_dont_use_unsupported_warning`.
    """
    supported: bool | None = None
    if resolved:
        supported = _abc_supports_dont_use()
    effective = resolved if supported else ()

    return effective, {
        "mode": mode,
        "requested": list(requested),
        "library_defaults": list(library_defaults),
        "effective": list(effective),
        "validated": validated,
        "engine_supports_dont_use": supported,
    }


def _dont_use_unsupported_warning(cell_exclusions: dict[str, Any]) -> dict[str, str]:
    """A capability warning when the request asked for a cell exclusion the
    resolved Yosys build's ``abc`` pass cannot express (issue #2382).

    Emitted **only** for a request-supplied list. The built-in
    :data:`_ABC_DONT_USE_GLOBS` table's own degradation on such a build is
    pre-existing, documented behaviour (issue #807) and stays silent; a
    caller's explicit request silently not taking effect is the new failure
    mode worth a warning, since the netlist still contains exactly the cells
    the request meant to keep out of it.
    """
    if cell_exclusions.get("engine_supports_dont_use") is not False:
        return {}
    if not cell_exclusions.get("requested"):
        return {}
    return {
        "dont_use_unsupported": (
            "The resolved Yosys build's abc pass does not support -dont_use, so "
            "request.constraints.dont_use was not applied: "
            + ", ".join(cell_exclusions["requested"])
            + ". The netlist may still contain those cells."
        )
    }


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
    latch_techmap_path: str | None = None,
    macro_inputs: tuple[dict[str, str], ...] = (),
    repo_root: str | None = None,
    pdk_root: str | None = None,
    parameters: dict[str, int | str] | None = None,
    defines: dict[str, str | None] | None = None,
    abc_fast: synthesize_abc.FastMode = None,
    abc_script_path: str | None = None,
    abc_timeout_s: float | None = None,
    stats_capture: AbcStatsCapture | None = None,
) -> str | abc_stages.StagedMapping:
    """Generate the ``.ys`` synthesis script (Yosys survey section 1's exact
    pass sequence: ``read_verilog`` -> ``hierarchy`` -> ``synth`` ->
    ``dfflibmap`` -> ``abc -liberty`` -> ``clean`` -> ``setundef`` ->
    ``hilomap`` -> ``stat``/``write_verilog``) into ``script_path``.

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

    ``tie_cells`` (issue #854, a :data:`_TIE_CELLS` entry) adds a
    ``setundef -zero`` line (issue #1973) immediately followed by
    ``hilomap -hicell <cell> <port> -locell <cell> <port>``, mapping every
    remaining constant driver onto a real standard cell. Both lines'
    position is load-bearing: **after** ``clean`` (they can only rewrite the
    constants ABC/``clean`` left behind) and **before** ``stat``/
    ``write_verilog`` (the tie cells they insert are real instances, so they
    must be counted in ``instance_count``/``area_um2`` and present in the
    emitted netlist). ``setundef -zero`` must additionally run **before**
    ``hilomap``: without it, a bit left ``x`` by Yosys (a dangling Verilog
    ``function`` argument is the common source -- issue #1973's Failure 2)
    is not a constant `hilomap` recognises at all, so it survives as a bare
    ``x`` literal that OpenSTA's Verilog reader turns into an unroutable
    ``GROUND``-typed net exactly like an unmapped ``1'b0``/``1'b1`` would
    (``DRT-0305``). Resolving every ``x`` bit to a concrete ``0`` first
    means `hilomap`'s existing pass then maps it onto a tie cell identically
    to a literal constant -- confirmed live (Yosys 0.69, a dangling-argument
    ``function`` against a real sky130 liberty): the resulting netlist
    carries zero bare constants of any kind, `x`-valued or not.

    With none of those (a ``cell_library`` in no table), the emitted script
    is byte-identical to the pre-#807 one -- `setundef` is scoped to the
    same tie-cell-table condition as `hilomap` since, without a tie-cell
    entry to map the resolved bit onto, converting a bare `x` into a bare
    `0` swaps one already-known, separately-tracked bare-constant limitation
    (:data:`_TIE_CELLS`'s own docstring, "gets **no** ``hilomap`` pass at
    all") for another rather than fixing anything.

    ``macro_inputs`` (issue #2635, :func:`_macro_blackbox_inputs`' own
    return value) inserts one line per declared hard macro **between** the
    ``read_verilog`` of the RTL sources and ``hierarchy``::

        read_liberty -lib <macro liberty>     # source == "liberty"
        read_verilog -lib <macro blackbox>    # source == "verilog_blackbox"/"lef"

    Its position is load-bearing: ``hierarchy -check -top`` is exactly the
    pass that rejects a module instantiated but never read (``Module
    `\\<cell>' referenced in module `\\<top>' in cell `\\<inst>' is not
    part of the design`` -- the failure this field exists to close), so the
    macro's declaration must already be loaded when it runs. ``-lib`` on
    either reader is what makes the module a **blackbox**: its instance
    survives ``synth``/``abc``/``clean`` untouched into ``write_verilog``'s
    netlist and into ``stat``'s own ``num_cells_by_type``, instead of being
    implemented out of standard cells -- which is the entire point of a hard
    macro, and the thing that feeding the vendor's behavioural Verilog
    through ``sources`` gets exactly backwards. With no macros declared
    (every pre-#2635 request) not one line is emitted and the script is
    byte-identical to before.

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

    ``latch_techmap_path`` (issue #4458, the request's ``latch_map`` field)
    inserts ``techmap -map <path>`` **after** ``synth`` and **before**
    ``dfflibmap``: ``synth`` is what infers the ``$_DLATCH_P_``/
    ``$_DLATCH_N_`` primitives the rule file matches, and ``dfflibmap``/``abc``
    would otherwise leave them unmapped. ``None`` emits nothing, so every
    pre-#4458 script is byte-identical.

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

    ``stats_capture`` (issue #7010, :func:`~klayout_tools.synthesize_abc_
    timing.plan_abc_stats_capture`'s plan) adds one exception to that
    sibling rule: the caller executes Yosys from the run directory so
    ABC's ``stime -d`` statistics dump lands there, so the runnable
    sibling's paths are **absolute** wherever the commit form keeps
    repo-relative ones (a sibling is emitted for that reason alone when no
    token was written), and the ``abc`` line carries the staged capture
    script via ``-script`` -- the engine's own constrained mapping
    commands with the ``stime`` tail widened to also dump statistics.

    With an ABC budget, return the three staged scripts and checkpoint paths.
    Otherwise return the path of the script to execute (``script_path``, or its
    rehydrated sibling) -- never the artifact to commit, which is always
    ``script_path``.
    """
    liberty_text = _script_liberty_text(liberty_path, pdk_root=pdk_root)
    # Issue #7010: with the statistics-dump capture staged, the executed
    # script must be runnable from the run directory (Yosys's cwd there is
    # what makes ABC's `stats.txt` invocation-local), so the same line set
    # is emitted twice: the commit-safe form below keeps issue #1844's
    # repo-relative paths, and the run form carries absolute paths only.
    render = functools.partial(
        _write_script_lines,
        liberty_text=liberty_text,
        stats_path=stats_path,
        netlist_path=netlist_path,
        constr_path=constr_path,
        abc_log_path=abc_log_path,
        abc_script_path=abc_script_path,
        stats_capture=stats_capture,
        delay_target_ps=delay_target_ps,
        dont_use_globs=dont_use_globs,
        tie_cells=tie_cells,
        adder_sources=adder_sources,
        adder_techmap_path=adder_techmap_path,
        latch_techmap_path=latch_techmap_path,
        macro_inputs=macro_inputs,
        sources=sources,
        hdl_toplevel=hdl_toplevel,
        parameters=parameters,
        defines=defines,
        abc_fast=abc_fast,
        pdk_root=pdk_root,
    )
    frontend, tail = render(repo_root=repo_root)
    run_frontend = run_tail = None
    if stats_capture is not None and repo_root is not None:
        run_frontend, run_tail = render(repo_root=None)
    return abc_stages.write_mapping(
        script_path,
        frontend,
        tail,
        hdl_toplevel,
        abc_fast,
        abc_timeout_s,
        repo_root=repo_root,
        pdk_root=pdk_root,
        run_frontend=run_frontend,
        run_tail=run_tail,
    )


def _publish_script(
    script_path: str,
    lines: list[str],
    pdk_root: str | None,
    run_lines: list[str] | None = None,
) -> str:
    script_text = "\n".join(lines) + "\n"
    if pdk_root is None or PDK_ROOT_TOKEN not in script_text:
        # Issue #7010: without a token there is normally nothing to
        # rehydrate, so no sibling -- unless the statistics-dump capture
        # needs a runnable form whose paths are absolute (`run_lines`).
        if run_lines is None:
            _write_script_text(script_path, script_text)
            return script_path
        run_path = run_script_path(script_path)
        _write_script_text(
            script_path,
            _COMMIT_RUNNABLE_HEADER.format(run_script=os.path.basename(run_path))
            + script_text,
        )
        _write_script_text(
            run_path,
            _RUN_ABSOLUTE_HEADER.format(script=os.path.basename(script_path))
            + "\n".join(run_lines)
            + "\n",
        )
        return run_path

    run_path = run_script_path(script_path)
    run_text = "\n".join(run_lines) + "\n" if run_lines is not None else script_text
    _write_script_text(
        script_path,
        _COMMIT_SCRIPT_HEADER.format(run_script=os.path.basename(run_path))
        + script_text,
    )
    _write_script_text(
        run_path,
        _RUN_SCRIPT_HEADER.format(script=os.path.basename(script_path))
        + rehydrate_script_text(run_text, pdk_root=pdk_root),
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


DEFAULT_YOSYS_TIMEOUT_S = ytimeout.DEFAULT_YOSYS_TIMEOUT_S  # see that module


def _run_yosys(
    script_path: str,
    *,
    timeout_s: float | None = ytimeout.UNSET,
    cwd: str | None = None,
) -> str:
    """Invoke ``yosys -s <script_path>``, raising :class:`SynthesizeError`
    on any failure to run: missing binary, non-zero exit, or exceeding
    ``timeout_s`` (default :data:`DEFAULT_YOSYS_TIMEOUT_S`; ``None`` = none).
    Never raises on a *successful* (exit 0) run; the caller validates that the
    declared output files appeared. Returns the captured ``stdout`` log text --
    Yosys's own ``Warning: `` lines land on stdout, never stderr (verified live;
    matches :func:`synthesis_error_message`'s stream-preference note for
    ``ERROR:`` lines) -- so :func:`_compute_structural`/:func:`_summarize_warnings`
    (issue #1588) can parse it without a second Yosys invocation.

    A timed-out run raises :class:`SynthesizeError` exactly like any other Yosys
    failure (never a bare ``subprocess.TimeoutExpired``) -- this is what lets
    :func:`_measure_candidate` (issue #1775) disqualify one hung arithmetic-
    architecture trial synthesis without killing the rest of the ``"auto"``
    sweep. The kill takes the child's whole process group (issue #5798), so a
    ``yosys-abc`` grandchild cannot outlive the watchdog that ended its parent.

    ``cwd`` (issue #1844) is passed straight through to the spawn's own ``cwd``;
    ``script_path`` is always given as an absolute path, so ``cwd`` only matters
    for a script :func:`_write_script` wrote with a relative-form embedded path
    (``repo_root`` given), which must run with ``cwd=<that same repo_root>`` to
    resolve correctly. The default ``None`` reproduces the pre-#1844 behaviour
    exactly (subprocess's own default: the invoking process's cwd), correct for
    every script that embeds only absolute paths. The child env is a copy of
    ``os.environ`` with ``COLUMNS`` pinned to 4096, applied unconditionally (an
    inherited ``COLUMNS=80`` is exactly the failing case), so the readline-wrapped
    ``source`` echo can never wedge the abc handshake (issue #5780; see
    ``docs/cli/synthesize.md``).
    """
    timeout_s = ytimeout.resolve_timeout(timeout_s, DEFAULT_YOSYS_TIMEOUT_S)
    try:
        completed = run_in_group(
            ["yosys", "-s", script_path],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=cwd,
            env={**os.environ, "COLUMNS": "4096"},
        )
    except OSError as exc:
        _write_yosys_log(script_path, "", str(exc))
        raise SynthesizeError(f"could not launch yosys: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        log_path = _write_yosys_log(script_path, exc.stdout, exc.stderr)
        raise SynthesizeError(
            ytimeout.timeout_message(timeout_s, script_path, log_path, exc.stdout)
        ) from exc

    _write_yosys_log(script_path, completed.stdout, completed.stderr)
    if completed.returncode != 0:
        raise SynthesizeError(synthesis_error_message(completed))

    return completed.stdout or ""


def _write_yosys_log(
    script_path: str, stdout: str | bytes | None, stderr: str | bytes | None
) -> str:
    """Keep both streams next to every executed script, including failed trials."""

    def text(stream: str | bytes | None) -> str:
        if isinstance(stream, bytes):
            return stream.decode("utf-8", errors="replace")
        return stream or ""

    log_path = os.path.splitext(script_path)[0] + ".log"
    _write_text_file(
        log_path,
        f"[stdout]\n{text(stdout)}\n[stderr]\n{text(stderr)}",
    )
    return log_path


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


def _read_stats(
    stats_path: str, hdl_toplevel: str, *, abc_script_path: str | None = None
) -> dict[str, Any]:
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
    synthesize_abc.check_custom_mapping(abc_script_path, result, SynthesizeError)
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
    happened to be mapped last. Every report is also listed, in log order, as
    ``regions: [{"module": "top", "critical_path_ps": 130.4}, ...]``
    (issue #4743); ``module`` is the ``Extracting gate netlist of module``
    banner preceding that ``stime`` line, or ``null`` when the log carries
    none. The maximum over ``regions`` equals ``critical_path_ps``.
    """
    if abc_log_path is None or not os.path.isfile(abc_log_path):
        return None
    try:
        with open(abc_log_path, encoding="utf-8", errors="replace") as handle:
            log_text = handle.read()
    except OSError:
        return None

    regions = parse_abc_regions(log_text)
    if not regions:
        return None

    worst = max(regions, key=lambda r: r.delay_ps)
    critical_path_ps, wire_load = worst.delay_ps, worst.wire_load
    return {
        "source": "abc_stime",
        "wire_load": None if wire_load.lower() == "none" else wire_load,
        "critical_path_ps": critical_path_ps,
        "delay_target_ps": delay_target_ps,
        "regions": [
            {"module": r.module, "critical_path_ps": r.delay_ps} for r in regions
        ],
    }


#: The response's ``sta_unavailable_reason`` for the "extension not
#: installed" case (issue #2628) -- the one case a caller can act on
#: directly, so the reason names the install path rather than just the
#: absence. Kept as a module constant so the test suite pins the exact
#: string a consumer branches on.
STA_UNAVAILABLE_EXTENSION_NOT_INSTALLED = (
    "the optional klt_statime_native extension is not installed (it is not "
    "shipped with the published klayout-tools package), so no gate-level "
    "static timing analysis was attempted -- from a repo checkout, run "
    "`uv sync --group statime` (or `maturin develop --release` inside "
    "native/statime/) and re-run to populate `sta`; from an installed "
    "package, `klt sta` with a `verilog` request gives a netlist-level "
    "timing estimate (needs OpenSTA)"
)


def _statime_native_is_importable() -> bool:
    """Whether ``import klt_statime_native`` succeeds in this interpreter.

    Used only to *classify* an already-raised
    :class:`~klayout_tools.sta.StaError` (see :func:`_read_sta_timing`):
    ``sta.py`` collapses "extension missing" and "engine could not analyze
    this netlist" into the same exception type, and a caller acting on
    ``sta_unavailable_reason`` needs them apart -- the first is fixed by
    installing something, the second never is.

    Deliberately :func:`importlib.import_module` rather than
    :func:`importlib.util.find_spec`: the latter raises :class:`ValueError`
    for a module object carrying no ``__spec__`` (exactly what the hermetic
    tests' stub injects), and that exception would escape the ``except
    StaError`` handler in :func:`_read_sta_timing` -- a hard failure of
    ``run_synthesize`` instead of the "extension not installed" reason this
    function exists to produce. ``find_spec`` does handle the other ordinary
    hermetic-test state, a ``sys.modules`` entry of ``None`` (CPython's own
    "this import must fail" sentinel), correctly -- it returns ``None``
    rather than raising -- but so does :func:`importlib.import_module`, via
    :class:`ModuleNotFoundError` (an :class:`ImportError` subclass), so that
    state does not favor either function. The import itself is free here --
    by the time this is called the real load has already been attempted, so
    a successful import is a ``sys.modules`` cache hit.
    """
    try:
        importlib.import_module("klt_statime_native")
    except ImportError:
        return False
    return True


def _read_sta_timing(
    netlist_path: str, liberty_path: str, hdl_toplevel: str
) -> tuple[dict[str, Any] | None, str | None]:
    """Run :func:`klayout_tools.sta.compute_critical_path` over the
    just-produced ``netlist_path`` and shape its result into the response's
    ``sta`` field (issue #925, Epic #704 Phase 3) -- a real timing-graph
    walk over the whole mapped netlist (register-to-register,
    register-to-port, or port-to-port, whichever is globally worst),
    computed by ``klt_statime_native`` (``native/statime/``).

    Returns ``(sta, sta_unavailable_reason)`` -- the response's two fields,
    and exactly one of them is ever non-``None``.

    Additive and best-effort, mirroring :func:`_read_abc_timing`'s own
    "no number to report" honesty discipline: ``sta`` is ``None`` -- never a
    fabricated result -- when the ``klt_statime_native`` extension is not
    installed (a Rust toolchain is optional for every other ``klt synthesize``
    caller, so a missing extension must not turn every synthesis run into a
    hard failure) or when the native engine itself could not analyze this
    particular netlist/liberty pair (:class:`~klayout_tools.sta.StaError`).
    A genuine analysis result is never partial -- either the full
    ``worst_path``/``worst_reg_to_reg_path`` shape comes back, or ``sta`` is
    ``None``.

    Issue #2628: a bare ``sta: null`` collapsed those two cases into one
    value, so a caller could not tell "STA unavailable here, install the
    extension and retry" from "STA not applicable to this netlist/liberty
    pair". The second element keeps them apart in the response itself --
    :data:`STA_UNAVAILABLE_EXTENSION_NOT_INSTALLED` for the first, and the
    :class:`~klayout_tools.sta.StaError`'s own message (which already
    describes the failure -- e.g. a mapped cell type with no liberty entry)
    for the second. Free text, matching the ``unavailable_reason`` precedent
    in ``place_and_route_power_audit.py``, not a fixed vocabulary a consumer
    would have to exhaustively match.
    """
    if _netlist_is_hierarchical(netlist_path):
        # Issue #4224: the native engine reads only the ``top`` module's
        # text and treats every instance as a liberty cell, so a user
        # sub-module instance is a "not found in liberty" failure. Analyze a
        # throwaway flattened copy; the committed netlist stays hierarchical.
        with tempfile.TemporaryDirectory(prefix="klt_sta_flat_") as scratch:
            try:
                flat_path = _flatten_netlist_for_sta(
                    netlist_path, liberty_path, hdl_toplevel, scratch
                )
            except SynthesizeError as exc:
                if not _statime_native_is_importable():
                    return None, STA_UNAVAILABLE_EXTENSION_NOT_INSTALLED
                return None, (
                    "the netlist is hierarchical (more than one module) and "
                    f"could not be flattened for the klt_statime_native engine: "
                    f"{exc} -- `klt sta` with a `verilog` request links and "
                    "flattens the hierarchy itself"
                )
            return _read_sta_timing_flat(flat_path, liberty_path, hdl_toplevel)
    return _read_sta_timing_flat(netlist_path, liberty_path, hdl_toplevel)


def _read_sta_timing_flat(
    netlist_path: str, liberty_path: str, hdl_toplevel: str
) -> tuple[dict[str, Any] | None, str | None]:
    """The un-flattening half of :func:`_read_sta_timing`: one
    ``compute_critical_path`` call with ``StaError`` shaped into the
    ``sta_unavailable_reason`` text."""
    try:
        return compute_critical_path(netlist_path, liberty_path, hdl_toplevel), None
    except StaError as exc:
        if not _statime_native_is_importable():
            return None, STA_UNAVAILABLE_EXTENSION_NOT_INSTALLED
        return None, (
            "the klt_statime_native engine could not analyze this "
            f"netlist/liberty pair: {exc}"
        )


_NETLIST_MODULE_RE = re.compile(r"^\s*module\s+(\\\S+|[A-Za-z_][\w$]*)", re.MULTILINE)


def _netlist_is_hierarchical(netlist_path: str) -> bool:
    """Whether the netlist file defines more than one distinct module
    (issue #4224). A cheap text check; an unreadable file is not
    hierarchical, leaving the error to the engine call as before."""
    try:
        with open(netlist_path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return False
    return len(set(_NETLIST_MODULE_RE.findall(text))) > 1


def _flatten_netlist_for_sta(
    netlist_path: str, liberty_path: str, hdl_toplevel: str, scratch_dir: str
) -> str:
    """Write a flattened copy of ``netlist_path`` into ``scratch_dir`` with
    Yosys and return its path (issue #4224). ``read_liberty -lib`` runs
    first so mapped-cell instances resolve (see
    :func:`_baseline_metrics_from_netlist`). Raises :class:`SynthesizeError`
    on any Yosys failure or a missing output."""
    script_path = os.path.join(scratch_dir, f"{hdl_toplevel}_sta_flatten.ys")
    flat_path = os.path.join(scratch_dir, f"{hdl_toplevel}_sta_flat.v")
    script_lines = [
        f"read_liberty -lib {liberty_path}",
        f"read_verilog {netlist_path}",
        f"hierarchy -check -top {hdl_toplevel}",
        "flatten",
        f"write_verilog -noattr {flat_path}",
    ]
    with open(script_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(script_lines) + "\n")
    _run_yosys(script_path)
    if not os.path.isfile(flat_path):
        raise SynthesizeError("yosys did not write the flattened netlist")
    return flat_path


def _leakage_unit_scale_to_nw(unit_text: str | None) -> float:
    """Multiplier turning a ``cell_leakage_power`` number expressed in the
    library's ``leakage_power_unit`` (``unit_text``, the attribute's value --
    see :data:`_LEAKAGE_POWER_UNIT_RE`) into nanowatts. Defaults to ``1.0``
    -- liberty's own ``1nW`` default is not assumed to apply, but a library
    declaring nothing is read as nanowatts -- when ``unit_text`` is absent or
    names a unit :data:`_LEAKAGE_UNIT_TO_NW` does not recognise. Never
    raises, matching :func:`_abc_supports_dont_use`'s own "never break a run
    over an optional/best-effort probe" posture.
    """
    if unit_text is None:
        return 1.0
    match = _LEAKAGE_POWER_UNIT_RE.match(unit_text.strip())
    if match is None:
        return 1.0
    try:
        magnitude = float(match.group("magnitude"))
    except ValueError:
        return 1.0
    per_unit_nw = _LEAKAGE_UNIT_TO_NW.get(match.group("unit").lower())
    if per_unit_nw is None:
        return 1.0
    return magnitude * per_unit_nw


def _parse_liberty_leakage_nw(liberty_text: str) -> dict[str, float]:
    """Read every ``cell (name) { ... cell_leakage_power : <value>; ... }``
    scalar out of ``liberty_text``, converted to nanowatts via that
    liberty's own ``leakage_power_unit`` (:func:`_leakage_unit_scale_to_nw`).

    Extracts only the one scalar :func:`_compute_leakage` needs. A cell with
    no ``cell_leakage_power`` attribute (see the note above
    :data:`_LEAKAGE_POWER_UNIT_RE` -- some libraries, e.g.
    gf180mcu_fd_sc_mcu9t5v0, report leakage only via per-input-state groups
    instead) is simply absent from the result, never assigned a guessed
    value. Text that is not parseable liberty yields ``{}`` -- the caller
    treats that as "leakage not establishable" (``None``), never as a zero.
    """
    try:
        library = parse_liberty_text(liberty_text)
    except LibertyCompareError:
        return {}
    unit_scale = _leakage_unit_scale_to_nw(library.attributes.get("leakage_power_unit"))
    leakage_by_cell: dict[str, float] = {}
    for cell in library.children("cell"):
        raw_value = cell.attributes.get("cell_leakage_power")
        if raw_value is None or not cell.args:
            continue
        try:
            value = float(raw_value)
        except ValueError:
            continue
        # A later block for the same cell name (should not happen in a
        # well-formed liberty) overwrites, "last one wins" -- the same
        # convention as `restructure._parse_liberty_pins`.
        leakage_by_cell[cell.args[0]] = value * unit_scale
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
    see the note above :data:`_LEAKAGE_POWER_UNIT_RE`). When *some* (not all)
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


#: Latch polarities ``request.latch_map`` may map, to the Yosys internal
#: primitive each one rewrites (issue #4458). ``$_DLATCH_N_`` is transparent
#: while its enable is low, ``$_DLATCH_P_`` while high.
_LATCH_MAP_POLARITIES = {"positive": "$_DLATCH_P_", "negative": "$_DLATCH_N_"}
_LATCH_MAP_PIN_KEYS = ("enable", "data", "q")
_LATCH_MAP_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")


def _resolve_latch_map(request: dict[str, Any]) -> dict[str, dict[str, str]] | None:
    """``request.latch_map`` (issue #4458) shape-validated, or ``None`` when
    absent. ``{"positive"|"negative": {"cell", "enable", "data", "q"}}``: the
    library latch each inferred ``$_DLATCH_*_`` is mapped onto. An omitted
    polarity stays unmapped. Raises :class:`SynthesizeError` on any other
    shape (unknown key, missing pin, non-identifier name, empty object)."""
    if "latch_map" not in request:
        return None
    latch_map = request["latch_map"]
    if not isinstance(latch_map, dict) or not latch_map:
        raise SynthesizeError(
            "request.latch_map must be a non-empty object keyed by "
            f"{' and/or '.join(repr(k) for k in _LATCH_MAP_POLARITIES)}"
        )
    _reject_unknown_keys(
        latch_map,
        frozenset(_LATCH_MAP_POLARITIES),
        "request",
        "latch_map",
        SynthesizeError,
    )
    resolved: dict[str, dict[str, str]] = {}
    for polarity in _LATCH_MAP_POLARITIES:
        if polarity not in latch_map:
            continue
        entry = latch_map[polarity]
        where = f"request.latch_map.{polarity}"
        if not isinstance(entry, dict):
            raise SynthesizeError(f"{where} must be an object")
        _reject_unknown_keys(
            entry,
            frozenset(("cell", *_LATCH_MAP_PIN_KEYS)),
            "request",
            f"latch_map.{polarity}",
            SynthesizeError,
        )
        resolved_entry: dict[str, str] = {}
        for key in ("cell", *_LATCH_MAP_PIN_KEYS):
            value = entry.get(key)
            if not isinstance(value, str) or not _LATCH_MAP_NAME.fullmatch(value):
                raise SynthesizeError(
                    f"{where}.{key} must be a string naming a Verilog "
                    "identifier (letters, digits, '_', '$')"
                )
            resolved_entry[key] = value
        if len(set(resolved_entry[k] for k in _LATCH_MAP_PIN_KEYS)) != 3:
            raise SynthesizeError(f"{where} enable, data and q must be distinct pins")
        resolved[polarity] = resolved_entry
    return resolved


def _prepare_latch_map(
    latch_map: dict[str, dict[str, str]] | None,
    liberty_path: str,
    output_dir: str,
    hdl_toplevel: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate ``latch_map`` against the liberty and write its techmap file
    (issue #4458); ``(report, techmap_path)``, both ``None`` without one."""
    if latch_map is None:
        return None, None
    report = _validate_latch_map_against_liberty(latch_map, liberty_path)
    techmap_path = os.path.join(output_dir, f"{hdl_toplevel}_latch_map.v")
    _write_script_text(techmap_path, _latch_techmap_text(latch_map))
    return report, techmap_path


def _attach_latch_map_report(
    response: dict[str, Any],
    report: dict[str, Any] | None,
    techmap_path: str | None,
    *,
    repo_root: str | None,
) -> None:
    """Add the ``latch_map`` block to ``response`` when a map was requested."""
    if report is None:
        return
    report["techmap_path"] = env_provenance.repo_relative_path(
        techmap_path, repo_root=repo_root
    )
    response["latch_map"] = report


def _validate_latch_map_against_liberty(
    latch_map: dict[str, dict[str, str]], liberty_path: str
) -> dict[str, Any]:
    """Reject a ``latch_map`` cell absent from the resolved liberty (issue
    #4458) and return the response's ``latch_map`` block. When the cell list
    cannot be established the check is skipped (``validated: false``), the
    same posture as :func:`_validate_dont_use_against_liberty`."""
    cell_names = _liberty_cell_names(liberty_path)
    if cell_names is not None:
        for polarity, entry in latch_map.items():
            if entry["cell"] not in cell_names:
                raise SynthesizeError(
                    f"request.latch_map.{polarity}.cell '{entry['cell']}' is "
                    f"not a cell in the resolved liberty "
                    f"'{os.path.basename(liberty_path)}'"
                )
    return {
        "mapped": {polarity: dict(entry) for polarity, entry in latch_map.items()},
        "validated": cell_names is not None,
        "techmap_path": None,
    }


def _latch_techmap_text(latch_map: dict[str, dict[str, str]]) -> str:
    """The Yosys ``techmap`` rule file for ``latch_map`` (issue #4458): one
    module per mapped polarity, instantiating the library latch in place of
    the ``$_DLATCH_*_`` primitive (ports ``E``/``D``/``Q``)."""
    chunks = ["// Generated by klt synthesize from request.latch_map (issue #4458)\n"]
    for polarity, primitive in _LATCH_MAP_POLARITIES.items():
        entry = latch_map.get(polarity)
        if entry is None:
            continue
        chunks.append(
            f"module \\{primitive} (input E, input D, output Q);\n"
            f"  {entry['cell']} _TECHMAP_REPLACE_ "
            f"(.{entry['enable']}(E), .{entry['data']}(D), .{entry['q']}(Q));\n"
            "endmodule\n"
        )
    return "\n".join(chunks)


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
    if not is_int(expected) or expected < 0:
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


def _summarize_warnings(
    log_text: str, *, capability_warnings: dict[str, str] | None = None
) -> dict[str, Any]:
    """Build the response's ``warnings`` field (issue #1588): a bounded,
    deterministic summary of every ``Warning: `` line in ``log_text`` --
    never the raw log itself, plus one warning per supplied capability category.

    ``total`` counts capability warnings plus raw engine warning lines
    (deliberately **not** deduplicated the
    way :func:`_compute_structural`'s own ``comb_loops``/``multi_driven``
    counts are -- ``synth``'s internal ``check`` calls can reprint an
    unresolved problem's identical text more than once, so this answers
    "how noisy was this run", not "how many distinct problems").
    ``by_category``/``representatives`` are sorted by category name for
    determinism; ``representatives`` is capped at
    :data:`_MAX_WARNING_REPRESENTATIVES` entries, one per category, each the
    first message text seen for that category (bounded, per issue #1588).
    """
    first_seen = dict(capability_warnings or {})
    total = len(first_seen)
    by_category = dict.fromkeys(first_seen, 1)
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
        resolved = _rpath(response_path, request_dir, "request.baseline.response_path")
        baseline_metrics = _baseline_metrics_from_response_file(resolved)
        ref = ref or _baseline_ref_fallback(resolved, repo_root=repo_root)
    else:
        assert netlist_path is not None
        resolved = _rpath(netlist_path, request_dir, "request.baseline.netlist_path")
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


# --------------------------------------------------------------------------- #
# --check / --rerun: verify a previously committed report (issue #2224)
# --------------------------------------------------------------------------- #

#: Run-scoped bookkeeping keys dropped from *both* sides of a ``--rerun``
#: diff (issue #2224), via :func:`klayout_tools._report_verify.strip_keys`.
#:
#: Every one of these is a name (or a path derived from a name) this module
#: mints fresh per invocation -- ``run_id`` defaults to a ``uuid4`` and every
#: artifact path below is written *inside* ``.klt/synthesize/<run_id>/`` -- so
#: two runs of the identical request against identical sources always
#: disagree on them. None of them is verdict-bearing: what the run *found*
#: lives in ``instance_count``/``area_um2``/``timing``/``sta``/``structural``/
#: ``warnings``/``equivalence.status``/``arithmetic.selected_architecture``,
#: all of which stay in the diff. A consumer that wants the fresh artifact
#: paths reads them off the embedded ``fresh`` report, which is never
#: canonicalized.
RERUN_BOOKKEEPING_KEYS: frozenset[str] = frozenset(
    {
        "run_id",
        "netlist_path",
        "stats_path",
        "script_path",
        "run_script_path",
        "restructured_netlist_path",
        "log_path",
        "stage2_script_path",
        "stage2_log_path",
        # A stub generated from LEF (#2635) lives under the run directory.
        # Its `macros[].blackbox_path` carries the run ID and would
        # otherwise make every rerun diff report a spurious difference.
        # `macros[].lef`/`.lib`/`.gds`/`.verilog_blackbox` are caller-
        # supplied inputs, stable across reruns, and deliberately stay in
        # the diff -- a committed report whose macro declaration changed
        # *should* fail the rerun check.
        "blackbox_path",
        *ytimeout.RERUN_BOOKKEEPING_KEYS,  # host-level budget (#5773), not minted
    }
)


def _canonicalize_synthesize_report_for_rerun_diff(
    report: dict[str, Any],
) -> dict[str, Any]:
    """Drop run bookkeeping and proof durations from the replay comparison."""
    view = strip_keys(report, RERUN_BOOKKEEPING_KEYS)
    for path in (("equivalence",), ("restructuring", "equivalence")):
        proof = get_path(view, path)
        if isinstance(proof, dict):
            proof.pop("elapsed_s", None)
    return view


def _synthesize_provenance_hashes(
    request_path: str,
    *,
    pdk_variant: str | None = None,
    pdk_root: str | None = None,
    skip_deck: bool = False,
) -> tuple[str | None, str | None]:
    """Re-derive ``(input_content_hash, deck_content_hash)`` for the request
    at ``request_path`` **without invoking Yosys** -- the cheap half of
    :func:`check_synthesize_report`.

    Deliberately mirrors :func:`run_synthesize`'s own hashing, step for step:
    the sources are resolved through the same :func:`_resolve_sources`, a
    single source hashes through :func:`klayout_tools._provenance.content_hash`
    (what :func:`build_provenance`'s ``input_path`` does) and multiple sources
    through :func:`klayout_tools._provenance._combined_content_hash` (what
    :func:`run_synthesize` substitutes for the multi-source case), and the
    liberty resolves through the same :func:`_resolve_liberty`. Any divergence
    here would show up as permanent, unexplained drift on a report that never
    moved, so there is exactly one rule and both call sites follow it.

    With ``skip_deck`` (issue #4315) the liberty is never resolved and the
    deck hash is returned as ``None`` -- the PDK-less path.

    Raises :class:`SynthesizeError` for a request that cannot be read,
    validated, or resolved -- never a traceback.
    """
    request = load_request(request_path)
    request_dir = os.path.dirname(os.path.abspath(request_path))
    resolved_sources = _resolve_sources(request["sources"], request_dir)

    pdk_spec = request["pdk"]
    if not isinstance(pdk_spec, dict):
        raise SynthesizeError("request.pdk must be a JSON object")
    cell_library = pdk_spec.get("cell_library")
    if not isinstance(cell_library, str) or not cell_library:
        raise SynthesizeError("request.pdk.cell_library is required")
    if len(resolved_sources) == 1:
        input_hash = content_hash(resolved_sources[0])
    else:
        input_hash = _combined_content_hash(resolved_sources)
    if skip_deck:
        return input_hash, None
    liberty_path, _corner, _pdk_info = _resolve_liberty(
        cell_library,
        pdk_spec.get("corner"),
        variant=pdk_variant,
        root=pdk_root,
    )
    return input_hash, content_hash(liberty_path)


def _synthesize_request_parameters(request_path: str) -> dict[str, int | str]:
    """The validated ``parameters`` override of the request at
    ``request_path`` (``{}`` when absent) -- what ``--check`` compares against
    the committed report's recorded ``parameters`` (issue #3466)."""
    return validate_parameters(
        load_request(request_path).get("parameters"),
        SynthesizeError,
        "request.parameters",
    )


def _synthesize_request_defines(request_path: str) -> dict[str, str | None]:
    """The validated ``defines`` of the request at ``request_path`` (``{}``
    when absent) -- what ``--check`` compares against the committed report's
    recorded ``defines`` (issue #4196)."""
    return validate_defines(
        load_request(request_path).get("defines"),
        SynthesizeError,
        "request.defines",
    )


def _defines_check(recorded: Any, current: dict[str, str | None]) -> dict[str, Any]:
    """Cheap-mode comparison entry for ``defines`` (issue #4196); an absent
    recorded ``defines`` counts as ``{}``."""
    expected = {} if recorded is None else recorded
    return {
        "field": "defines",
        "expected": expected,
        "actual": current,
        "match": expected == current,
    }


def _parameters_check(recorded: Any, current: dict[str, int | str]) -> dict[str, Any]:
    """Cheap-mode comparison entry for the ``parameters`` override (issue
    #3466). A report that predates the field recorded no override, so an
    absent ``parameters`` counts as ``{}``."""
    expected = {} if recorded is None else recorded
    return {
        "field": "parameters",
        "expected": expected,
        "actual": current,
        "match": expected == current,
    }


def _netlist_sha256_check(
    committed: dict[str, Any], report_path: str, netlist_override: str | None
) -> dict[str, Any]:
    """Cheap-mode ``netlist_sha256`` entry (issue #4313): does the committed
    gate-level netlist re-hash to the ``netlist_sha256`` the report recorded?

    The file is ``netlist_override`` when given (it always wins), otherwise
    the report's own ``netlist_path`` resolved from this checkout
    (``scope: "repo"`` only). A netlist that cannot be located here, or a file
    that does not exist, yields ``actual: null`` -- ``match: false``, never a
    silent skip. The hash is :func:`sha256_file` of the file as it sits on
    disk; the report hashed it after signed-qualifier stripping, which is what
    was written, so no re-stripping is applied.
    """
    path = resolve_committed_path(
        get_path(committed, ("netlist_path",)),
        report_path=report_path,
        override=netlist_override,
    )
    actual = sha256_file(path) if path and os.path.isfile(path) else None
    return hash_check(
        "netlist_sha256", get_path(committed, ("netlist_sha256",)), actual
    )


def check_synthesize_report(
    report_path: str,
    request_path: str,
    *,
    pdk_variant: str | None = None,
    pdk_root: str | None = None,
    netlist_override: str | None = None,
    skip_deck: bool = False,
) -> dict[str, Any]:
    """``klt synthesize <request> --check <report>`` (cheap mode, issue
    #2224): verify a previously committed ``klt synthesize --format json``
    report at ``report_path`` still reproduces from the request at
    ``request_path``, without invoking Yosys at all.

    Re-resolves the request's RTL sources and standard-cell liberty and
    re-hashes both (:func:`_synthesize_provenance_hashes`), comparing each
    against the ``sha256:``-prefixed digest the committed report already
    recorded in ``provenance.input.content_hash``/``provenance.deck
    .content_hash``. Returns the shared ``--check`` payload built by
    :func:`klayout_tools._report_verify.build_check_result` -- ``status:
    "match"`` when both agree, ``"drifted"`` (naming which one moved)
    otherwise -- exactly the contract ``klt drc --check`` established
    (issue #1106).

    A recorded hash that is itself ``None`` never counts as a match; see
    :func:`klayout_tools._report_verify.hash_check`. Tool identity
    (``engine_version``, ``provenance.klt_version``) is deliberately **not**
    among the checks, for the same reason ``--rerun`` excludes
    :data:`klayout_tools._report_verify.VOLATILE_FLOW_PATHS`: a Yosys upgrade
    that leaves the synthesis result untouched is not evidence drift, and
    reporting it as drift would train a consumer to ignore the verdict.

    A fifth check, ``netlist_sha256`` (issue #4313), re-hashes the committed
    gate-level netlist against the report's recorded ``netlist_sha256`` (see
    :func:`_netlist_sha256_check`); ``netlist_override`` is the ``--netlist``
    path. It proves the committed bytes are the ones the report recorded, not
    that Yosys would still produce them -- that is ``--rerun``.

    ``skip_deck`` (``--skip-deck``, issue #4315) never resolves the liberty,
    so the check runs on a host with no PDK install. The
    ``provenance.deck.content_hash`` entry is then omitted from ``checks[]``
    and named in an additive ``skipped[]`` list; a ``match`` proves the RTL,
    parameters, defines and netlist bytes, **not** that the library is the
    one the report was mapped against.

    Raises :class:`SynthesizeError` for a missing/unparseable committed
    report or an unresolvable request -- never a traceback.
    """
    committed = load_committed_report(report_path, SynthesizeError)
    input_hash, deck_hash = _synthesize_provenance_hashes(
        request_path,
        pdk_variant=pdk_variant,
        pdk_root=pdk_root,
        skip_deck=skip_deck,
    )
    checks = [
        hash_check(
            "provenance.input.content_hash",
            get_path(committed, ("provenance", "input", "content_hash")),
            input_hash,
        ),
        hash_check(
            "provenance.deck.content_hash",
            get_path(committed, ("provenance", "deck", "content_hash")),
            deck_hash,
        ),
        _parameters_check(
            get_path(committed, ("parameters",)),
            _synthesize_request_parameters(request_path),
        ),
        _defines_check(
            get_path(committed, ("defines",)),
            _synthesize_request_defines(request_path),
        ),
        synthesize_abc.mapping_check(
            committed,
            synthesize_abc.resolve_mapping(
                load_request(request_path), request_path, SynthesizeError
            ),
        ),
        _netlist_sha256_check(committed, report_path, netlist_override),
    ]
    if not skip_deck:
        return build_check_result(report_path=report_path, checks=checks)
    checks = [c for c in checks if c["field"] != "provenance.deck.content_hash"]
    return build_check_result(
        report_path=report_path,
        checks=checks,
        skipped=[
            {
                "field": "provenance.deck.content_hash",
                "reason": "--skip-deck: liberty not resolved",
            }
        ],
    )


def rerun_synthesize_report(
    report_path: str,
    request_path: str,
    *,
    pdk_variant: str | None = None,
    pdk_root: str | None = None,
    verify_equivalence: bool = False,
    equiv_timeout_s: float | None = None,
    equiv_engine: str | None = None,
    yosys_timeout_s: str | float | None = None,
    restructure_timing: bool = False,
    restructure_max_iterations: int | None = None,
) -> dict[str, Any]:
    """``klt synthesize <request> --check <report> --rerun`` (full mode,
    issue #2224): verify a committed report by actually re-running the
    synthesis the request declares and diffing the fresh report against the
    committed one.

    Diffs via :func:`klayout_tools._report_verify.diff_verdict_fields`,
    excluding :data:`klayout_tools._report_verify.VOLATILE_FLOW_PATHS`
    (``provenance.klt_version``/``klayout_version``/``pdk.version`` plus
    ``engine_version``) and canonicalizing
    :data:`RERUN_BOOKKEEPING_KEYS` out of both sides first. ``status:
    "drifted"`` names every other field that changed -- a moved
    ``instance_count``/``area_um2``/``timing``, a newly-inferred latch under
    ``structural``, a changed ``provenance.input.content_hash``.

    **Known limitations**, mirroring ``klt lvs``/``klt extract``'s own
    ``--rerun``: the optional flags (``--verify-equivalence``,
    ``--restructure-timing``, their tuning knobs) are not echoed anywhere in
    the response, so they cannot be recovered from ``report_path`` -- pass
    the same ones again here (the CLI does; that includes ``equiv_engine``,
    whose choice shows up as ``equivalence.engine``) or the corresponding blocks
    legitimately drift. A request pinning an explicit ``run_id`` cannot be
    re-run at all: :func:`_create_run_directory` refuses to reuse a retained
    run directory, and that refusal (a :class:`SynthesizeError` naming the
    directory) is surfaced unchanged rather than swallowed -- "could not
    verify" is the honest answer there, never a false ``"match"``.
    A request using ``outputs.dir`` *can* be re-run: the re-run ignores that
    stable directory (which the first run left populated) and writes into a
    fresh ``.klt/synthesize/run-<uuid>/`` scratch directory instead, so the
    committed artifacts are never touched.

    Raises :class:`SynthesizeError` for a missing/unparseable committed
    report or any error the re-run itself raises -- never a traceback.
    """
    committed = load_committed_report(report_path, SynthesizeError)
    fresh = run_synthesize(
        request_path,
        pdk_variant=pdk_variant,
        pdk_root=pdk_root,
        verify_equivalence=verify_equivalence,
        equiv_timeout_s=equiv_timeout_s,
        equiv_engine=equiv_engine,
        yosys_timeout_s=yosys_timeout_s,
        restructure_timing=restructure_timing,
        restructure_max_iterations=restructure_max_iterations,
        _scratch_run_dir=True,
    )
    return rerun_result_for("synthesize", report_path, committed=committed, fresh=fresh)

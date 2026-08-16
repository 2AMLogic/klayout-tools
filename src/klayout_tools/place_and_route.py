"""Place and route a synthesized netlist against a resolved standard-cell
PDK via OpenROAD, headless.

Pure library: :func:`run_place_and_route` returns plain Python data (a
``dict`` of JSON-serialisable primitives) and never prints, mirroring
``synthesize.py``/``lvs.py``. Serialisation and human-readable formatting
live in the CLI command module (``cli/place_and_route_cmd.py``).

This is Phase 4 of [Epic #391](https://github.com/2AMLogic/klayout-tools/issues/391)
("adopt the digital engine class -- Yosys + OpenROAD -- RTL->GDS as a first-
class ``klt`` flow"), the build carried by two accepted Phase 1 documents --
read them first:

- ``docs/design/openroad-invocation-survey.md`` (#397) settles the
  invocation shape (``openroad -no_init -exit script.tcl``, wrapping
  OpenROAD's native Tcl API stage-by-stage rather than
  OpenROAD-flow-scripts' (ORFS) Makefile) and the DEF->GDS merge approach
  (``def2stream.py``'s plain-``pya``-function structure, ported in-process
  onto ``klayout.db``).
- ``docs/design/digital-flow-contracts-spike.md`` section 5 (#399) settles
  the request/response JSON contract, the ``target_stage`` partial-
  completion design, and the exit-code table this module implements.

Like ``klt synthesize``/``klt lvs``/``klt sim``, ``klt place-and-route``
takes a **request document**, not positional file args.

Engine: ``"openroad"`` (only value implemented today; ``request.engine``
exists from day one so a later engine is an additive enum value, per the
contract spike section 2). OpenROAD is invoked as a **subprocess**
(``openroad -no_init -exit -metrics <file>.json <script>.tcl``), once per
logical stage -- never in-process, there is no Python binding for OpenROAD.

Stage granularity and invocation shape
---------------------------------------

The request/response contract names exactly four stages
(``"floorplan"``/``"place"``/``"cts"``/``"route"``, see :data:`STAGE_ORDER`)
via ``target_stage`` -- this module invokes **one OpenROAD process per named
stage**, chained via ``write_db``/``read_db`` ODB checkpoints between
invocations (verified live against ORFS's own real per-stage scripts,
``flow/scripts/load.tcl``'s ``load_design`` proc: every stage re-reads
liberty and either builds fresh from RTL or ``read_db``s the prior
checkpoint -- Sta's linked-library/SDC state is **not** carried across a
process boundary by ``write_db``/``read_db``, so every stage's generated
script re-issues ``read_liberty``/``create_clock``). This is a deliberate
per-stage-invocation design, not the survey's other option (a single
monolithic script) -- it is what makes ``target_stage`` a simple "how many
processes to run" decision, and what gives each ``stages[]`` entry its own
clean ``-metrics <file>.json`` snapshot (OpenROAD's ``-metrics`` flag dumps
the *whole process's* accumulated metrics DB at ``-exit``, so a fresh
process per stage is what isolates one stage's own metrics from the next
stage's).

``-metrics <file>.json`` confirmed working end-to-end
--------------------------------------------------------

The OpenROAD survey (section 6, point 1) flagged this as the open risk this
phase had to confirm or fall back from: **confirmed live** (this repo's own
worked example, run via a real ``openroad/orfs`` container against a real
volare-fetched ``sky130A`` install) that ``-metrics <file>.json`` captures a
flat JSON object of every named metric a stage's own commands populated --
both explicit ``report_*_metric`` proc calls (``report_worst_slack_metric``
-> ``timing__setup__ws``, ``report_tns_metric`` -> ``timing__setup__tns``,
``report_fmax_metric`` -> ``timing__fmax``, ``report_power_metric`` ->
``power__total``, ``report_design_area_metrics`` -> ``design__die__area``/
``design__core__area``/``design__instance__utilization``, and (``"cts"``/
``"route"`` stages only, issue #783) ``report_clock_skew_metric`` ->
``clock__skew__setup`` -- verified live via ``openroad -no_init -exit``'s
own ``info body report_clock_skew_metric`` against a real
``openroad/orfs:latest`` container, ``26Q3-1080-gab6fd26351``) *and* metrics
several stage commands populate automatically as a side effect
(``global_route``/``detailed_route`` -> ``route__wirelength``,
``detailed_placement`` -> ``route__wirelength__estimated``). No fallback to
scraping ``report_*`` text output was needed for these fields -- see
:func:`_extract_stage_metrics` for the exact key mapping. The one exception:
OpenROAD has **no** ``*_metric`` proc for setup/hold timing-*violation
counts* (only the scalar WNS/TNS) -- :func:`_count_violations` falls back to
counting ``"(VIOLATED)"`` lines in ``report_check_types -max_delay/-min_delay
-violators -format end``'s own stdout, exactly the fallback the contract
spike's build/wrap section authorises.

Floorplan methods
------------------

Three of ORFS's four floorplan-initialization methods are supported (the
fourth, IO-ring/footprint, is out of scope for a core-only block per the
survey section 2): ``"utilization"`` (``initialize_floorplan -utilization``),
``"explicit"`` (``initialize_floorplan -die_area/-core_area``), and
``"def"`` (``read_def -floorplan_initialize``, reusing an existing DEF's
floorplan). A request naming fields belonging to more than one method is
rejected, mirroring ORFS's own ``methods_defined > 1`` check
(``flow/scripts/floorplan.tcl``).

DEF->GDS merge
---------------

Ported directly from ``def2stream.py``'s own plain-``pya``-function
structure (survey section 4) onto this repo's already-installed
``klayout.db`` package, in-process -- never a ``klayout.sh -zz -rd ... -r
def2stream.py`` subprocess the way ORFS itself invokes it. Verified live
against the same real worked example above: a full floorplan->place->cts
->route run's ``write_def`` output, merged with the resolved standard-cell
GDS view and the tech+cell LEF (for layer/pin geometry), through zero
missing/orphan cells.

As-built netlist export (issue #996)
--------------------------------------

The ``"route"`` stage writes an OpenROAD ``write_verilog`` netlist
(``verilog_path``) alongside its ``write_def``/``def_path``: the design as
this command's own ``clock_tree_synthesis``/``repair_design``/
``repair_timing``/``repair_antennas`` calls actually left it, buffers,
resizes, and diodes included. Without it, the only netlist available to
build a golden-reference gate-level LVS from is ``klt synthesize``'s
**pre-CTS** output, which is *guaranteed* to differ from the routed layout
by however many cells P&R touched -- a divergence ``klt lvs`` has no way to
attribute (one real run: 40 of ~720 instances). ``write_verilog`` is a
top-level command of OpenROAD's always-loaded ``dbSta`` module
(``src/dbSta/src/dbReadVerilog.tcl`` -> ``sta::write_verilog_cmd``,
``src/dbSta/src/dbSta.i``), reading the OpenROAD network -- unrelated to
Yosys's identically-named command that ``klt synthesize`` drives. See
:func:`_stage_script_lines`'s own ``"route"`` branch for which flags are
deliberately not passed, and why the artifact is written at ``"route"``
only, never at ``"cts"``.

Standard-cell PDK plumbing
----------------------------

``pdk.cell_library``/``corner`` resolve a liberty exactly as ``klt
synthesize`` already does (:func:`_resolve_liberty`, this module's own
copy of that resolution -- each verb module in this repo is self-contained,
matching the existing precedent), and, like that module, accepts the CLI's
own ``--pdk``/``--pdk-root`` flags (mirroring ``klt extract``'s identical
pair) to pin a specific installed PDK variant/root rather than always
falling back to ``find_pdk()``'s own default search order. The tech +
merged-cell LEF pair resolves via the new :func:`klayout_tools.pdk.lef_files`
resolver (issue #397/#425 -- ``_ASSET_LAYOUT`` never carried a ``lef`` key),
pinned to whatever variant/root :func:`_resolve_liberty` already resolved.

Neither resolver is restricted to a single PDK family -- any standard-cell
library the resolved install ships ``libs_ref``/LEF assets for resolves the
same way. The one genuinely per-family gap is the ``cts``/``route`` stages'
own small reference-data tables (:data:`_CTS_BUFFER_CELLS`,
:data:`_ROUTING_LAYER_RANGE`, issue #629): a clock-tree buffer cell name and
a signal routing-layer range are not derivable from the resolved PDK install
itself the way liberty/LEF paths are, so each supported ``cell_library``
needs its own verified entry here (sourced from ORFS's own
``platforms/<variant>/config.mk`` where an ORFS platform exists, or
otherwise directly from the resolved install's own LEF/liberty content --
never guessed). A ``cell_library`` with no entry in either table still fails
with a clear error once a run reaches the stage that needs it, rather than
guessing. This reference data is never a runtime dependency; nothing in this
module shells out to, reads, or requires an ORFS checkout.

Deliberately out of scope for this v1 (a core-only block, matching the
contract's own IO-ring/footprint exclusion): tapcell insertion, power-grid
generation (PDN), metal fill, and a ``DONT_USE_CELLS``-style cell exclusion
list -- none of these are part of the request/response contract this phase
implements, and can be added later as additive request fields without a
contract-shape change.

Hard-macro placement (issue #438, Epic #393 Phase 2 Capability A)
--------------------------------------------------------------------

The v1 docstring above originally scoped *macro placement* out entirely;
this is the additive request field that closes that gap, exactly per its
own note ("can be added later as additive request fields"). An optional
``request.macros`` array names hard-macro instances (e.g. a LEF abstract
:mod:`klayout_tools.lef_abstract` emitted from an analog block) to fix at a
caller-given location during the ``"floorplan"`` stage, via OpenROAD's own
``place_macro -macro_name <instance> -location {x y} -orientation
<orientation>`` Tcl command (verified live against a real
``openroad/orfs`` container, ``help place_macro``'s own usage string) --
never OpenROAD's automatic macro placer (``rtl_macro_placer``), since a
socket-driven macro's location is a caller decision, not an optimization.
Each macro's LEF is ``read_lef``'d alongside the tech/cell LEF, before
``read_verilog``/``link_design`` (LEF must be loaded before the netlist that
references it structurally); the RTL's own top module must instantiate a
module whose name matches the macro LEF's own ``MACRO`` name, with a
matching port list, for ``link_design`` to resolve the instance's physical
view. An optional per-macro ``gds`` field also merges the macro's own GDS
view into the final ``gds_path`` output (mirroring the standard-cell GDS
merge below) -- when omitted, the DEF->GDS merge tolerates that instance's
cell being empty (a caller using this field purely for the DEF-level
placement/obstruction verification this issue's acceptance criteria call
for does not need to supply one).

Macro-pin routability cross-check (issue #464)
-------------------------------------------------

``klt lef-abstract`` (issue #438's other half) deliberately emits a ``PIN``
block with **no** ``PORT`` geometry when a socket-descriptor pin's declared
layer does not resolve to a routing-type tech-LEF layer (e.g. a device gate
pin on bare poly) -- a structurally valid LEF, reported via that command's
own ``warnings[]``/``unroutable_pins[]``, never an error there. Left
unchecked, wiring such a pin into a real net and placing the macro here
used to surface only as an opaque OpenROAD ``GRT-0029`` failure several
stages into a real run (global routing, well after floorplan/place/cts have
already succeeded) -- accurate, but useless for tracing the failure back to
its actual root cause (an earlier ``lef-abstract`` run, a different process
entirely).

:func:`_validate_macros` now catches this **before** OpenROAD is invoked at
all: for each declared macro, it reads the macro LEF's own ``PIN`` blocks
(:func:`klayout_tools.lef_header.read_lef_header`, whose ``pins[].has_port``
field this cross-check exists for) to find pins with no ``PORT`` at all,
then does a best-effort structural-Verilog scan of the netlist
(:func:`_macro_instance_port_connections`) for that instance's own named
port connections (``.PIN(NET)``). A ``PORT``-less pin that connects to a
non-empty net is rejected with a specific :class:`PlaceAndRouteError` naming
the instance, pin, and net -- before any OpenROAD subprocess runs. A
``PORT``-less pin the netlist leaves unconnected (``.PIN()``, or simply
absent from the instantiation's own port list) is not an error -- an
internally-terminated node is a legitimate macro-pin state, not a routing
request. The netlist scan is deliberately conservative: when the specific
``<cell_name> <instance>( ... )`` instantiation cannot be confidently
located in the netlist text (e.g. a positional-connection instantiation, a
non-Verilog/stubbed netlist, or the instance simply not referenced there),
the cross-check is skipped entirely for that macro rather than risk a false
positive or false negative on text it cannot actually parse -- OpenROAD's
own ``link_design`` remains the authority on whether the netlist and LEF
actually agree structurally.

Timing-driven global routing + bounded antenna-repair iteration (issue #939)
------------------------------------------------------------------------------

Epic #700 Phase 2's native-routing survey (``docs/design/native-routing-
survey.md`` #934, section 4.1) named this its own Priority 1 item: audit
``detailed_route``/``global_route``'s documented flag surface for a
timing-driven or congestion-tuning mode not currently passed, and evaluate
``repair_antennas``'s optional iteration count as a bounded multi-pass
alternative to the single-pass call issue #759 shipped.

**Methodology note (limitation, stated up front):** issue #783's own audit
of ``clock_tree_synthesis`` used live introspection (``info body
<proc>``/``help <proc>``) against a real ``openroad/orfs:latest`` container.
No such container was reachable in this task's environment (a Docker daemon
is present but this task has no permission to use it). This audit instead
reads OpenROAD's/OpenROAD-flow-scripts' own upstream Tcl **and C++** source
directly from ``The-OpenROAD-Project/OpenROAD``@``9b2de5c``/
``The-OpenROAD-Project/OpenROAD-flow-scripts``@``ef52564`` (``master``,
fetched 2026-08-13) -- a real, citable, but *not independently
container-verified* source; it has not been cross-checked against the exact
pinned build #783 used (``26Q3-1080-gab6fd26351``), so there is a small
residual risk of version drift between what this audit read and what any
given deployed OpenROAD binary actually accepts. Both new flags below are
additive and off by default specifically so a caller can still get today's
exact behaviour if this risk ever manifests as a real mismatch.

- **``global_route``'s flag surface** (``src/grt/src/GlobalRouter.tcl``/
  ``src/grt/README.md``) does carry a genuine timing-aware congestion knob:
  ``-critical_nets_percentage <percent>`` (0-100, default ``0``) -- "the
  percentage of nets with the worst slack value that are considered timing
  critical, having preference over other nets during congestion
  iterations." (``src/grt/README.md``). Traced into
  ``GlobalRouter::setCriticalNetsPercentage`` (``src/grt/src/
  GlobalRouter.cpp``): it forwards straight into FastRoute's/CUGR's own net
  ordering and is force-reset to ``0`` (with a logged warning) when no
  liberty/timing is loaded -- never silently wrong, always a real no-op
  when timing data is unavailable. This module's ``"route"`` stage always
  has a linked liberty + clock by the time it runs (`_validate_constraints`
  already requires a clock from ``"place"`` onward), so the flag is live
  whenever it is set. Exposed as the optional
  ``request.route_critical_nets_percentage`` field
  (:func:`_validate_route_critical_nets_percentage`, 0-100, default ``0``)
  -- ``0`` reproduces ``global_route``'s own default and emits no flag at
  all, the explicit A/B disable path this issue's acceptance criteria
  require. **Not evaluated with a real A/B run in this
  pass** (no OpenROAD available in this task's environment either, see
  below) -- shipped as an opt-in, off-by-default option rather than an
  always-on flag like #783's CTS pair, precisely because its QoR effect on
  this repo's own corpus is unmeasured.
- **``detailed_route``'s flag surface** (``src/drt/src/TritonRoute.tcl``,
  every key/flag in its ``sta::define_cmd_args``/``parse_key_args`` block
  transcribed) has **no** timing-driven or congestion-tuning flag at all --
  a genuine negative result, not an absence of searching: ``-or_seed``/
  ``-or_k`` (already passed), ``-droute_end_iter`` (a rip-up-reroute
  iteration cap, default/max 64, not scored by timing), ``-db_process_node``,
  ``-disable_via_gen``, ``-via_in_pin_bottom_layer``/``-via_in_pin_top_layer``/
  ``-via_access_layer``, ``-bottom_routing_layer``/``-top_routing_layer``
  (both deprecated -- use ``set_routing_layers``, already what this stage
  does), ``-verbose``, distributed-routing flags, ``-clean_patches``,
  ``-no_pin_access``, ``-min_access_points``, ``-save_guide_updates``,
  ``-repair_pdn_vias``, and an internal-only ``-single_step_dr`` (the
  source's own comment: "not a user option ... intended for algorithm
  development"). This confirms the survey's own open question (section 1)
  the honest way -- no flag was added to ``detailed_route`` itself.
- **``repair_antennas``'s iteration surface** (``src/grt/src/
  GlobalRouter.tcl``) does carry a real ``-iterations <n>`` flag (default
  ``1``, matching this stage's current single-pass call exactly) -- but
  ``GlobalRouter.cpp``'s own implementation explicitly warns
  (``"repair_antennas should perform only one iteration when the routing
  source is detailed routing."``) when called with ``iterations != 1``
  *after* ``detailed_route`` has already run -- exactly this stage's own
  call pattern. Passing ``-iterations`` directly on the existing call would
  therefore either warn-and-do-nothing-useful or (per the surrounding
  ``IncrementalGRoute`` loop body) drift the global-route guides across
  multiple internal iterations without ever legalizing them through a real
  ``detailed_route`` pass in between -- the wrong tool for this stage's
  shape. OpenROAD-flow-scripts' own canonical flow
  (``flow/scripts/detail_route.tcl``, its ``MAX_REPAIR_ANTENNAS_ITER_DRT``
  variable) instead loops at the **flow level**: a plain (default
  1-iteration) ``repair_antennas`` call, a full ``detailed_route`` reroute,
  then ``check_antennas``, repeated up to a bound. This stage now mirrors
  that exact shape via the optional ``request.max_antenna_repair_iterations``
  field (:func:`_validate_max_antenna_repair_iterations`, integer
  ``1``-``8``, default ``1``) -- ``1`` reproduces today's exact generated
  Tcl, byte-for-byte; a higher value repeats the ``repair_antennas``/
  ``detailed_route`` pair that many times unconditionally (no Tcl-level
  early exit on a zero-violation ``check_antennas`` result -- a deliberate
  simplification: every other generated script in this module is a flat,
  branch-free command sequence, and adding the first Tcl-level control flow
  here to save wall-clock on an already-rare residual-violation case was
  judged not worth that departure; a caller who wants the cost of extra
  passes only when needed should leave this at its default).
- **A/B measurement**: **not performed with real OpenROAD in this pass** --
  no ``openroad`` binary or ``openroad/orfs`` container was available in
  this task's environment (same constraint noted above), so
  ``antenna_violation_count``/``route_drc_violation_count`` (the latter
  still unimplemented -- survey section 4.5, a separate, not-yet-filed
  issue)/``wirelength_um``/``worst_slack_ns``/wall-clock deltas on the
  ``gcd``/``modexp``/``mult8`` corpus trio remain **unverified** against a
  real engine run. Both new fields are additive, off-by-default, and
  covered by unit tests asserting the exact generated Tcl (mirroring this
  module's own existing per-flag test style) instead.

Post-route SPEF STA (``request.post_route_spef``, issue #948, Epic #700
Phase 3, ``docs/design/post-route-sta-survey.md`` section 4.1): the
``"route"`` stage's own top-level timing fields are computed from
``estimate_parasitics -global_routing`` -- a coarse global-routing Steiner
estimate, not parasitics extracted from the detailed-routed geometry
``write_def`` actually commits. The optional ``post_route_spef`` boolean
(default ``false``) opts in to a real-parasitics **A/B** pass, never a
replacement: once the DEF->GDS merge completes, ``klt extract --parasitics``
runs against the merged routed GDS and writes its per-net R/C model as SPEF
(:func:`_post_route_spef_metrics`); a second ``openroad`` invocation, seeded
from the ``"route"`` stage's own ODB checkpoint, loads that SPEF via
``read_spef`` and re-reports the identical slack/violation-count metrics
(:func:`_spef_sta_script_lines`) into the additive ``spef_sta`` response
field. That extraction runs with ``def_net_names=True`` (issue #951), so each
routed net is named from the DEF net name KLayout's LEF/DEF reader recorded on
its geometry rather than from GDS text labels -- the merge emits labels for
top-level pins only, and naming from them left correlation at literally 0% of
nets when #948 shipped. Correlation against OpenSTA's own linked-design net
list -- the survey's own flagged open risk -- is still checked explicitly
*before* ``read_spef`` runs and reported in both directions
(``spef_sta.nets_annotated``/``nets_total`` and
``spef_sta.design_nets_annotated``/``design_nets_total``,
:func:`_count_spef_nets_annotated`), never assumed. See
``docs/cli/place-and-route.md``'s "Post-route SPEF STA" section for the full
contract.

Post-route SDF export (``request.post_route_sdf``, issue #1002, Epic #700
Phase 3, ``docs/design/post-route-sta-survey.md`` §4.3): the survey's §4.3
names ``write_sdf`` as the *upstream* half of an SDF-annotated gate-level
re-simulation, and is explicit about where it belongs -- "after §4.1's
``read_spef``, so the written SDF reflects real routed-parasitic delays, not
the global-routing estimate". The optional ``post_route_sdf`` boolean
(default ``false``, and a request error unless ``post_route_spef`` is also
``true``) adds exactly that: one ``write_sdf`` call inside the *same*
``post_route_spef`` OpenSTA session, immediately after its ``read_spef``, so
the emitted IEEE-1497 delays are computed from the design's own resolved
liberty (``request.pdk.corner``) plus the extracted routed parasitics rather
than from a synthetic or uniform delay model. The written file is reported as
``spef_sta.sdf_path`` and is the artifact ``klt functional-verification``'s
own ``options.sdf`` block consumes (see
``docs/design/sdf-annotate-feasibility-spike.md`` for the Icarus half's own
verified invocation recipe).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Iterable
from typing import Any

from ._paths import _load_request_json, validate_request_shape
from ._provenance import build_provenance
from .lef_header import read_lef_header
from .pdk import (
    PdkNotFoundError,
    find_pdk,
    lef_files,
    list_cell_libraries,
    list_lib_corners,
)

#: Bumped only on a non-additive (breaking) change to this command's own
#: JSON shape -- see docs/json-contract.md.
SCHEMA_VERSION = 1

#: ``"openroad"`` is the only engine implemented today; the field is present
#: in the request from day one (contract spike section 2) so a later backend
#: is an additive enum value, never a contract-shape change.
SUPPORTED_ENGINES = ("openroad",)

#: The four stages ``target_stage`` names, in execution order -- see this
#: module's own docstring "Stage granularity and invocation shape".
STAGE_ORDER = ("floorplan", "place", "cts", "route")

#: Valid ``request.macros[].orientation`` values -- OpenROAD's own
#: orientation vocabulary (the same set ``place_macro``/``place_cell``
#: accept), matching LEF's ``ORIENT`` enumeration.
_MACRO_ORIENTATIONS = frozenset(
    {"R0", "R90", "R180", "R270", "MX", "MY", "MXR90", "MYR90"}
)

#: Fields the floorplan spec's three mutually-exclusive methods each need,
#: used both to select the ``initialize_floorplan``/``read_def`` Tcl branch
#: and to detect a request naming fields from more than one method (mirrors
#: ORFS's own ``methods_defined > 1`` check, survey section 2).
_FLOORPLAN_METHOD_FIELDS: dict[str, tuple[str, ...]] = {
    "utilization": ("utilization_pct",),
    "explicit": ("die_area_um", "core_area_um"),
    "def": ("def_path",),
}

#: Per-cell-library clock-buffer choice for `clock_tree_synthesis`'s
#: ``-root_buf``/``-buf_list`` flags -- not derivable from the resolved PDK
#: install itself (issue #629), so each supported ``cell_library`` needs its
#: own verified entry here rather than a guess. A ``cell_library`` with no
#: entry raises a clear error when a run needs to reach the ``"cts"`` stage.
#:
#: **Neither ORFS platform pins a CTS buffer of its own** (issue #637):
#: ``flow/scripts/cts.tcl`` appends ``-buf_list`` only when the *optional*
#: ``CTS_BUF_LIST`` variable is set, and neither ``platforms/sky130hd`` nor
#: ``platforms/gf180`` sets it -- nor any ``CTS_BUF_CELL``, which does not
#: exist anywhere in ORFS (zero code-search hits across that repo). ORFS
#: therefore lets OpenROAD auto-select. This module names one explicitly
#: instead, because a run-to-run reproducible clock tree is worth more here
#: than OpenROAD's own per-version choice. Sourced, per library:
#:
#: - ``sky130_fd_sc_hd`` -> ``sky130_fd_sc_hd__buf_4``: the buffer
#:   ``platforms/sky130hd/config.mk`` itself names, via
#:   ``MIN_BUF_CELL_AND_PORTS = sky130_fd_sc_hd__buf_4 A X``.
#: - ``gf180mcu_fd_sc_mcu9t5v0`` -> ``gf180mcu_fd_sc_mcu9t5v0__buf_4``:
#:   ``platforms/gf180/config.mk``'s own reference non-inverting buffer for
#:   exactly this library -- ``ABC_DRIVER_CELL = gf180mcu_fd_sc_mcu$(TRACK_
#:   OPTION)$(POWER_OPTION)__buf_4``, whose platform defaults
#:   (``TRACK_OPTION ?= 9t``, ``POWER_OPTION ?= 5v0``) resolve to
#:   ``gf180mcu_fd_sc_mcu9t5v0``. Confirmed present as ``MACRO
#:   gf180mcu_fd_sc_mcu9t5v0__buf_4`` in that platform's own standard-cell
#:   LEF (``lef/gf180mcu_5LM_1TM_9K_9t_sc.lef``), and not excluded by the
#:   platform's ``DONT_USE_CELLS = *_1``. That platform's own
#:   ``MIN_BUF_CELL_AND_PORTS`` names ``__dlya_4`` instead -- deliberately
#:   *not* mirrored from the sky130hd entry's derivation here, since
#:   ``dlya`` is a delay cell for hold fixing, the wrong shape for a
#:   clock-tree root buffer.
_CTS_BUFFER_CELLS: dict[str, str] = {
    "sky130_fd_sc_hd": "sky130_fd_sc_hd__buf_4",
    "gf180mcu_fd_sc_mcu9t5v0": "gf180mcu_fd_sc_mcu9t5v0__buf_4",
}

#: Per-cell-library ``set_routing_layers -signal`` range for the ``"route"``
#: stage. Same not-derivable-from-the-install, verified-not-guessed posture
#: as :data:`_CTS_BUFFER_CELLS` -- but here ORFS *does* pin the value per
#: platform, as ``MIN_ROUTING_LAYER``/``MAX_ROUTING_LAYER``:
#:
#: - ``sky130_fd_sc_hd`` -> ``met1-met5``, from
#:   ``platforms/sky130hd/config.mk`` (``MIN_ROUTING_LAYER ?= met1``,
#:   ``MAX_ROUTING_LAYER ?= met5``).
#: - ``gf180mcu_fd_sc_mcu9t5v0`` -> ``Metal2-Metal5``, from
#:   ``platforms/gf180/config.mk`` (``MIN_ROUTING_LAYER ?= Metal2``,
#:   ``MAX_ROUTING_LAYER ?= Metal5``). This range deliberately starts one
#:   layer **above** the stack's bottom routing layer, unlike sky130hd's
#:   (issue #637): this library's standard cells pin out on ``Metal1``
#:   itself (``buf_4``'s ``I``/``Z`` are both ``LAYER Metal1`` in
#:   ``lef/gf180mcu_5LM_1TM_9K_9t_sc.lef``), so ``Metal1`` is left to pin
#:   access and intra-cell/power-rail geometry rather than opened up to
#:   free signal routing. sky130hd has no equivalent constraint -- its
#:   cells pin out on ``li1``, below ``met1`` entirely (``buf_4``'s
#:   ``A``/``X`` are ``LAYER li1`` in ``lef/sky130_fd_sc_hd_merged.lef``,
#:   with only the power rails on ``met1``). The layer-name case
#:   (``Metal``, not ``met``) is that PDK's own LEF convention, confirmed
#:   against the platform tech LEF: ``lef/gf180mcu_5LM_1TM_9K_9t_tech.lef``
#:   declares ``Metal1``..``Metal5`` as its five ``TYPE ROUTING`` layers.
#:
#: ORFS is **reference data only** for both tables, never a runtime
#: dependency -- nothing in this module shells out to, reads, or requires an
#: ORFS checkout, and no ORFS file is vendored here. In particular
#: ``platforms/gf180/config.mk``'s trailing ``-include
#: $(GF180_PRIVATE_DIR)/private.mk`` (a soft include, commented "for
#: proprietary tool enablements that are not public") is irrelevant to these
#: values: every variable read above is set in the public ``config.mk``
#: itself, above that line, and each is independently corroborated against
#: the platform's own open-source LEFs (issue #637). Values above read
#: 2026-08-09 from ``The-OpenROAD-Project/OpenROAD-flow-scripts`` @ ``master``.
_ROUTING_LAYER_RANGE: dict[str, str] = {
    "sky130_fd_sc_hd": "met1-met5",
    "gf180mcu_fd_sc_mcu9t5v0": "Metal2-Metal5",
}

#: Per-cell-library antenna-diode cell (and its signal pin) for the
#: ``"route"`` stage's post-route ``repair_antennas`` call. Same not-
#: derivable-from-the-install, verified-not-guessed posture as
#: :data:`_CTS_BUFFER_CELLS`/:data:`_ROUTING_LAYER_RANGE` (issue #629/#637)
#: -- but here **neither** ORFS platform's own ``config.mk`` names a diode
#: cell at all (confirmed against the real ``openroad/orfs:latest``
#: container's own vendored ORFS checkout, 2026-08-11: zero
#: ``ANTENNA``/``DIODE``-named `config.mk` variable across any platform;
#: ORFS's own ``flow/scripts/detail_route.tcl`` calls ``repair_antennas``
#: with **no** diode-cell argument at all, relying on OpenROAD's own
#: null-diode "jumper only" fallback). So this table's source of truth is
#: each platform's own standard-cell LEF, not `config.mk` -- specifically,
#: the one macro each platform's LEF marks ``CLASS CORE/core ANTENNACELL``:
#:
#: - ``sky130_fd_sc_hd`` -> ``sky130_fd_sc_hd__diode_2``, pin ``DIODE`` --
#:   ``platforms/sky130hd/lef/sky130_fd_sc_hd_merged.lef``'s only
#:   ``ANTENNACELL``-classed macro, with exactly one non-power/ground pin
#:   (``DIODE``, ``USE SIGNAL``; ``VGND``/``VNB``/``VPB``/``VPWR`` are all
#:   ``USE GROUND``/``USE POWER``). Not excluded by that platform's
#:   ``DONT_USE_CELLS`` (`config.mk`, checked live). Matches the sky130
#:   candidate the survey itself flagged as **[LIT]**-tier recollection
#:   only -- now confirmed **[REPO-RUN]** against the real LEF.
#: - ``gf180mcu_fd_sc_mcu9t5v0`` -> ``gf180mcu_fd_sc_mcu9t5v0__antenna``,
#:   pin ``I`` -- ``platforms/gf180/lef/gf180mcu_5LM_1TM_9K_9t_sc.lef``'s
#:   only ``ANTENNACELL``-classed macro (``CLASS core ANTENNACELL``), with
#:   exactly one non-power/ground pin (``I``, ``DIRECTION INPUT``; ``VDD``/
#:   ``VSS`` are ``USE power``/``USE ground``).
#:
#: The pin is recorded here for verification/documentation only (confirming
#: each cell has exactly one signal pin, matching what OpenROAD's own
#: auto-derivation would select) -- it is never passed to the Tcl call.
#: Verified live (``openroad -no_init``, ``help repair_antennas``, the same
#: ``openroad/orfs:latest`` container, build ``26Q3-1080-gab6fd26351``) that
#: the current ``repair_antennas`` Tcl command (**plural** -- the survey's
#: own "repair_antenna" naming does not exist as a command) takes only a
#: single positional ``diode_cell`` argument; there is no
#: ``-diode_pin_name``-shaped flag in the current API -- the pin (MTerm) is
#: auto-derived internally as the named cell's unique non-power/ground port,
#: erroring if more than one exists (which the entries above already rule
#: out).
_ANTENNA_DIODE_CELLS: dict[str, tuple[str, str]] = {
    "sky130_fd_sc_hd": ("sky130_fd_sc_hd__diode_2", "DIODE"),
    "gf180mcu_fd_sc_mcu9t5v0": ("gf180mcu_fd_sc_mcu9t5v0__antenna", "I"),
}

#: Per-cell-library ``klt extract --deck`` name (issue #948, Epic #700
#: Phase 3) -- used only by the optional ``request.post_route_spef`` path
#: (see :func:`_post_route_spef_metrics`) to resolve which curated
#: ``klayout_tools.decks`` extraction deck matches this run's PDK family.
#: Same "not derivable from the resolved PDK install itself" posture as
#: :data:`_CTS_BUFFER_CELLS`/:data:`_ROUTING_LAYER_RANGE` above -- a standard
#: cell library name (LEF/liberty naming) and an extraction deck name
#: (``klayout_tools.decks``' own family key) are two independent naming
#: conventions this module has to bridge explicitly. Matches
#: :func:`klayout_tools.decks.get_extraction_deck`'s/``get_parasitics_deck``'s
#: own accepted names exactly (``"sky130"``/``"gf180mcu"``).
_EXTRACT_DECK_FOR_CELL_LIBRARY: dict[str, str] = {
    "sky130_fd_sc_hd": "sky130",
    "gf180mcu_fd_sc_mcu9t5v0": "gf180mcu",
}

#: Fixed internal `global_placement -density` target -- not an exposed
#: request field (the contract's `floorplan` block sizes the die/core, not
#: placement density); verified live at this value against the worked
#: example (docstring above).
_GLOBAL_PLACEMENT_DENSITY = 0.6

_OPENROAD_VERSION_RE = re.compile(r"OpenROAD\s+(\S+)")

#: Matches `check_antennas`'s own stdout summary line, e.g.
#: ``"[INFO ANT-0002] Found 3 net violations."`` -- see
#: :func:`_count_antenna_violations`.
_ANTENNA_VIOLATION_COUNT_RE = re.compile(r"Found (\d+) net violations")

#: Matches TritonRoute's own constant-tie rejection, e.g. ``"[ERROR
#: DRT-0305] Net zero_ of signal type GROUND is not routable by TritonRoute.
#: Move to special nets."`` -- see :func:`_constant_tie_diagnosis` (#854).
_DRT_CONSTANT_NET_RE = re.compile(
    r"\[ERROR DRT-0305\][^\n]*?Net (?P<net>\S+) of signal type "
    r"(?P<sig_type>\S+)[^\n]*"
)

#: Markers delimiting each `report_check_types -violators` block in a
#: stage's captured stdout, so :func:`_count_violations` can isolate the
#: setup-check block from the hold-check block within one run's combined
#: output.
_SETUP_VIOLATIONS_BEGIN = "===KLT_SETUP_VIOLATIONS_BEGIN==="
_SETUP_VIOLATIONS_END = "===KLT_SETUP_VIOLATIONS_END==="
_HOLD_VIOLATIONS_BEGIN = "===KLT_HOLD_VIOLATIONS_BEGIN==="
_HOLD_VIOLATIONS_END = "===KLT_HOLD_VIOLATIONS_END==="

#: Same marker convention as the setup/hold pair above, isolating
#: `check_antennas`'s own stdout (run post-`repair_antennas`, `"route"`
#: stage only) so :func:`_count_antenna_violations` can parse its summary
#: count line without picking up unrelated output.
_ANTENNA_VIOLATIONS_BEGIN = "===KLT_ANTENNA_VIOLATIONS_BEGIN==="
_ANTENNA_VIOLATIONS_END = "===KLT_ANTENNA_VIOLATIONS_END==="

#: Literal per-violation header line TritonRoute writes to its own
#: `detailed_route -output_drc <rpt>` report -- one per violation, e.g.
#: ``"violation type: Metal Short"`` followed by indented `srcs:`/`bbox =
#: (...)`/`Layer: ...` detail lines. Confirmed live (`strings` against a
#: real `openroad/orfs:latest` build's `openroad` binary) as the exact
#: literal the binary both *writes* (`"violation type: "` -- issue #938)
#: and internally *re-parses* the same report format with
#: (`"\\s*violation type: (.*)"`) -- not guessed from documentation. See
#: :func:`_count_route_drc_violations`.
_ROUTE_DRC_VIOLATION_TYPE_LINE = "violation type: "

#: Same marker convention as the setup/hold/antenna pairs above, isolating
#: the ``request.post_route_spef`` net-name-correlation check's own report
#: (issue #948, extended to two lines by #951) -- see
#: :func:`_spef_sta_script_lines`/:func:`_count_spef_nets_annotated`. Line 1
#: is the SPEF-side ``"<annotated> <total>"`` pair (how many of the SPEF's own
#: net names resolve to a net in the linked design), line 2 the design-side
#: pair (how many of the *design's* nets the SPEF names at all).
_SPEF_NET_CHECK_BEGIN = "===KLT_SPEF_NET_CHECK_BEGIN==="
_SPEF_NET_CHECK_END = "===KLT_SPEF_NET_CHECK_END==="
_SPEF_NET_CHECK_RE = re.compile(r"(\d+)\s+(\d+)\s+(\d+)\s+(\d+)")

#: Response fields whose value is always "the last completed stage's own
#: value, restated at top level" -- see the contract spike section 5's
#: `stages` field description. Built once via `dict.get` so a field a stage
#: didn't populate degrades to `null`, never a `KeyError`.
_TOP_LEVEL_METRIC_KEYS = (
    "die_area_um2",
    "core_area_um2",
    "utilization_pct",
    "wirelength_um",
    "worst_slack_ns",
    "total_negative_slack_ns",
    "fmax_mhz",
    "setup_violation_count",
    "hold_violation_count",
    "antenna_violation_count",
    # Additive (issue #938, native-routing survey #935 section 4.5) --
    # never replaces an existing field; `null` on any stage before
    # `"route"` (only the `"route"` stage runs `detailed_route`).
    "route_drc_violation_count",
    "estimated_power_mw",
    # Additive (issue #783, P&R survey #735 section 3.4) -- never replaces an
    # existing field; `null` on any stage that hasn't run CTS yet.
    "clock_skew_ns",
    # Additive (issue #949, post-route-sta-survey #944/#945 section 4.2) --
    # never replaces `worst_slack_ns` (which stays the nominal-corner-only
    # value); `null` on any stage before `"route"` -- the corner sweep runs
    # once, after the route stage's own checkpoint is written (see
    # `_run_corner_sweep`).
    "worst_setup_slack_ns",
    "worst_hold_slack_ns",
)


class PlaceAndRouteError(Exception):
    """Raised when a place-and-route run cannot even reach the requested
    ``target_stage``: a missing/malformed request file, an unresolvable/
    unreadable netlist, an unresolvable ``pdk.cell_library``/``corner``/LEF,
    a floorplan spec naming more than one method, or an OpenROAD engine
    error partway through a stage.

    The CLI turns this into a clean stderr message + exit code 1, never a
    traceback -- matching ``klt synthesize``'s ``SynthesizeError``. Per the
    contract spike section 5's "Partial-completion design": a run that
    reaches (or exceeds) the requested ``target_stage`` is *always* a
    success (``exit 0``, ``stage_reached`` >= ``target_stage``) even when it
    stopped short of a *later* stage the request never asked for -- this
    exception is raised only when a run fails to reach the stage it was
    actually asked to reach.
    """


def load_request(request_path: str) -> dict[str, Any]:
    """Read and minimally validate a ``klt place-and-route`` request JSON
    file.

    Raises :class:`PlaceAndRouteError` if the file is missing/unreadable,
    not valid JSON, or missing a required top-level field (``netlist``,
    ``hdl_toplevel``, ``pdk``, ``floorplan``, ``seed``). Does not require a
    ``schema`` field, matching every other request-taking verb's
    ``load_request``.
    """
    request = _load_request_json(request_path, PlaceAndRouteError)
    return validate_request_shape(
        request,
        "request file",
        error_cls=PlaceAndRouteError,
        required_fields=("netlist", "hdl_toplevel", "pdk", "floorplan", "seed"),
    )


def run_place_and_route(
    request_path: str,
    *,
    pdk_variant: str | None = None,
    pdk_root: str | None = None,
) -> dict[str, Any]:
    """Run the OpenROAD place-and-route flow declared by the request at
    ``request_path``, through the requested (or default, ``"route"``)
    ``target_stage``.

    ``pdk_variant``/``pdk_root`` (the CLI's ``--pdk``/``--pdk-root`` flags,
    mirroring ``klt extract``'s identical pair and ``klt synthesize``'s own
    ``run_synthesize`` kwargs) optionally pin a specific installed PDK
    variant/root, passed straight through to :func:`_resolve_liberty`'s own
    ``find_pdk()`` call. ``None`` (the default) leaves ``find_pdk()``'s own
    default search order in effect, unchanged from before this parameter
    existed.

    Returns a dict matching the documented JSON schema (see
    ``docs/cli/place-and-route.md`` /
    ``docs/design/digital-flow-contracts-spike.md`` section 5). Raises
    :class:`PlaceAndRouteError` for anything that prevents the run from
    reaching the requested ``target_stage`` -- see that exception's
    docstring for the exact scope.

    Generated Tcl scripts, ODB checkpoints, raw OpenROAD ``-metrics`` JSON
    dumps, the final DEF, and the merged GDS are all written to
    ``.klt/place-and-route/`` next to the request file (the same
    "next to the input" convention ``klt synthesize``'s ``.klt/synthesize/``
    already uses) and kept as debuggable artifacts, never deleted.
    """
    request = load_request(request_path)
    request_dir = os.path.dirname(os.path.abspath(request_path))

    engine = request.get("engine", "openroad")
    if engine not in SUPPORTED_ENGINES:
        raise PlaceAndRouteError(
            f"unsupported engine '{engine}' (supported: {', '.join(SUPPORTED_ENGINES)})"
        )

    netlist_path = _resolve_netlist(request["netlist"], request_dir)

    hdl_toplevel = request["hdl_toplevel"]
    if not isinstance(hdl_toplevel, str) or not hdl_toplevel:
        raise PlaceAndRouteError("request.hdl_toplevel must be a non-empty string")

    pdk_spec = request["pdk"]
    if not isinstance(pdk_spec, dict):
        raise PlaceAndRouteError("request.pdk must be a JSON object")
    cell_library = pdk_spec.get("cell_library")
    if not isinstance(cell_library, str) or not cell_library:
        raise PlaceAndRouteError("request.pdk.cell_library is required")
    requested_corner = pdk_spec.get("corner")
    if requested_corner is not None and not (
        isinstance(requested_corner, str) and requested_corner
    ):
        raise PlaceAndRouteError(
            "request.pdk.corner must be a non-empty string when given"
        )

    floorplan = _validate_floorplan(request["floorplan"])
    io_spec = _validate_io(request.get("io"))
    macros = _validate_macros(request.get("macros"), request_dir, netlist_path)
    clock_port, clock_period_ns = _validate_constraints(request.get("constraints"))
    seed = _validate_seed(request["seed"])
    target_stage = _validate_target_stage(request.get("target_stage", "route"))
    route_critical_nets_percentage = _validate_route_critical_nets_percentage(
        request.get("route_critical_nets_percentage")
    )
    max_antenna_repair_iterations = _validate_max_antenna_repair_iterations(
        request.get("max_antenna_repair_iterations")
    )
    post_route_spef = _validate_post_route_spef(request.get("post_route_spef"))
    post_route_sdf = _validate_post_route_sdf(
        request.get("post_route_sdf"), post_route_spef=post_route_spef
    )

    liberty_path, corner, pdk_info = _resolve_liberty(
        cell_library, requested_corner, variant=pdk_variant, root=pdk_root
    )
    tech_lef, cell_lef = _resolve_lef(cell_library, pdk_info)

    stage_index = STAGE_ORDER.index(target_stage)
    stages_to_run = STAGE_ORDER[: stage_index + 1]

    # Stages beyond "floorplan" need a clock -- P&R signoff (CTS, routing,
    # timing-driven repair) has no meaning without one, and STA/`-metrics`
    # would otherwise report an unconstrained-design sentinel instead of a
    # real number. `_validate_constraints` already enforced clock_port and
    # clock_period_ns are given together; this additionally requires them
    # once a caller actually asks for a stage that needs them.
    if stage_index >= STAGE_ORDER.index("place") and clock_port is None:
        raise PlaceAndRouteError(
            "request.constraints.clock_port/clock_period_ns are required to "
            f"reach target_stage '{target_stage}'"
        )
    if stage_index >= STAGE_ORDER.index("place") and io_spec is None:
        raise PlaceAndRouteError(
            f"request.io.layer_h/layer_v are required to reach target_stage "
            f"'{target_stage}'"
        )
    if (
        stage_index >= STAGE_ORDER.index("cts")
        and cell_library not in _CTS_BUFFER_CELLS
    ):
        raise PlaceAndRouteError(
            f"no clock-tree buffer cell known for standard-cell library "
            f"'{cell_library}' (supported: {', '.join(sorted(_CTS_BUFFER_CELLS))}) "
            f"-- cannot reach target_stage '{target_stage}'"
        )
    if (
        stage_index >= STAGE_ORDER.index("route")
        and cell_library not in _ROUTING_LAYER_RANGE
    ):
        raise PlaceAndRouteError(
            f"no routing-layer range known for standard-cell library "
            f"'{cell_library}' (supported: {', '.join(sorted(_ROUTING_LAYER_RANGE))}) "
            f"-- cannot reach target_stage '{target_stage}'"
        )
    if (
        stage_index >= STAGE_ORDER.index("route")
        and cell_library not in _ANTENNA_DIODE_CELLS
    ):
        raise PlaceAndRouteError(
            f"no antenna-diode cell known for standard-cell library "
            f"'{cell_library}' (supported: {', '.join(sorted(_ANTENNA_DIODE_CELLS))}) "
            f"-- cannot reach target_stage '{target_stage}'"
        )

    output_dir = os.path.join(request_dir, ".klt", "place-and-route")
    try:
        os.makedirs(output_dir, exist_ok=True)
    except OSError as exc:
        raise PlaceAndRouteError(
            f"could not create output directory '{output_dir}': {exc}"
        ) from exc

    stages: list[dict[str, Any]] = []
    checkpoint_path: str | None = None

    for stage in stages_to_run:
        script_path = os.path.join(output_dir, f"pnr_{hdl_toplevel}_{stage}.tcl")
        metrics_path = os.path.join(output_dir, f"{hdl_toplevel}_{stage}_metrics.json")
        next_checkpoint = os.path.join(output_dir, f"{hdl_toplevel}_{stage}.odb")

        lines = _stage_script_lines(
            stage=stage,
            checkpoint_in=checkpoint_path,
            checkpoint_out=next_checkpoint,
            tech_lef=tech_lef,
            cell_lef=cell_lef,
            liberty_path=liberty_path,
            netlist_path=netlist_path,
            hdl_toplevel=hdl_toplevel,
            floorplan=floorplan,
            io_spec=io_spec,
            macros=macros,
            clock_port=clock_port,
            clock_period_ns=clock_period_ns,
            cell_library=cell_library,
            seed=seed,
            output_dir=output_dir,
            route_critical_nets_percentage=route_critical_nets_percentage,
            max_antenna_repair_iterations=max_antenna_repair_iterations,
        )
        _write_script(script_path, lines)

        completed = _run_openroad(script_path, metrics_path)
        if completed.returncode != 0:
            raise PlaceAndRouteError(_engine_error_message(stage, completed))

        metrics = _read_metrics(metrics_path, stage)
        setup_count, hold_count = (None, None)
        if stage != "floorplan":
            setup_count = _count_violations(
                completed.stdout, _SETUP_VIOLATIONS_BEGIN, _SETUP_VIOLATIONS_END
            )
            hold_count = _count_violations(
                completed.stdout, _HOLD_VIOLATIONS_BEGIN, _HOLD_VIOLATIONS_END
            )
        antenna_count = None
        route_drc_count = None
        worst_setup_slack_ns = None
        worst_hold_slack_ns = None
        if stage == "route":
            antenna_count = _count_antenna_violations(completed.stdout)
            # Same deterministic path `_stage_script_lines`'s own `route`
            # branch built for `detailed_route -output_drc` -- recomputed
            # here (rather than threaded back out of that function) since
            # both `output_dir`/`hdl_toplevel` are already in scope and the
            # naming is a fixed, single-source-of-truth convention (issue
            # #938).
            drc_report_path = os.path.join(output_dir, f"{hdl_toplevel}_route_drc.rpt")
            route_drc_count = _count_route_drc_violations(drc_report_path)

            # Issue #949: sweep every corner `cell_library` ships for
            # setup/hold slack, as a second OpenROAD invocation over the
            # checkpoint this stage's own script just wrote -- see
            # `_run_corner_sweep`/`_corner_sweep_script_lines` for why this
            # is a separate session rather than more Tcl appended above.
            assert io_spec is not None  # required to reach "route" (validated above)
            corners = list_lib_corners(cell_library, pdk_info)
            worst_setup_slack_ns, worst_hold_slack_ns = _run_corner_sweep(
                checkpoint_in=next_checkpoint,
                corners=corners,
                io_spec=io_spec,
                clock_port=clock_port,
                clock_period_ns=clock_period_ns,
                output_dir=output_dir,
                hdl_toplevel=hdl_toplevel,
            )

        stages.append(
            _extract_stage_metrics(
                stage,
                metrics,
                setup_count,
                hold_count,
                antenna_count,
                route_drc_count,
                worst_setup_slack_ns=worst_setup_slack_ns,
                worst_hold_slack_ns=worst_hold_slack_ns,
            )
        )
        checkpoint_path = next_checkpoint

    gds_path: str | None = None
    verilog_path: str | None = None
    spef_sta: dict[str, Any] | None = None
    layer_map_info: dict[str, Any] | None = None
    if target_stage == "route":
        def_path = os.path.join(output_dir, f"{hdl_toplevel}.def")
        gds_path = os.path.join(output_dir, f"{hdl_toplevel}.gds")
        # Same deterministic path `_stage_script_lines`'s own `"route"`
        # branch just handed `write_verilog` (issue #996) -- recomputed here
        # rather than threaded back out, exactly as `def_path` above and the
        # `-output_drc` report path earlier already are.
        verilog_path = os.path.join(output_dir, f"{hdl_toplevel}.v")
        layer_map_info = _merge_def_to_gds(
            def_path=def_path,
            tech_lef=tech_lef,
            cell_lef=cell_lef,
            pdk_info=pdk_info,
            cell_library=cell_library,
            hdl_toplevel=hdl_toplevel,
            macros=macros,
            out_path=gds_path,
        )
        # `request.post_route_spef` (issue #948, Epic #700 Phase 3): real
        # routed-geometry parasitics, via `klt extract --parasitics` against
        # the GDS just merged above, fed back into a fresh OpenSTA session
        # (`read_spef`) seeded from the `"route"` stage's own checkpoint --
        # after the DEF->GDS merge, before this function's own final
        # response is assembled, exactly the "after write_def/the DEF->GDS
        # merge... before the report calls" ordering the issue's own scope
        # names (the report calls in question are this helper's own
        # internal `_metrics_report_lines`/`_violation_count_lines`, run
        # inside the second `openroad` invocation it launches -- the merge
        # itself runs in-process Python, so no *Tcl* command can sit
        # "between" it and `write_def`; the ordering constraint is honored
        # at the pipeline-step level instead). Off by default -- see
        # `_validate_post_route_spef`'s own docstring for why.
        if post_route_spef:
            spef_sta = _post_route_spef_metrics(
                output_dir=output_dir,
                hdl_toplevel=hdl_toplevel,
                gds_path=gds_path,
                def_path=def_path,
                cell_library=cell_library,
                liberty_path=liberty_path,
                clock_port=clock_port,
                clock_period_ns=clock_period_ns,
                checkpoint_in=checkpoint_path,
                # Issue #1002: `write_sdf` inside that same session, right
                # after its `read_spef` -- see `_validate_post_route_sdf`.
                write_sdf=post_route_sdf,
            )
    else:
        def_path = None

    last_stage = stages[-1]
    top_metrics = {key: last_stage.get(key) for key in _TOP_LEVEL_METRIC_KEYS}

    engine_version = _openroad_version()
    # `deck` names the resolved LEF/liberty/GDS-view platform set (contract
    # spike section 5's own "e.g. sky130hd" example) -- `<cell_library>__
    # <corner>` matching the STA/timing corner actually requested, the same
    # naming `klt synthesize`'s own `deck.name` uses, since that is the
    # decision-relevant corner (the tech LEF's own "nom"/"min"/"max"
    # parasitic-corner selection is an internal implementation detail, not
    # a request field).
    deck_name = f"{cell_library}__{corner}"
    provenance = build_provenance(
        deck_name=deck_name,
        deck_path=liberty_path,
        pdk=pdk_info,
        input_path=netlist_path,
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "engine": engine,
        "engine_version": engine_version,
        "hdl_toplevel": hdl_toplevel,
        "status": "ok",
        "stage_reached": target_stage,
        "seed": seed,
        **top_metrics,
        "stages": stages,
        "macros": [
            {
                "instance": macro["instance"],
                "lef": macro["lef"],
                "x_um": macro["x_um"],
                "y_um": macro["y_um"],
                "orientation": macro["orientation"],
            }
            for macro in macros
        ],
        "def_path": def_path,
        "gds_path": gds_path,
        # Additive field (issue #1029): whether the DEF->GDS merge above
        # actually applied a KLayout LEF/DEF layer-map file, and how it was
        # resolved -- `_resolve_layer_map` silently degrades to no map at
        # all (`resolution: "none"`) when neither a variant-named nor a
        # family-level map file exists, which previously left no trace in
        # this response for a caller to notice. `null` unless
        # `stage_reached` is `"route"`, mirroring `gds_path`/`verilog_path`.
        "layer_map": layer_map_info,
        # Additive field (issue #996): the `write_verilog`-produced,
        # *as-built* gate-level netlist -- the design as CTS/timing repair/
        # antenna repair actually left it, i.e. the netlist the routed
        # `def_path`/`gds_path` above genuinely implement. `null` unless
        # `stage_reached` is `"route"`, mirroring those two fields. This is
        # the artifact a golden-reference digital LVS run should build its
        # reference from; `klt synthesize`'s own netlist is pre-CTS and is
        # *expected* to diverge from the routed layout.
        "verilog_path": verilog_path,
        # Additive field (issue #948): `null` unless `request.post_route_spef`
        # was `true` *and* `stage_reached` is `"route"` -- the real-parasitics
        # A/B counterpart to the top-level (`estimate_parasitics
        # -global_routing`-derived) `worst_slack_ns`/etc. fields above. See
        # `_post_route_spef_metrics`'s docstring for the field list. Its own
        # `sdf_path` member (issue #1002) is `null` in turn unless
        # `request.post_route_sdf` was also `true`.
        "spef_sta": spef_sta,
        "provenance": provenance,
    }


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #


def _resolve_netlist(netlist: Any, request_dir: str) -> str:
    if not isinstance(netlist, str) or not netlist:
        raise PlaceAndRouteError("request.netlist must be a non-empty string")
    path = netlist if os.path.isabs(netlist) else os.path.join(request_dir, netlist)
    if not os.path.isfile(path):
        raise PlaceAndRouteError(f"netlist not found: {netlist}")
    try:
        with open(path, "rb"):
            pass
    except OSError as exc:
        raise PlaceAndRouteError(f"could not read netlist '{netlist}': {exc}") from exc
    return os.path.abspath(path)


def _validate_floorplan(floorplan: Any) -> dict[str, Any]:
    if not isinstance(floorplan, dict):
        raise PlaceAndRouteError("request.floorplan must be a JSON object")

    method = floorplan.get("method")
    if method not in _FLOORPLAN_METHOD_FIELDS:
        raise PlaceAndRouteError(
            "request.floorplan.method must be one of: "
            + ", ".join(sorted(_FLOORPLAN_METHOD_FIELDS))
        )

    methods_defined = [
        name
        for name, fields in _FLOORPLAN_METHOD_FIELDS.items()
        if any(floorplan.get(field) is not None for field in fields)
    ]
    if len(methods_defined) > 1:
        raise PlaceAndRouteError(
            "request.floorplan specifies fields from more than one floorplan "
            f"method: {', '.join(sorted(methods_defined))} -- exactly one "
            "method's fields may be set (mirroring OpenROAD-flow-scripts' "
            "own methods_defined > 1 check)"
        )

    required = _FLOORPLAN_METHOD_FIELDS[method]
    missing = [field for field in required if floorplan.get(field) is None]
    if missing:
        raise PlaceAndRouteError(
            f"request.floorplan.method '{method}' requires: {', '.join(required)} "
            f"(missing: {', '.join(missing)})"
        )

    if method in ("utilization", "explicit") and not floorplan.get("site"):
        raise PlaceAndRouteError(
            f"request.floorplan.site is required for method '{method}'"
        )

    if method == "explicit":
        for field in ("die_area_um", "core_area_um"):
            value = floorplan[field]
            if (
                not isinstance(value, (list, tuple))
                or len(value) != 4
                or not all(isinstance(v, (int, float)) for v in value)
            ):
                raise PlaceAndRouteError(
                    f"request.floorplan.{field} must be an array of 4 numbers "
                    "[llx, lly, urx, ury]"
                )

    return dict(floorplan)


def _validate_io(io_spec: Any) -> dict[str, str] | None:
    if io_spec is None:
        return None
    if not isinstance(io_spec, dict):
        raise PlaceAndRouteError("request.io must be a JSON object")
    layer_h = io_spec.get("layer_h")
    layer_v = io_spec.get("layer_v")
    if not (
        isinstance(layer_h, str) and layer_h and isinstance(layer_v, str) and layer_v
    ):
        raise PlaceAndRouteError(
            "request.io.layer_h/layer_v must both be non-empty strings"
        )
    return {"layer_h": layer_h, "layer_v": layer_v}


def _validate_macros(
    macros: Any, request_dir: str, netlist_path: str | None = None
) -> list[dict[str, Any]]:
    """Validate the optional ``request.macros`` array (issue #438). Each
    entry fixes one hard-macro instance at a caller-given location during
    the ``"floorplan"`` stage -- see this module's docstring "Hard-macro
    placement" section. Returns ``[]`` when the field is omitted/``None``
    (unchanged behavior -- purely additive).

    ``netlist_path`` (issue #464) additionally cross-checks each macro's LEF
    pins with no ``PORT`` geometry at all against the netlist's own wiring --
    see this module's docstring "Macro-pin routability cross-check" section.
    ``None`` (the default, used by this module's own direct unit tests below
    that exercise validation in isolation, with no netlist in hand) skips
    that cross-check entirely -- it is purely additive to the pre-existing
    validation this function already performed."""
    if macros is None:
        return []
    if not isinstance(macros, list):
        raise PlaceAndRouteError("request.macros must be a list")

    validated: list[dict[str, Any]] = []
    for i, macro in enumerate(macros):
        if not isinstance(macro, dict):
            raise PlaceAndRouteError(f"request.macros[{i}] must be an object")

        instance = macro.get("instance")
        if not isinstance(instance, str) or not instance:
            raise PlaceAndRouteError(
                f"request.macros[{i}].instance is required and must be a "
                "non-empty string"
            )

        lef = macro.get("lef")
        if not isinstance(lef, str) or not lef:
            raise PlaceAndRouteError(
                f"request.macros[{i}].lef is required and must be a non-empty string"
            )
        lef_path = lef if os.path.isabs(lef) else os.path.join(request_dir, lef)
        if not os.path.isfile(lef_path):
            raise PlaceAndRouteError(f"request.macros[{i}].lef not found: {lef}")
        macro_cells = read_lef_header(lef_path)["macros"]
        if len(macro_cells) != 1:
            raise PlaceAndRouteError(
                f"request.macros[{i}].lef '{lef}' must declare exactly one MACRO "
                f"(found {len(macro_cells)})"
            )
        macro_cell_name = macro_cells[0]["name"]

        no_port_pin_names = sorted(
            pin["name"] for pin in macro_cells[0]["pins"] if not pin["has_port"]
        )
        if no_port_pin_names and netlist_path is not None:
            _reject_wired_port_less_pins(
                index=i,
                instance=instance,
                lef=lef,
                macro_cell_name=macro_cell_name,
                no_port_pin_names=no_port_pin_names,
                netlist_path=netlist_path,
            )

        for key in ("x_um", "y_um"):
            if not isinstance(macro.get(key), (int, float)) or isinstance(
                macro.get(key), bool
            ):
                raise PlaceAndRouteError(
                    f"request.macros[{i}].{key} is required and must be a number"
                )

        orientation = macro.get("orientation", "R0")
        if orientation not in _MACRO_ORIENTATIONS:
            raise PlaceAndRouteError(
                f"request.macros[{i}].orientation must be one of: "
                + ", ".join(sorted(_MACRO_ORIENTATIONS))
            )

        gds = macro.get("gds")
        gds_path: str | None = None
        if gds is not None:
            if not isinstance(gds, str) or not gds:
                raise PlaceAndRouteError(
                    f"request.macros[{i}].gds must be a non-empty string when given"
                )
            gds_path = gds if os.path.isabs(gds) else os.path.join(request_dir, gds)
            if not os.path.isfile(gds_path):
                raise PlaceAndRouteError(f"request.macros[{i}].gds not found: {gds}")

        validated.append(
            {
                "instance": instance,
                "lef": os.path.abspath(lef_path),
                "cell_name": macro_cell_name,
                "x_um": float(macro["x_um"]),
                "y_um": float(macro["y_um"]),
                "orientation": orientation,
                "gds": gds_path,
            }
        )

    instances = [m["instance"] for m in validated]
    if len(instances) != len(set(instances)):
        raise PlaceAndRouteError("request.macros[].instance values must be unique")

    return validated


#: A structural-Verilog module instantiation with **named** port
#: connections: ``<cell_name> <instance> ( .PORT(NET), ... ) ;`` -- the form
#: every real synthesis tool (Yosys included, per this module's own
#: ``read_verilog``/``link_design`` use) emits for a blackbox module
#: instance. Deliberately does **not** match a ``#( ... )`` parameter block
#: (an analog macro blackbox is never parameterized) or a positional
#: connection list (no declared port order is available to this module to
#: interpret one) -- either shape simply fails to match, and
#: :func:`_macro_instance_port_connections` reports "cannot confidently
#: locate" (``None``) rather than a wrong answer.
_MACRO_INSTANCE_RE_TEMPLATE = r"\b{cell}\b\s+\b{instance}\b\s*\("

#: One named port connection within an instantiation's own port list, e.g.
#: ``.Q1_1_G(net123)`` or ``.Q1_1_G()`` (left unconnected).
_PORT_CONNECTION_RE = re.compile(r"\.\s*([A-Za-z_$][A-Za-z0-9_$]*)\s*\(([^()]*)\)")

_LINE_COMMENT_RE = re.compile(r"//[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


def _strip_verilog_comments(text: str) -> str:
    return _LINE_COMMENT_RE.sub("", _BLOCK_COMMENT_RE.sub("", text))


def _macro_instance_port_connections(
    netlist_path: str, cell_name: str, instance_name: str
) -> dict[str, str] | None:
    """Best-effort structural-Verilog lookup of one macro instance's own
    named port connections (``{port_name: connection_expression}``), used
    only by :func:`_reject_wired_port_less_pins`'s netlist-wiring
    cross-check (issue #464).

    Returns ``None`` -- "cannot confidently locate" -- when the netlist
    cannot be read, or no ``<cell_name> <instance_name>( ... )``
    instantiation is found (a positional-connection instantiation, a
    non-Verilog/placeholder netlist, or the instance genuinely not
    referenced by that exact name/type pair). The caller treats ``None`` as
    "skip the cross-check for this macro" -- never as "no pins are wired".
    """
    try:
        with open(netlist_path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return None

    text = _strip_verilog_comments(text)
    pattern = re.compile(
        _MACRO_INSTANCE_RE_TEMPLATE.format(
            cell=re.escape(cell_name), instance=re.escape(instance_name)
        )
    )
    match = pattern.search(text)
    if match is None:
        return None

    depth = 1
    i = match.end()
    while i < len(text) and depth > 0:
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
        i += 1
    if depth != 0:
        return None  # unterminated -- malformed/truncated text, don't guess

    port_list_text = text[match.end() : i - 1]
    connections = {
        port_name: connection.strip()
        for port_name, connection in _PORT_CONNECTION_RE.findall(port_list_text)
    }
    return connections if connections else None


def _reject_wired_port_less_pins(
    *,
    index: int,
    instance: str,
    lef: str,
    macro_cell_name: str,
    no_port_pin_names: list[str],
    netlist_path: str,
) -> None:
    """Raise :class:`PlaceAndRouteError` when any of ``no_port_pin_names``
    (macro pins with no ``PORT`` geometry at all in the declared LEF) is
    actually wired to a real net in the netlist's own instantiation of this
    macro -- see this module's docstring "Macro-pin routability cross-check"
    section. A no-op when the netlist cannot be confidently parsed for this
    instance, or when every no-``PORT`` pin is left unconnected there."""
    connections = _macro_instance_port_connections(
        netlist_path, macro_cell_name, instance
    )
    if connections is None:
        return

    for pin_name in no_port_pin_names:
        net = connections.get(pin_name)
        if net:
            raise PlaceAndRouteError(
                f"request.macros[{index}] instance '{instance}' pin "
                f"'{pin_name}' has no PORT geometry in '{lef}' (MACRO "
                f"'{macro_cell_name}') but is wired to net '{net}' in the "
                "netlist -- OpenROAD's global router would fail with "
                "GRT-0029 once this pin is reached. The pin's declared "
                "layer likely does not resolve to a routing-type tech-LEF "
                "layer (see klt lef-abstract's 'unroutable_pins[]'/"
                "warnings[] for that run); fix the LEF abstract, or leave "
                f"'{pin_name}' unconnected in the netlist."
            )


def _validate_constraints(constraints: Any) -> tuple[str | None, float | None]:
    if constraints is None:
        return None, None
    if not isinstance(constraints, dict):
        raise PlaceAndRouteError("request.constraints must be a JSON object")

    clock_port = constraints.get("clock_port")
    clock_period_ns = constraints.get("clock_period_ns")
    if clock_port is None and clock_period_ns is None:
        return None, None
    if not (isinstance(clock_port, str) and clock_port):
        raise PlaceAndRouteError(
            "request.constraints.clock_port must be a non-empty string"
        )
    if not (isinstance(clock_period_ns, (int, float)) and clock_period_ns > 0):
        raise PlaceAndRouteError(
            "request.constraints.clock_period_ns must be a positive number"
        )
    return clock_port, float(clock_period_ns)


def _validate_seed(seed: Any) -> int:
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise PlaceAndRouteError("request.seed must be an integer")
    return seed


def _validate_target_stage(target_stage: Any) -> str:
    if target_stage not in STAGE_ORDER:
        raise PlaceAndRouteError(
            "request.target_stage must be one of: " + ", ".join(STAGE_ORDER)
        )
    return target_stage


def _validate_route_critical_nets_percentage(value: Any) -> int:
    """Optional ``request.route_critical_nets_percentage`` (issue #939) --
    ``global_route``'s own ``-critical_nets_percentage`` flag: the
    percentage of worst-slack nets treated as timing-critical during
    congestion-removal iterations (0-100, the same bound OpenROAD's own
    ``sta::check_percent`` enforces). Omitted/``None`` defaults to ``0``,
    matching ``global_route``'s own default and this module's prior
    behaviour exactly -- the explicit A/B disable path this issue's
    acceptance criteria require. See this module's own docstring, "Timing-
    driven global routing + bounded antenna-repair iteration"."""
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise PlaceAndRouteError(
            "request.route_critical_nets_percentage must be an integer"
        )
    if not (0 <= value <= 100):
        raise PlaceAndRouteError(
            "request.route_critical_nets_percentage must be between 0 and 100"
        )
    return value


#: Upper bound on ``request.max_antenna_repair_iterations`` (issue #939) --
#: each additional iteration re-runs a full ``repair_antennas``/
#: ``detailed_route`` pair (a real, potentially expensive detailed-routing
#: pass), so this option is deliberately *bounded*, not unlimited, per this
#: issue's own scope ("a bounded multi-pass option"). Chosen generously
#: relative to ORFS's own default posture (``MAX_REPAIR_ANTENNAS_ITER_DRT``
#: is unset in ORFS's own default flow, i.e. inactive/1) while still ruling
#: out a runaway request.
_MAX_ANTENNA_REPAIR_ITERATIONS_CAP = 8


def _validate_max_antenna_repair_iterations(value: Any) -> int:
    """Optional ``request.max_antenna_repair_iterations`` (issue #939) -- a
    bounded multi-pass generalisation of the ``"route"`` stage's existing
    single ``repair_antennas``+``detailed_route`` reroute pass (issue #759).
    Omitted/``None`` defaults to ``1``, reproducing today's exact generated
    Tcl byte-for-byte. See this module's own docstring, "Timing-driven
    global routing + bounded antenna-repair iteration"."""
    if value is None:
        return 1
    if isinstance(value, bool) or not isinstance(value, int):
        raise PlaceAndRouteError(
            "request.max_antenna_repair_iterations must be an integer"
        )
    if not (1 <= value <= _MAX_ANTENNA_REPAIR_ITERATIONS_CAP):
        raise PlaceAndRouteError(
            "request.max_antenna_repair_iterations must be between 1 and "
            f"{_MAX_ANTENNA_REPAIR_ITERATIONS_CAP}"
        )
    return value


def _validate_post_route_spef(value: Any) -> bool:
    """Optional ``request.post_route_spef`` (issue #948, Epic #700 Phase 3,
    ``docs/design/post-route-sta-survey.md`` §4.1) -- opts in to a real
    post-route parasitic-extraction A/B pass on the ``"route"`` stage:
    ``klt extract --parasitics`` runs against the merged routed GDS, its
    output is written as SPEF, and a fresh OpenSTA session seeded from the
    ``"route"`` stage's own checkpoint re-reports slack/violation counts via
    ``read_spef`` -- *alongside*, never replacing, that stage's existing
    ``estimate_parasitics -global_routing``-derived top-level fields (see
    this module's own docstring and :func:`_post_route_spef_metrics`).

    Omitted/``None`` defaults to ``False`` -- this feature adds real
    wall-clock cost on top of every existing ``"route"``-stage caller (a
    `klt extract --parasitics` pass over the merged GDS, plus a second
    `openroad` subprocess invocation), so it is opt-in, matching this
    module's own convention for every other flag whose added cost a survey
    flagged rather than assumed free (`route_critical_nets_percentage`,
    `max_antenna_repair_iterations`) -- the explicit A/B disable path this
    module's other flags already established.
    """
    if value is None:
        return False
    if not isinstance(value, bool):
        raise PlaceAndRouteError("request.post_route_spef must be a boolean")
    return value


def _validate_post_route_sdf(value: Any, *, post_route_spef: bool) -> bool:
    """Optional ``request.post_route_sdf`` (issue #1002, Epic #700 Phase 3,
    ``docs/design/post-route-sta-survey.md`` §4.3) -- opts in to writing the
    ``post_route_spef`` OpenSTA session's own delays out as an IEEE-1497 SDF
    file (``write_sdf``, immediately after that session's ``read_spef``), for
    ``klt functional-verification``'s ``options.sdf`` block to back-annotate
    onto a gate-level re-run of the design's own testbench.

    Omitted/``None`` defaults to ``False``: like every other flag in this
    module whose cost a survey flagged rather than assumed free, it is
    opt-in, and it has no meaning at all without the session that produces
    it.

    **Requires ``post_route_spef: true``**, and says so rather than silently
    writing nothing. That is not an implementation convenience -- it is the
    whole point of §4.3's own sequencing: an SDF written from a session whose
    parasitics came from ``estimate_parasitics -global_routing`` would carry
    the coarse global-routing estimate's delays while *looking* exactly like
    a post-route measurement to every downstream simulation that annotates
    it. The `write_sdf` call therefore rides inside the real-parasitics
    session or not at all.
    """
    if value is None:
        return False
    if not isinstance(value, bool):
        raise PlaceAndRouteError("request.post_route_sdf must be a boolean")
    if value and not post_route_spef:
        raise PlaceAndRouteError(
            "request.post_route_sdf requires request.post_route_spef: true -- "
            "the SDF is written from the post-route read_spef session, so an "
            "SDF written without it would carry estimate_parasitics "
            "-global_routing delays while looking like a post-route measurement"
        )
    return value


# --------------------------------------------------------------------------- #
# PDK / LEF / liberty resolution
# --------------------------------------------------------------------------- #


def _resolve_liberty(
    cell_library: str,
    requested_corner: str | None,
    *,
    variant: str | None = None,
    root: str | None = None,
) -> tuple[str, str, dict[str, Any]]:
    """Resolve ``(liberty_path, corner, pdk_info)`` for ``cell_library`` --
    this module's own copy of ``synthesize.py``'s identical resolution
    (each verb module in this repo is self-contained). ``variant``/``root``
    (the CLI's ``--pdk``/``--pdk-root`` flags, threaded through from
    :func:`run_place_and_route`) select a specific installed PDK
    variant/root exactly as :func:`klayout_tools.pdk.find_pdk` does; ``None``
    for either leaves that resolver's own default search order in effect.
    Raises :class:`PlaceAndRouteError` (never
    :class:`~klayout_tools.pdk.PdkNotFoundError`).
    """
    try:
        info = find_pdk(variant=variant, root=root)
    except PdkNotFoundError as exc:
        raise PlaceAndRouteError(str(exc)) from exc

    libs_ref = info["assets"]["libs_ref"]
    if libs_ref is None:
        raise PlaceAndRouteError(
            f"liberty not found for deck: resolved PDK install "
            f"'{info['variant']}' at '{info['root']}' ships no libs_ref asset"
        )

    lib_dir = os.path.join(libs_ref, cell_library)
    if not os.path.isdir(lib_dir):
        raise PlaceAndRouteError(
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
            raise PlaceAndRouteError(
                f"liberty not found for deck: could not determine a nominal "
                f"corner for '{cell_library}' -- pass request.pdk.corner "
                "explicitly"
            )

    liberty_path = os.path.join(lib_dir, "lib", f"{cell_library}__{corner}.lib")
    if not os.path.isfile(liberty_path):
        raise PlaceAndRouteError(
            f"liberty not found for deck: no '{corner}' corner for "
            f"'{cell_library}' under resolved PDK install '{info['variant']}' "
            f"(expected '{liberty_path}')"
        )
    return liberty_path, corner, info


def _resolve_lef(cell_library: str, pdk_info: dict[str, Any]) -> tuple[str, str]:
    """Resolve the tech + merged-cell LEF pair for ``cell_library``, pinned
    to the *same* PDK install/variant :func:`_resolve_liberty` already
    resolved (never re-searches). Raises :class:`PlaceAndRouteError` when
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
        raise PlaceAndRouteError(
            f"LEF not found for deck: standard-cell library '{cell_library}' "
            f"under resolved PDK install '{pdk_info['variant']}' at "
            f"'{pdk_info['root']}' is missing: {', '.join(missing)}"
        )
    return tech_lef, cell_lef


def _resolve_gds_view(pdk_info: dict[str, Any], cell_library: str) -> str:
    libs_ref = pdk_info["assets"]["libs_ref"]
    gds_path = os.path.join(libs_ref or "", cell_library, "gds", f"{cell_library}.gds")
    if libs_ref is None or not os.path.isfile(gds_path):
        raise PlaceAndRouteError(
            f"standard-cell GDS view not found for '{cell_library}' under "
            f"resolved PDK install '{pdk_info['variant']}' (expected "
            f"'{gds_path}')"
        )
    return gds_path


def _resolve_layer_map(pdk_info: dict[str, Any]) -> tuple[str | None, str]:
    """The KLayout LEF/DEF layer-map file open_pdks ships alongside its
    ``klayout`` tool area (``libs.tech/klayout/tech/<variant>.map``), or
    ``(None, "none")`` when the resolved install ships no ``klayout`` asset
    or no matching map file at all -- the DEF->GDS merge still proceeds
    without one (matching ``def2stream.py``'s own ``if len(layer_map) > 0``
    guard), just without a guaranteed-matching layer/datatype assignment for
    routing shapes.

    Prefers the variant-named file (``<variant>.map``, e.g.
    ``sky130A.map``) when it exists (``resolution="exact"``). Falls back to
    a family-level file (``<family>.map``, e.g. ``gf180mcu.map`` for variant
    ``gf180mcuC``/``gf180mcuD``) when the variant-named file is absent
    (``resolution="family"``) -- some open_pdks families (gf180mcu, unlike
    sky130) ship a single ``klayout.tech`` map file shared across every
    variant rather than one per variant (issue #1029). ``family`` is
    derived by stripping a trailing single uppercase PDK-suite designator
    from the variant, the same convention :func:`klayout_tools.pdk.
    lvs_deck_file` already uses for its own variant/family fallback --
    restated locally rather than imported, matching this module's existing
    "each verb module is self-contained" precedent (see
    ``lef_abstract.py``'s own duplicate of this function).
    """
    klayout_dir = pdk_info["assets"].get("klayout")
    if klayout_dir is None:
        return None, "none"
    tech_dir = os.path.join(klayout_dir, "tech")
    variant = pdk_info["variant"]
    exact = os.path.join(tech_dir, f"{variant}.map")
    if os.path.isfile(exact):
        return exact, "exact"

    family = variant
    if len(family) > 1 and family[-1].isupper():
        family = family[:-1]
    if family != variant:
        family_candidate = os.path.join(tech_dir, f"{family}.map")
        if os.path.isfile(family_candidate):
            return family_candidate, "family"

    return None, "none"


# --------------------------------------------------------------------------- #
# Tcl script generation
# --------------------------------------------------------------------------- #


def _write_script(script_path: str, lines: list[str]) -> None:
    try:
        with open(script_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    except OSError as exc:
        raise PlaceAndRouteError(
            f"could not write place-and-route script '{script_path}': {exc}"
        ) from exc


def _clock_lines(clock_port: str | None, clock_period_ns: float | None) -> list[str]:
    """``create_clock`` (OpenROAD's minimal SDC-equivalent input, contract
    spike section 5's ``constraints`` field) -- must run only **after** the
    design is linked (``link_design``, or a ``read_db`` that already
    restored a linked network): ``create_clock``'s own ``[get_ports ...]``
    lookup raises ``STA-1570 No network has been linked`` otherwise
    (confirmed live for issue #425's own worked example -- see
    :func:`_stage_script_lines`'s call sites, never before ``link_design``).
    """
    if clock_port is None:
        return []
    return [
        f"create_clock -name {clock_port} -period {clock_period_ns} "
        f"[get_ports {clock_port}]"
    ]


def _floorplan_init_lines(floorplan: dict[str, Any]) -> list[str]:
    method = floorplan["method"]
    if method == "def":
        return [f"read_def -floorplan_initialize {floorplan['def_path']}"]

    if method == "utilization":
        args = [f"-utilization {floorplan['utilization_pct']}"]
        if floorplan.get("aspect_ratio") is not None:
            args.append(f"-aspect_ratio {floorplan['aspect_ratio']}")
        if floorplan.get("core_margin_um") is not None:
            args.append(f"-core_space {floorplan['core_margin_um']}")
        args.append(f"-site {floorplan['site']}")
        return ["initialize_floorplan " + " ".join(args)]

    # method == "explicit"
    die = " ".join(str(v) for v in floorplan["die_area_um"])
    core = " ".join(str(v) for v in floorplan["core_area_um"])
    return [
        f"initialize_floorplan -die_area {{{die}}} -core_area {{{core}}} "
        f"-site {floorplan['site']}"
    ]


def _metrics_report_lines(
    *, include_fmax: bool, include_power: bool, include_clock_skew: bool = False
) -> list[str]:
    lines = [
        "report_worst_slack_metric -setup",
        "report_tns_metric -setup",
        "report_design_area_metrics",
    ]
    if include_fmax:
        lines.append("report_fmax_metric")
    if include_power:
        lines.append("report_power_metric")
    if include_clock_skew:
        # `-setup` matches this module's existing convention of reporting
        # only the setup-side variant of a metric that also has a `-hold`
        # form (`report_worst_slack_metric -setup`/`report_tns_metric
        # -setup` above) -- `-hold` skew is a possible additive follow-on,
        # not this issue's scope (#783). Only meaningful once a clock tree
        # exists, so callers gate this to the `"cts"`/`"route"` stages.
        lines.append("report_clock_skew_metric -setup")
    return lines


def _violation_count_lines() -> list[str]:
    return [
        f'puts "{_SETUP_VIOLATIONS_BEGIN}"',
        "report_check_types -max_delay -violators -format end",
        f'puts "{_SETUP_VIOLATIONS_END}"',
        f'puts "{_HOLD_VIOLATIONS_BEGIN}"',
        "report_check_types -min_delay -violators -format end",
        f'puts "{_HOLD_VIOLATIONS_END}"',
    ]


def _antenna_check_lines() -> list[str]:
    """``"route"`` stage only, run right after `repair_antennas`'s own
    reroute -- reports the post-repair antenna-violation count via
    `check_antennas`'s own stdout summary line ("Found N net violations."),
    isolated the same `puts` marker way :func:`_violation_count_lines` isolates
    the setup/hold blocks."""
    return [
        f'puts "{_ANTENNA_VIOLATIONS_BEGIN}"',
        "check_antennas",
        f'puts "{_ANTENNA_VIOLATIONS_END}"',
    ]


def _stage_script_lines(
    *,
    stage: str,
    checkpoint_in: str | None,
    checkpoint_out: str,
    tech_lef: str,
    cell_lef: str,
    liberty_path: str,
    netlist_path: str,
    hdl_toplevel: str,
    floorplan: dict[str, Any],
    io_spec: dict[str, str] | None,
    macros: list[dict[str, Any]],
    clock_port: str | None,
    clock_period_ns: float | None,
    cell_library: str,
    seed: int,
    output_dir: str,
    route_critical_nets_percentage: int,
    max_antenna_repair_iterations: int,
) -> list[str]:
    if stage == "floorplan":
        # `create_clock` (`_clock_lines`) must come after `link_design` --
        # see that helper's own docstring; `read_liberty` itself is read
        # ahead of the design load, matching ORFS's own `load.tcl` order
        # (liberty -> LEF -> verilog -> link_design -> SDC/clock). Macro
        # LEFs (issue #438) are read alongside the tech/cell LEF, before
        # `read_verilog` -- `link_design` needs every macro's physical view
        # already loaded to resolve the netlist's own macro instances.
        lines = [
            f"read_liberty {liberty_path}",
            f"read_lef {tech_lef}",
            f"read_lef {cell_lef}",
        ]
        lines += [f"read_lef {macro['lef']}" for macro in macros]
        lines += [
            f"read_verilog {netlist_path}",
            f"link_design {hdl_toplevel}",
        ]
        lines += _clock_lines(clock_port, clock_period_ns)
        lines += _floorplan_init_lines(floorplan)
        # `place_macro` fixes each declared hard-macro instance at its
        # caller-given location -- must run after the floorplan's own die/
        # core area is initialized (a location outside the die is invalid)
        # and after `link_design` (the instance must already exist in the
        # linked network). Never OpenROAD's automatic macro placer
        # (`rtl_macro_placer`): a socket-driven macro's location is a
        # caller decision, not something to optimize away.
        lines += [
            f"place_macro -macro_name {macro['instance']} "
            f"-location {{{macro['x_um']} {macro['y_um']}}} "
            f"-orientation {macro['orientation']} -exact"
            for macro in macros
        ]
        lines += ["make_tracks"]
        lines += _metrics_report_lines(include_fmax=False, include_power=False)
        lines += [f"write_db {checkpoint_out}"]
        return lines

    assert checkpoint_in is not None  # every non-floorplan stage has a checkpoint
    # `read_db` already restores a linked network (built by the floorplan
    # stage's own `link_design`), so `create_clock` may safely follow
    # `read_liberty` directly here -- unlike the floorplan stage above.
    lines = [f"read_db {checkpoint_in}", f"read_liberty {liberty_path}"]
    lines += _clock_lines(clock_port, clock_period_ns)

    if stage == "place":
        assert io_spec is not None
        lines += [
            f"place_pins -hor_layers {io_spec['layer_h']} "
            f"-ver_layers {io_spec['layer_v']}",
            f"set_wire_rc -layer {io_spec['layer_v']}",
            f"global_placement -density {_GLOBAL_PLACEMENT_DENSITY} "
            f"-routability_driven -timing_driven -random_seed {seed}",
            "estimate_parasitics -placement",
            "repair_design",
            "repair_timing",
            "detailed_placement",
        ]
    elif stage == "cts":
        assert io_spec is not None
        buf_cell = _CTS_BUFFER_CELLS[cell_library]
        lines += [
            f"set_wire_rc -layer {io_spec['layer_v']}",
            "estimate_parasitics -placement",
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
            "detailed_placement",
        ]
    else:  # stage == "route"
        routing_range = _ROUTING_LAYER_RANGE[cell_library]
        diode_cell = _ANTENNA_DIODE_CELLS[cell_library][0]
        drc_report = os.path.join(output_dir, f"{hdl_toplevel}_route_drc.rpt")
        maze_log = os.path.join(output_dir, f"{hdl_toplevel}_route_maze.log")
        detailed_route_call = (
            f"detailed_route -output_drc {drc_report} -output_maze {maze_log} "
            f"-or_seed {seed}"
        )
        # `-critical_nets_percentage` (issue #939, native-routing survey
        # section 4.1): a real, documented `global_route` flag -- see this
        # module's own docstring, "Timing-driven global routing + bounded
        # antenna-repair iteration" -- that weights the given percentage of
        # worst-slack nets as timing-critical during congestion-removal
        # iterations. `0` (unset, `request.route_critical_nets_percentage`
        # omitted) reproduces `global_route`'s own default and this
        # module's prior generated Tcl byte-for-byte -- the explicit A/B
        # disable path.
        global_route_call = "global_route"
        if route_critical_nets_percentage:
            global_route_call = (
                "global_route "
                f"-critical_nets_percentage {route_critical_nets_percentage}"
            )
        lines += [
            f"set_routing_layers -signal {routing_range}",
            global_route_call,
            detailed_route_call,
        ]
        # Post-route antenna repair (survey section 2.7/3.3): inserting a
        # diode instance on a violating net changes that net's routing,
        # so -- mirroring ORFS's own `flow/scripts/detail_route.tcl`,
        # which re-runs `detailed_route` immediately after
        # `repair_antennas` to route/legalize each new diode instance --
        # this repeats the `repair_antennas`/`detailed_route` pair
        # `max_antenna_repair_iterations` times (issue #939; default `1`,
        # reproducing the original single repair+reroute pass byte-for-
        # byte). `repair_antennas` itself is never called with `-iterations`
        # here -- OpenROAD's own `GlobalRouter.cpp` explicitly warns against
        # `-iterations != 1` once `detailed_route` has already run, exactly
        # this stage's own call pattern (see the module docstring for the
        # full citation) -- so the bounded multi-pass behaviour is built at
        # the flow level instead, mirroring ORFS's own opt-in
        # `MAX_REPAIR_ANTENNAS_ITER_DRT` loop shape (unset, and therefore
        # inactive/single-pass, in ORFS's own default flow).
        # `check_antennas` then reports the post-repair violation count
        # unconditionally, exactly as ORFS's own flow does.
        for _ in range(max_antenna_repair_iterations):
            lines += [
                f"repair_antennas {diode_cell}",
                detailed_route_call,
            ]
        lines += _antenna_check_lines()
        lines += ["estimate_parasitics -global_routing"]

    lines += _metrics_report_lines(
        include_fmax=True,
        include_power=True,
        # A clock tree only exists from `"cts"` onward -- `"place"` runs
        # before `clock_tree_synthesis`, so `report_clock_skew_metric`
        # there would report on an ideal (zero-latency) clock, not a real
        # tree (#783).
        include_clock_skew=stage in ("cts", "route"),
    )
    lines += _violation_count_lines()

    if stage == "route":
        def_path = os.path.join(output_dir, f"{hdl_toplevel}.def")
        verilog_path = os.path.join(output_dir, f"{hdl_toplevel}.v")
        # `write_verilog` (issue #996) -- the *as-built* gate-level netlist,
        # written from the same linked design `write_def` above just dumped
        # the geometry of, so the two artifacts describe one and the same
        # design state. Without it the only netlist a caller can build an LVS
        # reference from is `klt synthesize`'s own **pre-CTS** output, which
        # is guaranteed to diverge from the routed layout by exactly the
        # cells this command's own `clock_tree_synthesis`/`repair_timing`/
        # `repair_design`/`repair_antennas` calls inserted or resized -- a
        # divergence `klt lvs` has no way to attribute (one real run: 40 of
        # ~720 instances, all ordinary CTS/resizer/diode output).
        #
        # `write_verilog` is a real, top-level OpenROAD Tcl command from the
        # always-loaded `dbSta` module -- `src/dbSta/src/dbReadVerilog.tcl`
        # declares `write_verilog {[-sort] [-include_pwr_gnd] [-remove_cells
        # cells] filename}` and forwards to `sta::write_verilog_cmd`
        # (`src/dbSta/src/dbSta.i`), reading the OpenROAD network rather than
        # any Yosys state (verified against The-OpenROAD-Project/OpenROAD
        # `master`, fetched 2026-08-14; ORFS calls the same command in its
        # own `flow/scripts/final_outputs.tcl` to write `6_final.v`). It
        # needs only a linked design, which every non-floorplan stage has via
        # `read_db`. If a future OpenROAD build were to drop it, the run
        # fails loudly (nonzero exit -> `PlaceAndRouteError`), never silently
        # skipping the artifact.
        #
        # Flags deliberately **not** passed:
        # - `-include_pwr_gnd`: omitted, matching ORFS's own `6_final.v` and
        #   keeping this artifact directly diffable against `klt
        #   synthesize`'s netlist (which likewise carries no VPWR/VGND
        #   connections -- power comes from the LEF/DEF grid, not the
        #   netlist). That diff is the issue's own motivating workflow.
        # - `-remove_cells [find_physical_only_masters]`: ORFS strips
        #   fill/tap/endcap cells there, but this flow never inserts any
        #   (no `filler_placement`, no `tapcell` -- see the stage branches
        #   above), so the flag would be a no-op that only risks dropping a
        #   real cell. Antenna diodes (`repair_antennas`) are genuine
        #   instances present in the routed GDS and are kept.
        # - `-sort`: OpenROAD warns it is ignored (`utl::warn STA 2065`).
        #
        # Written only at `"route"`, not at `"cts"` (issue #996's own open
        # design question, resolved here): the artifact exists to be the LVS
        # reference for a *routed* GDS, and `"route"` is the only stage that
        # produces one (`def_path`/`gds_path` are `null` before it). A
        # cts-stage netlist would be a snapshot that no shippable layout
        # corresponds to -- it predates `repair_antennas`'s own diode
        # insertions -- and would need a second response field with no
        # physical artifact to pair with. `verilog_path` therefore follows
        # `def_path`/`gds_path` exactly: populated at `"route"`, `null`
        # before it.
        lines += [f"write_def {def_path}", f"write_verilog {verilog_path}"]
    elif stage == "place":
        # A placement-only DEF, written as a side artifact (never referenced
        # by `run_place_and_route`'s own return value) alongside the
        # `write_db` checkpoint below. This exists purely so an out-of-band
        # caller -- the FLUTE/RUDY-family congestion pre-check
        # (`klayout_tools.congestion`, issue #785, Epic #700 Phase 1 §3.6)
        # -- can read real post-placement cell/pin geometry via
        # `klayout.db`'s DEF parser (mirroring `_merge_def_to_gds`'s own use
        # of it) without needing the far more expensive `route` stage to
        # have run first. Deliberately **not** added to
        # `run_place_and_route`'s response dict or `PlaceAndRouteResult`:
        # this is an internal artifact, not part of the public
        # request/response contract (issue #785's own explicit acceptance
        # criterion) -- callers that need it locate it at this same
        # deterministic path (`<output_dir>/<hdl_toplevel>.place.def`), the
        # same way the DSE-loop pre-check itself would.
        place_def_path = os.path.join(output_dir, f"{hdl_toplevel}.place.def")
        lines += [f"write_def {place_def_path}"]

    lines += [f"write_db {checkpoint_out}"]
    return lines


def _corner_sweep_script_lines(
    *,
    checkpoint_in: str,
    corners: list[dict[str, str]],
    io_spec: dict[str, str],
    clock_port: str | None,
    clock_period_ns: float | None,
) -> list[str]:
    """Tcl for the post-route multi-corner setup/hold sweep (issue #949,
    ``docs/design/post-route-sta-survey.md`` section 4.2) -- a **second**,
    lightweight OpenROAD invocation run *after* the ``"route"`` stage's own
    script (:func:`_stage_script_lines`) has already written its checkpoint,
    deliberately never folded into that script's own OpenSTA session.

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
    (:func:`_stage_script_lines`'s own docstring/comments). Parasitics are
    re-estimated (``estimate_parasitics -global_routing``) since
    ``write_db``/``read_db`` does not carry Sta's own parasitics state
    across the process boundary any more than it carries SDC/linked-library
    state (same docstring) -- this reproduces the *same* global-routing RC
    estimate the route stage's own existing ``worst_slack_ns`` is already
    based on, not a fidelity regression or a second, differently-sourced
    number.

    ``report_worst_slack_metric -setup``/``-hold`` (run here for the first
    and only time in this session, so no scoping concern applies) writes
    this invocation's own ``-metrics`` dump's ``timing__setup__ws``/
    ``timing__hold__ws`` keys -- automatically the worst value across every
    corner :func:`klayout_tools.pdk.list_lib_corners` enumerated (confirmed
    live: setup slack worst-cases at the slowest loaded corner, hold slack
    at the fastest, exactly the industrial "setup at slow PVT, hold at fast
    PVT" convention this survey's section 3.3 describes -- no manual
    slow/fast corner classification needed in Python).
    """
    lines = [f"read_db {checkpoint_in}"]
    lines += ["define_corners " + " ".join(corner["name"] for corner in corners)]
    lines += [
        f"read_liberty -corner {corner['name']} {corner['path']}" for corner in corners
    ]
    lines += _clock_lines(clock_port, clock_period_ns)
    lines += [
        f"set_wire_rc -layer {io_spec['layer_v']}",
        "estimate_parasitics -global_routing",
        "report_worst_slack_metric -setup",
        "report_worst_slack_metric -hold",
    ]
    return lines


# --------------------------------------------------------------------------- #
# Post-route SPEF STA (`request.post_route_spef`, issue #948)
# --------------------------------------------------------------------------- #


def _tcl_net_list(names: Iterable[str]) -> str:
    """Render ``names`` as a brace-quoted Tcl list body (each name wrapped
    ``{...}``, whitespace-joined) -- the standard Tcl idiom for embedding
    arbitrary literal strings in a generated script without word-splitting
    or ``$``/``[...]`` substitution inside each token. Net names this module
    ever embeds this way come from ``klt extract``'s own SPICE-safe naming
    (:func:`klayout_tools.extract.spice_safe_net_name`), which does not
    introduce a literal ``}`` -- an unbalanced brace in a caller-supplied
    name is a known, unguarded edge case, consistent with this module's
    existing "not independently verified live" posture for newly-added Tcl
    surface (see this module's own docstring)."""
    return " ".join("{" + name + "}" for name in names)


def _spef_sta_script_lines(
    *,
    checkpoint_in: str,
    liberty_path: str,
    clock_port: str | None,
    clock_period_ns: float | None,
    spef_path: str,
    net_names: list[str],
    sdf_path: str | None = None,
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
    lines = [f"read_db {checkpoint_in}", f"read_liberty {liberty_path}"]
    lines += _clock_lines(clock_port, clock_period_ns)
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


def _count_spef_nets_annotated(stdout: str) -> tuple[int, int, int, int] | None:
    """``(nets_annotated, nets_total, design_nets_annotated,
    design_nets_total)`` parsed from :func:`_spef_sta_script_lines`'s own
    ``===KLT_SPEF_NET_CHECK_BEGIN===``/``===END===``-delimited stdout block,
    or ``None`` when the markers aren't found (defensive; should not happen
    for a successful run) -- same marker-scrape convention as
    :func:`_count_antenna_violations`."""
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
    ``klayout.db``'s LEF/DEF reader (:func:`_merge_def_to_gds`) for every
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

    Raises :class:`PlaceAndRouteError` for an unsupported ``cell_library``
    (no known `klt extract --deck`, see
    :data:`_EXTRACT_DECK_FOR_CELL_LIBRARY`), a `klt extract --parasitics`
    failure on the merged GDS, or a failed second `openroad` invocation --
    the same "fail loud, never silently skip" posture this module's other
    per-cell-library reference tables already follow (see
    :data:`_CTS_BUFFER_CELLS` and its call sites).
    """
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
    )
    _write_script(script_path, lines)

    completed = _run_openroad(script_path, metrics_path)
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
    nets_check = _count_spef_nets_annotated(completed.stdout)
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
        "setup_violation_count": setup_violation_count,
        "hold_violation_count": hold_violation_count,
        "nets_annotated": nets_annotated,
        "nets_total": nets_total,
        "design_nets_annotated": design_nets_annotated,
        "design_nets_total": design_nets_total,
        "annotation_complete": annotation_complete,
        "annotation_warning": annotation_warning,
    }


# --------------------------------------------------------------------------- #
# OpenROAD subprocess invocation
# --------------------------------------------------------------------------- #


def _run_openroad(script_path: str, metrics_path: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["openroad", "-no_init", "-exit", "-metrics", metrics_path, script_path],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise PlaceAndRouteError(f"could not launch openroad: {exc}") from exc


def _run_corner_sweep(
    *,
    checkpoint_in: str,
    corners: list[dict[str, str]],
    io_spec: dict[str, str],
    clock_port: str | None,
    clock_period_ns: float | None,
    output_dir: str,
    hdl_toplevel: str,
) -> tuple[float | None, float | None]:
    """Run the post-route multi-corner setup/hold sweep (issue #949) as a
    second OpenROAD invocation, after the ``"route"`` stage's own script has
    already written ``checkpoint_in`` -- see :func:`_corner_sweep_script_lines`
    for why this cannot be folded into that script's own session.

    Returns ``(worst_setup_slack_ns, worst_hold_slack_ns)``, each rounded to
    5 decimal places (matching :func:`_extract_stage_metrics`'s own
    ``worst_slack_ns`` rounding) or ``None`` if that invocation's own
    ``-metrics`` dump didn't populate the corresponding key. ``(None, None)``
    when ``corners`` is empty -- should not happen in practice (the resolved
    ``liberty_path`` a run already reached ``"route"`` with implies at least
    that one corner's own ``.lib`` file exists under the same ``lib/``
    directory :func:`~klayout_tools.pdk.list_lib_corners` walks), but
    degrading to ``null`` rather than raising on an empty sweep target list
    keeps this helper total.

    Raises :class:`PlaceAndRouteError` on an OpenROAD engine failure, exactly
    like every other stage invocation in this module -- silently degrading
    these fields to ``null`` on a broken sweep would undermine the very
    multi-corner evidence claim they exist to support (T1 checklist item 5,
    ``docs/design-evidence-tiers.md``).
    """
    if not corners:
        return None, None

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
    )
    _write_script(script_path, lines)

    completed = _run_openroad(script_path, metrics_path)
    if completed.returncode != 0:
        raise PlaceAndRouteError(
            _engine_error_message("route (corner sweep)", completed)
        )

    metrics = _read_metrics(metrics_path, "route (corner sweep)")
    worst_setup = metrics.get("timing__setup__ws")
    worst_hold = metrics.get("timing__hold__ws")
    return (
        round(worst_setup, 5) if worst_setup is not None else None,
        round(worst_hold, 5) if worst_hold is not None else None,
    )


def _engine_error_message(stage: str, completed: subprocess.CompletedProcess) -> str:
    """Build an actionable error message from a failed per-stage OpenROAD
    run -- prefers the last ``[ERROR ...]``/``Error:`` line OpenROAD itself
    printed, on either stream (mirrors ``synthesize.py``'s
    ``_synthesis_error_message``).

    ``DRT-0305`` (a constant-tie net reaching TritonRoute) is additionally
    *diagnosed* rather than passed through -- see
    :func:`_constant_tie_diagnosis`.
    """
    diagnosis = _constant_tie_diagnosis(completed)
    if diagnosis is not None:
        error_line, hint = diagnosis
        return f"openroad '{stage}' stage failed: {error_line} -- {hint}"

    for stream in (completed.stderr or "", completed.stdout or ""):
        error_lines = [
            line.strip()
            for line in stream.splitlines()
            if "[ERROR" in line or line.strip().startswith("Error:")
        ]
        if error_lines:
            return f"openroad '{stage}' stage failed: {error_lines[-1]}"

    tail_source = (completed.stderr or completed.stdout or "").strip().splitlines()
    snippet = " ".join(tail_source[-3:]) if tail_source else "no output captured"
    return (
        f"openroad '{stage}' stage exited with code {completed.returncode}: {snippet}"
    )


def _constant_tie_diagnosis(
    completed: subprocess.CompletedProcess,
) -> tuple[str, str] | None:
    """``(error_line, hint)`` when a failed run hit ``DRT-0305``, else
    ``None`` (issue #854).

    ``DRT-0305`` is the one OpenROAD error this module translates rather
    than echoes, because its default surface is actively unhelpful: the
    informative line goes to **stdout** (utl's logger) while the
    uninformative Tcl summary -- ``Error: pnr_<top>_route.tcl, 6 DRT-0305``
    -- goes to **stderr**, which :func:`_engine_error_message`'s stderr-first
    preference would otherwise pick. So a caller saw a script line number
    and nothing else.

    What it actually means: OpenSTA's Verilog reader materialises one net
    per constant *value* it reads in a netlist (conventionally ``zero_`` and
    ``one_``), OpenROAD types those nets ``GROUND``/``POWER``, and
    TritonRoute refuses to route a power/ground-typed net as signal. The fix
    is upstream, in synthesis: map constants onto real tie cells so no bare
    constant literal ever reaches place-and-route -- which ``klt synthesize``
    now does for every ``cell_library`` in its own tie-cell table (#854).
    """
    combined = f"{completed.stdout or ''}\n{completed.stderr or ''}"
    match = _DRT_CONSTANT_NET_RE.search(combined)
    if match is None:
        return None
    net, sig_type = match.group("net"), match.group("sig_type")
    return (
        match.group(0).strip(),
        f"net '{net}' is a constant tie, not a real signal: a bare 1'b0/1'b1 "
        f"literal in the netlist becomes a net OpenROAD types {sig_type}, and "
        f"TritonRoute will not route one. Re-synthesize the netlist so its "
        f"constants are driven by real tie cells -- `klt synthesize` does this "
        f"via yosys's `hilomap` pass for every standard-cell library in its "
        f"tie-cell table -- or hand-edit the netlist to instantiate tie-high/"
        f"tie-low cells before place-and-route.",
    )


def _openroad_version() -> str | None:
    """The resolved OpenROAD build string, or ``None`` if unresolvable --
    never raises.

    ``openroad -version`` prints a **bare** version token on its own stdout
    line (e.g. ``26Q3-771-g7cfb2105c9``) -- confirmed live for issue #425's
    own worked example against a real OpenROAD build, distinct from the
    ``OpenROAD <version>`` banner a no-flag/`-no_init` script invocation
    prints before running. The banner form is matched first (defensively,
    in case a future build changes `-version`'s own output shape); the bare
    first-token form is the fallback that matches today's real behavior.
    """
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


def _count_violations(stdout: str, begin: str, end: str) -> int:
    """Count ``"(VIOLATED)"`` lines between ``begin``/``end`` markers in a
    stage's captured stdout -- OpenROAD has no ``*_metric`` proc for
    setup/hold *violation counts* (only the scalar WNS/TNS), so this is the
    documented ``report_*`` stdout-scrape fallback (contract spike section
    5's build/wrap section). Returns ``0`` when the markers aren't found
    (defensive; should not happen for a successful run) or when the report
    found no violating paths."""
    try:
        start_idx = stdout.index(begin) + len(begin)
        stop_idx = stdout.index(end, start_idx)
    except ValueError:
        return 0
    return stdout[start_idx:stop_idx].count("(VIOLATED)")


def _count_antenna_violations(stdout: str) -> int | None:
    """Parse the post-repair antenna-*violating-net* count from
    `check_antennas`'s own stdout, isolated between the
    ``_ANTENNA_VIOLATIONS_BEGIN``/``_END`` markers -- same marker-scrape
    convention as :func:`_count_violations`, but `check_antennas` prints one
    summary count line (``"Found N net violations."``, verified live against
    a real ``openroad/orfs:latest`` run) rather than one line per violation,
    so this parses that count directly instead of counting lines. Returns
    ``None`` (never ``0`` defensively) when the markers or the expected
    message aren't found -- should not happen for a successful ``"route"``
    stage run, and keeps a genuinely missing signal distinguishable from a
    confirmed-zero violation count."""
    try:
        start_idx = stdout.index(_ANTENNA_VIOLATIONS_BEGIN) + len(
            _ANTENNA_VIOLATIONS_BEGIN
        )
        stop_idx = stdout.index(_ANTENNA_VIOLATIONS_END, start_idx)
    except ValueError:
        return None
    match = _ANTENNA_VIOLATION_COUNT_RE.search(stdout[start_idx:stop_idx])
    return int(match.group(1)) if match else None


def _count_route_drc_violations(drc_report_path: str) -> int | None:
    """Count TritonRoute's own `detailed_route -output_drc <rpt>` violation
    entries -- each violation in the report begins with a literal
    ``"violation type: "`` header line (see
    :data:`_ROUTE_DRC_VIOLATION_TYPE_LINE`; confirmed live via `strings`
    against a real `openroad/orfs:latest` build's `openroad` binary, which
    embeds this exact literal both to *write* the report and to *re-parse*
    it internally), so counting occurrences of that line is exact -- one
    per violation, never a partial match on unrelated report text (`comment:
    `/`bbox = (...)`/`Layer: ...` detail lines never themselves start with
    `"violation type: "`).

    A 0-byte report -- confirmed live for a real, DRC-clean `detailed_route`
    run on this repo's own `gcd` corpus fixture (issue #938) -- means zero
    violations, correctly returned as ``0``, not ``None``. Returns ``None``
    (never ``0`` defensively) only when the report file itself cannot be
    read -- should not happen for a successful `"route"` stage run
    (`detailed_route` always writes its `-output_drc` target, even when
    empty), and keeps a genuinely missing signal distinguishable from a
    confirmed-zero violation count, mirroring
    :func:`_count_antenna_violations`."""
    try:
        with open(drc_report_path, encoding="utf-8") as handle:
            content = handle.read()
    except OSError:
        return None
    return content.count(_ROUTE_DRC_VIOLATION_TYPE_LINE)


def _read_metrics(metrics_path: str, stage: str) -> dict[str, Any]:
    if not os.path.isfile(metrics_path):
        raise PlaceAndRouteError(
            f"openroad exited successfully but did not produce the expected "
            f"'{stage}' stage metrics file '{metrics_path}'"
        )
    try:
        with open(metrics_path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, UnicodeDecodeError) as exc:
        raise PlaceAndRouteError(
            f"could not read '{stage}' stage metrics '{metrics_path}': {exc}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise PlaceAndRouteError(
            f"'{stage}' stage metrics '{metrics_path}' is not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise PlaceAndRouteError(
            f"'{stage}' stage metrics '{metrics_path}' must contain a JSON object"
        )
    return data


def _extract_stage_metrics(
    stage: str,
    metrics: dict[str, Any],
    setup_violation_count: int | None,
    hold_violation_count: int | None,
    antenna_violation_count: int | None = None,
    route_drc_violation_count: int | None = None,
    worst_setup_slack_ns: float | None = None,
    worst_hold_slack_ns: float | None = None,
) -> dict[str, Any]:
    """Map one stage's raw OpenROAD ``-metrics`` JSON dump onto this
    contract's field names -- see this module's docstring
    "``-metrics <file>.json`` confirmed working end-to-end" for the exact
    key mapping, verified live. Only includes keys that stage's own report
    calls actually populate (contract spike section 5: "each with whatever
    subset of the top-level metric fields that stage's own OpenROAD reports
    provide")."""
    entry: dict[str, Any] = {"name": stage}

    die_area = metrics.get("design__die__area")
    if die_area is not None:
        entry["die_area_um2"] = round(die_area, 4)
    core_area = metrics.get("design__core__area")
    if core_area is not None:
        entry["core_area_um2"] = round(core_area, 4)
    utilization = metrics.get("design__instance__utilization")
    if utilization is not None:
        entry["utilization_pct"] = round(utilization * 100, 4)
    worst_slack = metrics.get("timing__setup__ws")
    if worst_slack is not None:
        entry["worst_slack_ns"] = round(worst_slack, 5)
    tns = metrics.get("timing__setup__tns")
    if tns is not None:
        entry["total_negative_slack_ns"] = round(tns, 5)

    if stage != "floorplan":
        wirelength = metrics.get(
            "route__wirelength", metrics.get("route__wirelength__estimated")
        )
        if wirelength is not None:
            entry["wirelength_um"] = round(wirelength, 4)
        fmax_hz = metrics.get("timing__fmax")
        if fmax_hz is not None:
            entry["fmax_mhz"] = round(fmax_hz / 1e6, 4)
        power_w = metrics.get("power__total")
        if power_w is not None:
            entry["estimated_power_mw"] = round(power_w * 1000, 4)
        # Only the `"cts"`/`"route"` stages' own generated Tcl runs
        # `report_clock_skew_metric` (see `_stage_script_lines`), so this
        # key is simply absent -- never present-but-zero -- on the other
        # stages; `.get` degrades that to `None` the same way every other
        # field above does (#783).
        clock_skew = metrics.get("clock__skew__setup")
        if clock_skew is not None:
            entry["clock_skew_ns"] = round(clock_skew, 5)
        if setup_violation_count is not None:
            entry["setup_violation_count"] = setup_violation_count
        if hold_violation_count is not None:
            entry["hold_violation_count"] = hold_violation_count
        if antenna_violation_count is not None:
            entry["antenna_violation_count"] = antenna_violation_count
        if route_drc_violation_count is not None:
            entry["route_drc_violation_count"] = route_drc_violation_count
        # Issue #949: the corner-swept worst-case setup/hold slack, from the
        # post-route corner sweep (`_run_corner_sweep`) -- absent (never
        # present-but-null) on every stage but `"route"`, the same
        # `.get`-degrades-to-absent convention every other route-only field
        # above follows.
        if worst_setup_slack_ns is not None:
            entry["worst_setup_slack_ns"] = worst_setup_slack_ns
        if worst_hold_slack_ns is not None:
            entry["worst_hold_slack_ns"] = worst_hold_slack_ns

    return entry


# --------------------------------------------------------------------------- #
# DEF -> GDS merge (ported from def2stream.py onto klayout.db, in-process)
# --------------------------------------------------------------------------- #


def _merge_def_to_gds(
    *,
    def_path: str,
    tech_lef: str,
    cell_lef: str,
    pdk_info: dict[str, Any],
    cell_library: str,
    hdl_toplevel: str,
    macros: list[dict[str, Any]],
    out_path: str,
) -> dict[str, Any]:
    """Merge the routed DEF with the resolved standard-cell GDS view into a
    single GDS -- ported directly from ORFS's ``def2stream.py`` (survey
    section 4) onto this repo's ``klayout.db`` package, in-process. Never
    shells out to a ``klayout`` binary (matching ``klt drc``'s existing
    in-process ``pya`` posture).

    ``macros`` (issue #438) additionally merges each hard macro's own GDS
    view (when its request entry declared one via ``gds``) alongside the
    standard-cell GDS view, keyed by the macro's own LEF ``MACRO`` name
    (``macro["cell_name"]``, resolved once at request-validation time via
    :func:`klayout_tools.lef_header.read_lef_header` -- the DEF reader
    creates one cell per distinct macro reference, named after its LEF
    ``MACRO``, exactly like a standard cell). A macro with no declared
    ``gds`` is exempted from the "missing (unmatched) cell" check below
    instead -- an abstract-only macro instance (this issue's own DEF-level
    placement/obstruction verification does not need a real GDS view) is
    expected to stay empty, not an error.

    Returns ``{"path": <str | None>, "resolution": <str>}`` describing the
    :func:`_resolve_layer_map` result actually applied (or not) to this
    merge, so the caller can surface it in the response envelope's
    ``layer_map`` field (issue #1029) -- a caller has no other way to tell
    whether the merged GDS's routing shapes got a guaranteed-matching
    layer/datatype assignment.
    """
    import klayout.db as kdb

    cell_gds = _resolve_gds_view(pdk_info, cell_library)
    layer_map_path, layer_map_resolution = _resolve_layer_map(pdk_info)

    opts = kdb.LoadLayoutOptions()
    lefdef_config = opts.lefdef_config
    lefdef_config.lef_files = [tech_lef, cell_lef, *(macro["lef"] for macro in macros)]
    if layer_map_path is not None:
        lefdef_config.map_file = layer_map_path

    # Issue #1032: KLayout's DEF reader silently proceeds when the target
    # layout DBU it is configured for does not match the DEF file's own
    # declared `UNITS DISTANCE MICRONS` value, producing wrong/lossy
    # via-cut geometry as a side effect (only a `Warning:` line, never a
    # raised error). Left unset, `lefdef_config.dbu` inherits KLayout's
    # compiled-in default (0.001, i.e. `DATABASE MICRONS 1000`), which
    # happens to match sky130 but is wrong for any PDK whose own tech LEF
    # declares a different `DATABASE MICRONS` value (e.g. gf180mcu's 2000).
    # Resolve the tech LEF's own declared value and set the reader's target
    # DBU to match; when the tech LEF doesn't declare one (or the
    # regex-based parser can't extract it), fall back to KLayout's existing
    # default DBU behavior -- `read_lef_header` never raises on malformed
    # input, so `database_microns is None` is a normal, expected case here.
    database_microns = read_lef_header(tech_lef)["database_microns"]
    if database_microns is not None:
        lefdef_config.dbu = 1.0 / database_microns

    main_layout = kdb.Layout()
    try:
        main_layout.read(def_path, opts)
    except Exception as exc:  # klayout raises RuntimeError for bad/unknown streams
        raise PlaceAndRouteError(
            f"could not read DEF '{def_path}' for GDS merge: {exc}"
        ) from exc

    top_cell = main_layout.cell(hdl_toplevel)
    if top_cell is None:
        raise PlaceAndRouteError(
            f"DEF '{def_path}' does not define top cell '{hdl_toplevel}'"
        )
    top_cell_index = top_cell.cell_index()

    # Clear every non-top cell except LEF-via cells (KLayout prepends
    # "VIA_" when reading a DEF that instantiates a LEF via) and DEF fill
    # cells -- matching def2stream.py's own orphan-cell handling exactly.
    for cell in main_layout.each_cell():
        if cell.cell_index() != top_cell_index:
            if not cell.name.startswith("VIA_") and not cell.name.endswith("_DEF_FILL"):
                cell.clear()

    try:
        main_layout.read(cell_gds)
    except Exception as exc:
        raise PlaceAndRouteError(
            f"could not read standard-cell GDS view '{cell_gds}' for GDS merge: {exc}"
        ) from exc

    for macro in macros:
        if macro["gds"] is None:
            continue
        try:
            main_layout.read(macro["gds"])
        except Exception as exc:
            raise PlaceAndRouteError(
                f"could not read macro GDS view '{macro['gds']}' for GDS merge: {exc}"
            ) from exc

    top_only = kdb.Layout()
    top_only.dbu = main_layout.dbu
    top = top_only.create_cell(hdl_toplevel)
    top.copy_tree(main_layout.cell(hdl_toplevel))

    # Abstract-only macro instances (no `gds` declared) are expected to stay
    # empty -- exempt their own LEF MACRO cell name from the missing-cell
    # check, mirroring the VIA_/_DEF_FILL exemption above.
    abstract_only_macro_cells = {
        macro["cell_name"] for macro in macros if macro["gds"] is None
    }
    missing = sorted(
        cell.name
        for cell in top_only.each_cell()
        if cell.is_empty() and cell.name not in abstract_only_macro_cells
    )
    if missing:
        raise PlaceAndRouteError(
            "DEF/GDS merge produced empty (unmatched) cells: "
            + ", ".join(missing[:10])
            + (f" (+{len(missing) - 10} more)" if len(missing) > 10 else "")
        )

    orphans = sorted(
        cell.name
        for cell in top_only.each_cell()
        if cell.name != hdl_toplevel and cell.parent_cells() == 0
    )
    if orphans:
        raise PlaceAndRouteError(
            "DEF/GDS merge produced orphan cells: " + ", ".join(orphans[:10])
        )

    try:
        top_only.write(out_path)
    except OSError as exc:
        raise PlaceAndRouteError(
            f"could not write merged GDS '{out_path}': {exc}"
        ) from exc

    return {"path": layer_map_path, "resolution": layer_map_resolution}


__all__ = [
    "SCHEMA_VERSION",
    "SUPPORTED_ENGINES",
    "STAGE_ORDER",
    "PlaceAndRouteError",
    "load_request",
    "run_place_and_route",
]

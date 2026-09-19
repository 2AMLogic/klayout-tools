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
-> ``timing__setup__ws`` (``-hold`` -> ``timing__hold__ws``, issue #1826),
``report_tns_metric`` -> ``timing__setup__tns``,
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
spike's build/wrap section authorises. Issue #1709's post-route
max-transition/max-capacitance verdict
(:func:`_design_rule_check_lines`, ``max_transition_violation_count``/
``max_capacitance_violation_count``) reuses that same fallback for the same
reason.

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
synthesize`` already does (:func:`_resolve_liberty`, a thin wrapper around
the shared :func:`klayout_tools.pdk.resolve_liberty_for_cell_library`
resolution both modules call, issue #1652 -- PDK-resolution helpers are the
one exception to this repo's otherwise-self-contained verb modules, since
letting them drift independently is a live-bug risk, not a design feature),
and, like that module, accepts the CLI's
own ``--pdk``/``--pdk-root`` flags (mirroring ``klt extract``'s identical
pair) to pin a specific installed PDK variant/root rather than always
falling back to ``find_pdk()``'s own default search order. The tech +
merged-cell LEF pair resolves via the new :func:`klayout_tools.pdk.lef_files`
resolver (issue #397/#425 -- ``_ASSET_LAYOUT`` never carried a ``lef`` key),
pinned to whatever variant/root :func:`_resolve_liberty` already resolved.
The optional ``pdk.interconnect_corner`` (issue #1100, one of ``"min"``/
``"nom"``/``"max"``, default ``"nom"``) independently selects which tech-LEF
*parasitic* corner ``lef_files`` resolves -- a wire-model choice orthogonal
to the liberty (device) corner ``pdk.corner`` names, echoed back as its own
top-level ``interconnect_corner`` response field (distinct from ``deck_name``,
which encodes only the liberty corner).

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
contract's own IO-ring/footprint exclusion): metal fill (density fill, not
gap-filler cells -- see "Power delivery" below for those) and a
``DONT_USE_CELLS``-style cell exclusion list -- neither is part of the
request/response contract this phase implements, and each can be added later
as an additive request field without a contract-shape change. Tapcell
insertion and power-grid generation (PDN) were also originally scoped out
here; see "Power delivery" below for the additive field that closes that gap
(issue #1091), exactly per this same note's own precedent (the "Hard-macro
placement" section above closed the identical kind of v1 exclusion for
macros).

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

Netlist pre-flight: ``signed`` qualifiers and bare-``x`` constants (issue #1973)
---------------------------------------------------------------------------------

Two structural-Verilog constructs reach this command routinely and fail it
deterministically, each several stages into a real OpenROAD run and each
with a diagnostic that points at a generated file the caller never wrote:

- A ``signed`` port/wire qualifier (``output signed [15:0] sample;``).
  Yosys's ``write_verilog`` preserves it and offers no flag to suppress it;
  OpenSTA's Verilog reader -- the very first thing the ``"floorplan"`` stage
  runs -- rejects the keyword outright with a bare ``[ERROR STA-0171]
  <file> line N, syntax error``.
- A bare ``x``-valued constant (``assign \\foo$func$..o = 5'hxx;``, most
  often a Verilog ``function``'s own dangling argument wire). OpenSTA reads
  it as a constant and materialises an empty ``GROUND``-typed net from it
  (conventionally ``zero_``), which TritonRoute then refuses with
  ``DRT-0305`` -- **after** floorplan, placement and CTS have all already
  succeeded, so it costs a full route attempt to discover.

:func:`_reject_unsupported_netlist_constructs` catches both **before** any
OpenROAD subprocess runs, raising a :class:`PlaceAndRouteError` that names
the construct, the 1-based netlist line number, and the offending source
line -- and, for the ``x`` case, says what to do about it. The scan is
comment-aware (a ``signed`` inside Yosys's own header banner, or an
``x``-constant quoted in a comment, is not a construct) and deliberately
narrow: the ``signed`` pattern is anchored to a preceding declaration
keyword so an expression-level ``$signed(...)`` cast -- valid everywhere
downstream, and the recommended RTL replacement for a ``signed`` port -- is
never flagged.

This is a **safety net for netlists that did not come from `klt
synthesize`** (hand-written, third-party, or post-edited). ``klt
synthesize`` itself now prevents both constructs at the source: it strips
``signed`` declarations after ``write_verilog``, and runs ``setundef -zero``
ahead of its existing ``hilomap`` pass so every ``x`` bit is resolved to a
concrete ``0`` and then tie-cell-mapped exactly like an ordinary
``1'b0``/``1'b1`` literal (issue #854's own fix, which alone covered only
``0``/``1``).

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
  ``0``-``8``, default ``1``). Zero skips repair; the default keeps a
  single repair/reroute pass; a higher value repeats the ``repair_antennas``/
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

Power delivery (``request.power``, issue #1091)
--------------------------------------------------

Before this field existed, this module's generated Tcl never called
``global_connect``/``pdngen`` and never inserted tapcells or filler cells --
the routed DEF it wrote had no ``SPECIALNETS`` section at all, every standard
cell's ``VDD``/``VSS`` LEF pin belonged to no net, and cell rows were
discontinuous wherever placement left a gap. That is a "signals route"
result, not a "block is implemented" one -- DRC (well/substrate ties, rail
continuity), LVS (power nets in the reference netlist), and any real handoff
all need power delivery first. The optional ``request.power`` block closes
this gap: net names for the power/ground rails, plus the PDN strap
layers/pitch/width, drive ``tapcell``/``add_global_connection``/
``global_connect``/``pdngen`` at the end of the ``"floorplan"`` stage (right
after ``place_macro``/``make_tracks``, before that stage's own ``write_db``)
and ``filler_placement``/``global_connect`` at the end of the ``"route"``
stage (right after the antenna-repair loop, before ``write_def``).

This insertion ordering is not invented -- it mirrors OpenROAD-flow-scripts'
own stage sequence exactly, confirmed live against real ORFS flow scripts
fetched 2026-08-17 from ``The-OpenROAD-Project/OpenROAD-flow-scripts``@
``master``: ``flow/scripts/tapcell.tcl`` runs immediately after
``macro_place.tcl`` and checkpoints before ``flow/scripts/pdn.tcl``, which
itself checkpoints right before that flow's own global-placement stage
begins -- exactly this module's own ``"floorplan"``->``"place"`` checkpoint
boundary. ``flow/scripts/fillcell.tcl`` likewise runs after
``detail_route.tcl`` (post antenna-repair), before the flow's own
``final_connect.tcl`` -- which itself re-runs a plain ``global_connect`` with
no new ``add_global_connection`` rules, specifically because
``filler_placement``/``repair_antennas`` can add instances whose PG pins
still need wiring to the connection rules already registered; this module's
own ``"route"``-stage ``global_connect`` call after ``filler_placement``
mirrors that same need.

``tapcell``/``add_global_connection``/``global_connect``/``pdngen``/
``filler_placement`` are all real, top-level OpenROAD Tcl commands --
verified live (``openroad -no_init``, ``help <command>``, this repo's own
sandboxed OpenROAD 26Q3-1278-g4421880472 install, 2026-08-17), not guessed
from documentation. The per-library masters/pin-patterns/distances/fill-cell
lists themselves live in :data:`_TAPCELL_CELLS`/:data:`_POWER_PIN_PATTERNS`/
:data:`_FILLER_CELLS` -- the same "not derivable from the resolved PDK
install itself" table convention :data:`_CTS_BUFFER_CELLS`/
:data:`_ROUTING_LAYER_RANGE`/:data:`_ANTENNA_DIODE_CELLS` already established
(see each table's own docstring for its exact ORFS source citation). A
``cell_library`` with no entry in any of the three new tables fails with a
clear error as soon as a ``request.power``-bearing run reaches the stage
that needs it, exactly like the existing CTS/routing-layer/antenna-diode
checks.

**Scope deliberately excluded from this v1 of ``request.power``:**
macro-specific PDN grids (``define_pdn_grid -macro``, with their own
halo/orientation config) -- a design with hard macros needs a caller-supplied
macro halo/grid spec this field does not yet expose, so ``pdngen`` here
builds only the flat standard-cell grid (:func:`_power_delivery_lines`).
Real per-instance tapcell/filler *placement counts* are also not reported in
the additive ``power`` response field below (only which cell masters/net
names this run was configured with) -- OpenROAD reports these only via
``report_design_area``'s free-text summary or a custom
``get_cells -filter``/``utl::metric_integer`` combination, neither of which
this module currently threads through its existing per-stage ``-metrics
<file>.json`` mechanism; both are natural, separable follow-ups (each would
need the same "verified live" rigor the rest of this response contract
carries, not a guess). Neither exclusion changes any existing field's
behaviour, and both can be added later as additive request/response fields
without a contract-shape change -- the same precedent every other v1
exclusion in this module's docstring already follows.

``straps[].spacing_um`` and ``connects[]`` (issue #1133) close a further gap:
sourcing strap geometry from a real platform's own PDN config (e.g. gf180's
``flow/platforms/gf180/openROAD/pdn/pdn_grid_strategy_9t_6M.cfg``) previously
had no way to express that config's ``add_pdn_stripe -spacing`` (the paired
power/ground stripe spacing on one layer) or its ``add_pdn_connect`` via-stack
tuning (``-max_columns``/``-ongrid``/``-split_cuts``) -- both silently
dropped, with nothing in the request or response recording that a deviation
from the cited platform config was taken. ``straps[].spacing_um`` (optional,
per-strap) drives ``-spacing`` on that strap's own ``add_pdn_stripe`` call.
``connects[]`` (optional; each entry: ``layers`` naming one of ``straps[]``'s
own consecutive pairs, plus optional ``max_columns``/``ongrid``/
``split_cuts``) drives the matching pair's ``add_pdn_connect`` call; a pair
with no matching ``connects[]`` entry keeps this module's prior bare
``add_pdn_connect -grid {grid} -layers {lower upper}`` call, unchanged. Both
fields are purely additive -- a request omitting them produces byte-identical
Tcl to before this field existed -- and the response's own ``power`` field
echoes exactly what was applied (``power.straps[].spacing_um``,
``power.connects[]``), for every consecutive pair, not just the ones a
caller's ``connects[]`` tuned (see :func:`_pdn_connect_spec`/
:func:`_pdn_connects_applied`).

``request.power`` omitted (the default) still emits **no**
``tapcell``/full-PDN ``add_global_connection``/``pdngen -- straps`` line
anywhere -- the caller-configured PDN this section otherwise describes
never runs -- and the response's additive ``power`` field's own
``pdn``/``global_connect``/``tapcell_master``/``endcap_master``/
``straps``/``connects`` all still report that nothing ran, exactly as
before. It is **not**, however, byte-identical Tcl to before issue #1442:
the ``"route"`` stage now always emits a small, separate,
``request.power``-independent row-rail fallback
(:func:`_row_rail_lines`/:data:`_ROW_RAIL_STRAP`) on every
``cell_library`` where the real defect exists -- a single
``add_global_connection``/``global_connect`` pair binding the library's
own literal ``VPWR``/``VGND`` pins, plus a ``pdngen`` drawing just that
library's row-rail ``-followpins`` stripe (no straps, no tapcells, no
caller-net aliasing) -- *and*, once that rail exists, this stage's own
``filler_placement``/``global_connect`` pair now also runs (previously
gated on ``request.power`` alone), closing every row gap the same way a
full PDN's own filler pass would. See :data:`_ROW_RAIL_STRAP`'s own
docstring for the full root-cause citation: without the row-rail
obstruction, a ``request.power``-less run left every unfilled standard-
cell row gap as genuine free routing space on the same layer the row's
own power rail lives on, letting a signal net route straight across it --
a real, DRC-invisible short once a filler cell's PG strap later landed in
that same gap; live-verified (issue #1442's own PR description) that the
row-rail obstruction closes that gap with zero
``merged_net_labels[]`` power/signal shorts, whether or not
``filler_placement`` also runs. The response's own additive
``power.row_rail`` field (``emitted``/``layer``/``power_net``/
``ground_net``/``filler_masters``) reports exactly what this fallback
did -- see ``docs/cli/place-and-route.md``'s "Power delivery" section for
the full request/response contract.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from typing import Any

from ._openroad_engine import (
    _count_violations,
    _openroad_version,
    _run_openroad,
    _timing_status,
)

# `_count_spef_nets_annotated`/`_tcl_net_list` are `_paths.py`-hosted helpers
# that only the STA subsystem calls after the issue #1808 split; they are
# imported here purely so `klayout_tools.place_and_route.<name>` keeps
# resolving for callers that reach them by that path (the test suite does).
# Same intent -- and same redundant `X as X` alias form -- as the
# `place_and_route_sta` re-export block below.
from ._paths import _count_spef_nets_annotated as _count_spef_nets_annotated
from ._paths import (
    _load_request_json,
    validate_request_shape,
)
from ._paths import _tcl_net_list as _tcl_net_list
from ._provenance import build_provenance
from .lef_header import read_lef_header
from .pdk import lef_files
from .pdk_cells import list_lib_corners, resolve_liberty_for_cell_library

# The DEF->GDS merge subsystem (issue #1090/#438/#1032/#1029/#1488) lives in
# `place_and_route_gds_merge.py` (issue #1820 split). `_merge_def_to_gds` is
# the one name that subsystem calls back into this module for -- called once
# from `run_place_and_route` below, and imported here (rather than called via
# a qualified `place_and_route_gds_merge._merge_def_to_gds`) purely so
# `klayout_tools.place_and_route._merge_def_to_gds` keeps resolving for
# `monkeypatch.setattr(place_and_route, "_merge_def_to_gds", ...)` callers
# (the test suite does this). The two DEF-property-id constants are re-
# exported for the same reason -- the test suite reaches
# `place_and_route._DEF_INSTANCE_NAME_PROPERTY_ID` by that path from before
# this split. Same redundant `X as X` re-export convention as the
# `place_and_route_sta` block above.
from .place_and_route_gds_merge import (
    _DEF_INSTANCE_NAME_PROPERTY_ID as _DEF_INSTANCE_NAME_PROPERTY_ID,
)
from .place_and_route_gds_merge import (
    _SINGLE_PIN_NET_MARKER_HALF_DBU as _SINGLE_PIN_NET_MARKER_HALF_DBU,
)
from .place_and_route_gds_merge import _merge_def_to_gds as _merge_def_to_gds
from .place_and_route_reports import (
    count_route_drc_violations as _count_route_drc_violations,
)
from .place_and_route_reports import (
    detailed_route_lines,
    route_metrics_object,
    write_route_metrics,
)

# The post-route multi-corner sweep + SPEF-annotated STA subsystem (issues
# #949/#948/#961) lives in `place_and_route_sta.py` (issue #1808 split). The
# rest of this module never calls these directly by their own names -- they
# are re-exported purely so `klayout_tools.place_and_route.<name>` keeps
# working (the test suite imports several of them by that path from before
# this split). The redundant `X as X` aliases mark them as intentional
# re-exports (mirrors `gen_compose.py`'s own re-export block for
# `gen_compose_routing.py`'s names).
from .place_and_route_sta import (
    _corner_sweep_script_lines as _corner_sweep_script_lines,
)
from .place_and_route_sta import _def_pin_net_names as _def_pin_net_names
from .place_and_route_sta import _post_route_spef_metrics as _post_route_spef_metrics
from .place_and_route_sta import _run_corner_sweep as _run_corner_sweep
from .place_and_route_sta import _spef_sta_script_lines as _spef_sta_script_lines
from .place_and_route_sta import def_pin_names as def_pin_names

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

#: Valid ``request.pdk.interconnect_corner`` values (issue #1100) -- the
#: fixed three-value open_pdks tech-LEF parasitic-corner convention
#: :func:`klayout_tools.pdk.lef_files`'s own docstring documents
#: (``min``/``nom``/``max``), not a dynamic per-installed-file set the way
#: liberty corners are, so this is a plain enum rather than something
#: :func:`_resolve_liberty`-style file-existence resolution needs to
#: enumerate.
_TECH_LEF_CORNERS = frozenset({"min", "nom", "max"})

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
#: - ``gf180mcu_fd_sc_mcu7t5v0`` -> ``gf180mcu_fd_sc_mcu7t5v0__buf_4``: the
#:   same ``platforms/gf180/config.mk`` ``ABC_DRIVER_CELL`` template
#:   resolved for the platform's other supported ``TRACK_OPTION ?= 9t|7t``
#:   value (``POWER_OPTION`` still defaults to ``5v0``), i.e.
#:   ``gf180mcu_fd_sc_mcu7t5v0__buf_4`` (issue #1649). Confirmed present as
#:   ``MACRO gf180mcu_fd_sc_mcu7t5v0__buf_4`` in that library's own LEF
#:   (``libs.ref/gf180mcu_fd_sc_mcu7t5v0/lef/gf180mcu_fd_sc_mcu7t5v0.lef``,
#:   gf180mcuA variant), and not excluded by the platform's
#:   ``DONT_USE_CELLS = *_1``.
#: - ``sg13g2_stdcell`` (issue #1784) -> ``sg13g2_buf_16``: IHP ships no
#:   ORFS platform config, so the source of truth is its own LibreLane
#:   platform config (``libs.tech/librelane/sg13g2_stdcell/config.tcl``,
#:   IHP-Open-PDK v0.3.0), whose own dedicated ``CTS_ROOT_BUFFER`` field
#:   names this cell -- the closest direct analog to
#:   :func:`clock_tree_synthesis`'s ``-root_buf`` this table's single-cell
#:   shape needs (that same config's ``CTS_CLK_BUFFERS`` additionally names
#:   a ``sg13g2_buf_8``/``sg13g2_buf_4``/``sg13g2_buf_2`` size ladder for
#:   ``-buf_list``, which this table has no field for -- this module passes
#:   the same single cell to both flags, matching every existing entry's
#:   own shape rather than inventing a ladder field). Confirmed present as
#:   ``MACRO sg13g2_buf_16`` in the installed
#:   ``libs.ref/sg13g2_stdcell/lef/sg13g2_stdcell.lef``.
_CTS_BUFFER_CELLS: dict[str, str] = {
    "sky130_fd_sc_hd": "sky130_fd_sc_hd__buf_4",
    "gf180mcu_fd_sc_mcu9t5v0": "gf180mcu_fd_sc_mcu9t5v0__buf_4",
    "gf180mcu_fd_sc_mcu7t5v0": "gf180mcu_fd_sc_mcu7t5v0__buf_4",
    "sg13g2_stdcell": "sg13g2_buf_16",
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
#:
#: ``gf180mcu_fd_sc_mcu7t5v0`` (issue #1649) shares the ``gf180mcu_fd_sc_
#: mcu9t5v0`` entry's value verbatim: ``platforms/gf180/config.mk``'s
#: ``MIN_ROUTING_LAYER ?= Metal2``/``MAX_ROUTING_LAYER ?= Metal5`` are plain,
#: un-templated values -- unlike ``TECH_LEF``/``SC_LEF``/``FILL_CELLS``/etc,
#: they do not interpolate ``$(TRACK_OPTION)``, so the platform pins the same
#: routing-layer range regardless of the ``9t``/``7t`` track option. The same
#: ``Metal1``-reserved-for-pin-access rationale above also holds for this
#: library: its own ``buf_4``'s ``I``/``Z`` are both ``LAYER Metal1`` in
#: ``libs.ref/gf180mcu_fd_sc_mcu7t5v0/lef/gf180mcu_fd_sc_mcu7t5v0.lef``
#: (gf180mcuA variant).
#:
#: ``sg13g2_stdcell`` (issue #1784) -> ``Metal2-TopMetal2``. IHP ships no
#: ORFS platform config; the source of truth is its own LibreLane platform
#: config (``libs.tech/librelane/config.tcl``, IHP-Open-PDK v0.3.0):
#: ``RT_MIN_LAYER "Metal2"`` / ``RT_MAX_LAYER "TopMetal2"`` verbatim. Same
#: "``Metal1`` reserved for pin access" rationale as both gf180mcu entries
#: above: this library's own ``buf_4``'s ``A``/``X`` pins are both ``LAYER
#: Metal1`` in the installed ``libs.ref/sg13g2_stdcell/lef/sg13g2_stdcell.lef``.
#: The upper bound reaches all the way to ``TopMetal2`` -- this stack's own
#: ``libs.ref/sg13g2_stdcell/lef/sg13g2_tech.lef`` declares exactly seven
#: routing layers (``Metal1``..``Metal5``, ``TopMetal1``, ``TopMetal2``,
#: confirmed live via ``grep '^LAYER '``), matching that same LibreLane
#: config's own ``RT_MAX_LAYER``.
_ROUTING_LAYER_RANGE: dict[str, str] = {
    "sky130_fd_sc_hd": "met1-met5",
    "gf180mcu_fd_sc_mcu9t5v0": "Metal2-Metal5",
    "gf180mcu_fd_sc_mcu7t5v0": "Metal2-Metal5",
    "sg13g2_stdcell": "Metal2-TopMetal2",
}

#: Per-cell-library fallback "row rail" ``-followpins`` PDN stripe, emitted
#: unconditionally at the start of the ``"route"`` stage whenever
#: ``request.power`` was *not* given (issue #1442) -- **not** gated behind
#: ``request.power`` the way :data:`_TAPCELL_CELLS`/:data:`_POWER_PIN_PATTERNS`
#: (the *full*, caller-configured PDN) are.
#:
#: Root cause this closes: :data:`_ROUTING_LAYER_RANGE`'s own
#: ``sky130_fd_sc_hd`` entry starts signal routing at ``met1`` -- the same
#: layer that library's own standard-cell ``VPWR``/``VGND`` row rail lives
#: on. A real ORFS run is safe doing this only because ``pdngen`` *always*
#: runs before ``global_route``/``detailed_route`` there, drawing each row's
#: rail as one continuous ``-followpins`` shape spanning the row's full
#: width -- a real, pre-existing routing obstruction, independent of which
#: cells happen to occupy each gap in that row. `klt place-and-route`
#: uniquely allows reaching the ``"route"`` stage with no PDN at all
#: (``request.power`` omitted): without *some* row-rail shape already
#: drawn, every unfilled row gap is genuine free space on ``met1`` as far as
#: the router is concerned, and it may legally route a signal net straight
#: across it -- a real, DRC-invisible short once a filler cell's own PG
#: strap later lands in that same gap (see this module's own "Power
#: delivery" docstring section and issue #1442 for the full analysis).
#:
#: This table intentionally covers only ``sky130_fd_sc_hd``: neither
#: gf180mcu cell library is affected -- both ``gf180mcu_fd_sc_mcu9t5v0`` and
#: ``gf180mcu_fd_sc_mcu7t5v0`` (issue #1649) share the same
#: :data:`_ROUTING_LAYER_RANGE` entry, which starts one layer *above* where
#: that platform's row rail lives (``Metal2``, not ``Metal1`` -- see that
#: table's own docstring), so their signal router never shares a layer with
#: the row rail in the first place, and this repo has no independently-
#: verified gf180mcu row-rail geometry to add here regardless (this table
#: follows the same "not derivable from the install, verified not guessed"
#: posture as every other per-library table in this module).
#:
#: ``sg13g2_stdcell`` (issue #1784) is unaffected for the same structural
#: reason: its own :data:`_ROUTING_LAYER_RANGE` entry also starts one layer
#: above where its row rail lives -- IHP's own LibreLane platform config
#: (``libs.tech/librelane/config.tcl``, IHP-Open-PDK v0.3.0) names
#: ``PDN_RAIL_LAYER "Metal1"`` while ``RT_MIN_LAYER`` is ``"Metal2"``.
#:
#: Values sourced 2026-08-26 from the real ``openroad/orfs:latest``
#: container's own vendored ORFS checkout (``The-OpenROAD-Project/
#: OpenROAD-flow-scripts`` @ ``master``), ``platforms/sky130hd/pdn.tcl``'s
#: own ``standard cell grid`` section: ``add_pdn_stripe -grid {grid} -layer
#: {met1} -width {0.48} -pitch {5.44} -offset {0} -followpins`` -- the exact
#: same stripe a full ``request.power`` PDN run would draw for a met1
#: strap, just without the vertical straps/tapcells/global-connect-to-a-
#: caller-net that the rest of that config also carries. The power/ground
#: pin name (``VPWR``/``VGND``) is the LEF's own literal standard-cell pin
#: name -- also confirmed against that same ``pdn.tcl``'s own
#: ``add_global_connection ... -pin_pattern {VPWR}``/``{VGND}`` lines
#: (unaliased to a caller-chosen net, unlike :data:`_POWER_PIN_PATTERNS`,
#: since there is no caller-given ``request.power.power_net``/
#: ``.ground_net`` to alias to here) -- deliberately reusing ``VPWR``/
#: ``VGND`` as both the pin pattern *and* the net name this fallback
#: creates, so a caller inspecting the routed DEF's ``SPECIALNETS`` sees the
#: library's own familiar pin names, not an invented placeholder.
#:
#: Tuple shape: ``(layer, width_um, pitch_um, offset_um, power_pin,
#: ground_pin)``.
_ROW_RAIL_STRAP: dict[str, tuple[str, float, float, float, str, str]] = {
    "sky130_fd_sc_hd": ("met1", 0.48, 5.44, 0.0, "VPWR", "VGND"),
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
#: - ``gf180mcu_fd_sc_mcu7t5v0`` -> ``gf180mcu_fd_sc_mcu7t5v0__antenna``,
#:   pin ``I`` (issue #1649) -- that library's own LEF
#:   (``libs.ref/gf180mcu_fd_sc_mcu7t5v0/lef/gf180mcu_fd_sc_mcu7t5v0.lef``,
#:   gf180mcuA variant)'s only ``ANTENNACELL``-classed macro (``CLASS core
#:   ANTENNACELL``), with exactly one non-power/ground pin (``I``,
#:   ``DIRECTION INPUT``; ``VDD``/``VNW`` are ``USE POWER``, ``VSS``/``VPW``
#:   are ``USE GROUND``) -- the same shape as the ``9t`` sibling above.
#: - ``sg13g2_stdcell`` (issue #1784) -> ``sg13g2_antennanp``, pin ``A`` --
#:   IHP's own LibreLane platform config
#:   (``libs.tech/librelane/sg13g2_stdcell/config.tcl``, IHP-Open-PDK
#:   v0.3.0) names this cell/pin explicitly, as ``DIODE_CELL =
#:   "sg13g2_antennanp/A"`` (its own ``"<cell>/<pin>"`` shape for this
#:   field) -- so unlike the sky130/gf180mcu entries above (each derived
#:   from the LEF alone, since neither ORFS platform names a diode cell),
#:   this one has a direct platform-config citation. Cross-checked against
#:   the installed ``libs.ref/sg13g2_stdcell/lef/sg13g2_stdcell.lef``:
#:   ``sg13g2_antennanp`` is that LEF's only ``CLASS CORE ANTENNACELL``
#:   macro, with exactly one non-power/ground pin (``A``, ``USE SIGNAL``;
#:   ``VDD``/``VSS`` are ``USE POWER``/``USE GROUND``) -- the same
#:   one-signal-pin shape every other entry in this table has. Its own
#:   liberty (``sg13g2_stdcell_typ_1p20V_25C.lib``) additionally marks it
#:   ``dont_touch : true; dont_use : true;``, matching a diode cell's
#:   intended use (inserted post-route by ``repair_antennas`` only, never
#:   synthesis-mapped) -- not carried into :data:`_ABC_DONT_USE_GLOBS`
#:   since that table's job is excluding cells from *synthesis* mapping,
#:   which this cell would never reach regardless (no combinational
#:   function to map to).
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
    "gf180mcu_fd_sc_mcu7t5v0": ("gf180mcu_fd_sc_mcu7t5v0__antenna", "I"),
    "sg13g2_stdcell": ("sg13g2_antennanp", "A"),
}

#: Per-cell-library ``add_global_connection`` pin-pattern rules for the
#: optional ``request.power`` PDN stage (issue #1091) -- same not-derivable-
#: from-the-install, verified-not-guessed posture as
#: :data:`_CTS_BUFFER_CELLS`/:data:`_ROUTING_LAYER_RANGE`/
#: :data:`_ANTENNA_DIODE_CELLS` above, but sourced from each platform's own
#: PDN grid config rather than `config.mk`: `add_global_connection`'s own
#: `-pin_pattern` alias list (`VDDPE`/`VDDCE`/etc.) is a *platform*
#: convention describing how real macro/IO-ring instances name their PG
#: pins, not something derivable from a standard cell's own LEF `PIN ...
#: USE POWER/GROUND` entries.
#:
#: Each tuple is `(net_role, pin_pattern, is_primary)`. `net_role` selects
#: which of `request.power.power_net`/`.ground_net` the generated
#: `add_global_connection -net {...}` call names; exactly one entry per role
#: carries `is_primary=True`, emitting that call's `-power`/`-ground` flag --
#: the pin `pdngen` treats as that net's primary, connectivity-defining
#: port, mirroring `add_global_connection`'s own flag semantics (confirmed
#: live, `openroad -no_init`, `help add_global_connection`, this repo's own
#: sandboxed OpenROAD 26Q3-1278-g4421880472 install, 2026-08-17). The
#: pattern list itself is copied verbatim from each platform's own PDN
#: config, fetched 2026-08-17 from
#: `The-OpenROAD-Project/OpenROAD-flow-scripts` @ `master`:
#: - `sky130_fd_sc_hd` -> `platforms/sky130hd/pdn.tcl`
#: - `gf180mcu_fd_sc_mcu9t5v0` ->
#:   `platforms/gf180/openROAD/pdn/pdn_grid_strategy_9t_6M.cfg`
#: - `gf180mcu_fd_sc_mcu7t5v0` (issue #1649) ->
#:   `platforms/gf180/openROAD/pdn/pdn_grid_strategy_7t_6M.cfg`'s `global
#:   connections` section -- byte-for-byte the same `add_global_connection`
#:   pattern list as the `9t` config above (`PDN_TCL ?= .../pdn_grid_strategy_
#:   $(TRACK_OPTION)_6M.cfg` selects the sibling file per `TRACK_OPTION`, but
#:   both files' global-connection rules are identical), fetched 2026-09-11
#:   from the same `OpenROAD-flow-scripts` @ `master`.
#: - `sg13g2_stdcell` (issue #1784) -> IHP's own LibreLane platform config
#:   (`libs.tech/librelane/config.tcl`, IHP-Open-PDK v0.3.0) names exactly
#:   `SCL_POWER_PINS "VDD"` / `SCL_GROUND_PINS "VSS"` -- no macro/IO-ring
#:   pin-alias list at all, unlike sky130/gf180mcu's `VDDPE`/`VDDCE`/etc
#:   families (this platform's own PDN config has no macro-power-domain
#:   convention to alias). Cross-checked against the installed
#:   `libs.ref/sg13g2_stdcell/lef/sg13g2_stdcell.lef`: every standard cell's
#:   only power/ground pins are `VDD` (`USE POWER`) / `VSS` (`USE GROUND`),
#:   with no other `USE POWER`/`USE GROUND` pin name anywhere in the file --
#:   so this entry is deliberately just the two primary patterns, no
#:   `is_primary=False` aliases.
_POWER_PIN_PATTERNS: dict[str, tuple[tuple[str, str, bool], ...]] = {
    "sky130_fd_sc_hd": (
        ("power", "^VDD$", True),
        ("power", "^VDDPE$", False),
        ("power", "^VDDCE$", False),
        ("power", "VPWR", False),
        ("power", "VPB", False),
        ("ground", "^VSS$", True),
        ("ground", "^VSSE$", False),
        ("ground", "VGND", False),
        ("ground", "VNB", False),
    ),
    "gf180mcu_fd_sc_mcu9t5v0": (
        ("power", "^VDD$", True),
        ("power", "^VDDPE$", False),
        ("power", "^VDDCE$", False),
        ("power", "^VDDP$", False),
        ("power", "^VDDC$", False),
        ("power", "^VNW$", False),
        ("ground", "^VSS$", True),
        ("ground", "^VSSE$", False),
        ("ground", "^VSSC$", False),
        ("ground", "^VPW$", False),
    ),
    "gf180mcu_fd_sc_mcu7t5v0": (
        ("power", "^VDD$", True),
        ("power", "^VDDPE$", False),
        ("power", "^VDDCE$", False),
        ("power", "^VDDP$", False),
        ("power", "^VDDC$", False),
        ("power", "^VNW$", False),
        ("ground", "^VSS$", True),
        ("ground", "^VSSE$", False),
        ("ground", "^VSSC$", False),
        ("ground", "^VPW$", False),
    ),
    "sg13g2_stdcell": (
        ("power", "^VDD$", True),
        ("ground", "^VSS$", True),
    ),
}

#: Per-cell-library `tapcell` call arguments for the optional
#: `request.power` PDN stage (issue #1091). Same posture as the tables
#: above. Tuple shape: `(tapcell_master, endcap_master_or_None,
#: distance_um)`. Sourced 2026-08-17 from each platform's own
#: `tapcell.tcl`, `The-OpenROAD-Project/OpenROAD-flow-scripts` @ `master`:
#: - `sky130_fd_sc_hd` -> `platforms/sky130hd/tapcell.tcl`: `tapcell
#:   -distance 14 -tapcell_master $::env(TAP_CELL_NAME)`, where
#:   `platforms/sky130hd/config.mk` names `TAP_CELL_NAME =
#:   sky130_fd_sc_hd__tapvpwrvgnd_1`. That platform's own `tapcell.tcl`
#:   passes no `-endcap_master` at all.
#: - `gf180mcu_fd_sc_mcu9t5v0` -> `platforms/gf180/openROAD/tapcell.tcl`:
#:   `tapcell -distance 100 -tapcell_master $::env(TIE_CELL) -endcap_master
#:   $::env(ENDCAP_CELL)`, where `platforms/gf180/config.mk` names
#:   `TIE_CELL = gf180mcu_fd_sc_mcu9t5v0__filltie` and `ENDCAP_CELL =
#:   gf180mcu_fd_sc_mcu9t5v0__endcap` (`TRACK_OPTION ?= 9t`, `POWER_OPTION ?=
#:   5v0` are that platform's own defaults, matching this supported
#:   `cell_library` name).
#: - `gf180mcu_fd_sc_mcu7t5v0` (issue #1649) -> the same
#:   `platforms/gf180/openROAD/tapcell.tcl`/`config.mk` templates resolved
#:   for `TRACK_OPTION = 7t` instead: `TIE_CELL`/`ENDCAP_CELL` both
#:   interpolate `$(TRACK_OPTION)$(POWER_OPTION)`, giving
#:   `gf180mcu_fd_sc_mcu7t5v0__filltie`/`gf180mcu_fd_sc_mcu7t5v0__endcap`; the
#:   `tapcell.tcl` call's own `-distance 100` is a plain, un-templated value
#:   shared by both track options. Confirmed present as
#:   `MACRO gf180mcu_fd_sc_mcu7t5v0__filltie`/`__endcap` in that library's own
#:   LEF (gf180mcuA variant).
#:
#: `sg13g2_stdcell` (issue #1784) has **no entry, deliberately** -- this
#: standard-cell library ships no tap or endcap cells at all. Confirmed two
#: ways: (1) `sg13g2_stdcell.lef`/`.lib` contain no cell whose footprint or
#: name suggests a well/substrate tie (no `tap`/`fill*tie`/`endcap`-shaped
#: macro anywhere in either file, `grep`-verified against the installed
#: IHP-Open-PDK v0.3.0 files); (2) IHP's own LibreLane platform config says
#: so explicitly -- `libs.tech/librelane/sg13g2_stdcell/config.tcl`'s own
#: comment: `"Welltap and endcap cells / There are no endcap and welltie
#: cells in ihp-sg13g2 / thus set to undefined to skip insertion"`
#: (`WELLTAP_CELL`/`ENDCAP_CELL` are both left commented out), and the
#: sibling `libs.tech/librelane/config.tcl` sets `FP_TAPCELL_DIST 0` with
#: its own `"No tap cells"` comment. Inventing a tapcell entry here would be
#: exactly the guess this table's docstring convention exists to avoid --
#: so `request.power` correctly raises "no tapcell master known for
#: standard-cell library 'sg13g2_stdcell'" (this module's own validation,
#: above) rather than silently skipping the well-tie step a caller asked
#: for. A `klt place-and-route` run that never sets `request.power`, or
#: that reaches only the `"floorplan"`/`"place"`/`"cts"`/`"route"` stages
#: without it, is unaffected -- none of those paths consult this table.
_TAPCELL_CELLS: dict[str, tuple[str, str | None, int]] = {
    "sky130_fd_sc_hd": ("sky130_fd_sc_hd__tapvpwrvgnd_1", None, 14),
    "gf180mcu_fd_sc_mcu9t5v0": (
        "gf180mcu_fd_sc_mcu9t5v0__filltie",
        "gf180mcu_fd_sc_mcu9t5v0__endcap",
        100,
    ),
    "gf180mcu_fd_sc_mcu7t5v0": (
        "gf180mcu_fd_sc_mcu7t5v0__filltie",
        "gf180mcu_fd_sc_mcu7t5v0__endcap",
        100,
    ),
}

#: Per-cell-library filler-cell masters for the optional `request.power`
#: PDN stage's post-route `filler_placement` call (issue #1091). Same
#: posture as the tables above; sourced 2026-08-17 from each platform's own
#: `config.mk` `FILL_CELLS` variable
#: (`platforms/sky130hd/config.mk`/`platforms/gf180/config.mk`,
#: `The-OpenROAD-Project/OpenROAD-flow-scripts` @ `master`), in the same
#: order each platform's own list names.
#:
#: `gf180mcu_fd_sc_mcu7t5v0` (issue #1649) reuses `platforms/gf180/
#: config.mk`'s own `FILL_CELLS` template -- `gf180mcu_fd_sc_mcu$(TRACK_
#: OPTION)$(POWER_OPTION)__fill_*`, largest-to-smallest -- resolved for
#: `TRACK_OPTION = 7t` instead of the `9t` entry's default, giving the same
#: seven sizes in the same order. Confirmed present as `MACRO
#: gf180mcu_fd_sc_mcu7t5v0__fill_{1,2,4,8,16,32,64}` in that library's own
#: LEF (gf180mcuA variant).
#:
#: `sg13g2_stdcell` (issue #1784) -> `sg13g2_fill_1`, `sg13g2_fill_2`,
#: copied verbatim (same order) from IHP's own LibreLane platform config
#: (`libs.tech/librelane/sg13g2_stdcell/config.tcl`, IHP-Open-PDK v0.3.0):
#: `FILL_CELLS "sg13g2_fill_1 sg13g2_fill_2"`. The installed
#: `libs.ref/sg13g2_stdcell/lef/sg13g2_stdcell.lef` additionally ships
#: larger `sg13g2_fill_4`/`sg13g2_fill_8` masters, but the platform's own
#: curated list deliberately uses only the two smallest -- mirrored as-is
#: rather than widened by analogy to sky130hd's four-size list.
_FILLER_CELLS: dict[str, tuple[str, ...]] = {
    "sky130_fd_sc_hd": (
        "sky130_fd_sc_hd__fill_1",
        "sky130_fd_sc_hd__fill_2",
        "sky130_fd_sc_hd__fill_4",
        "sky130_fd_sc_hd__fill_8",
    ),
    "gf180mcu_fd_sc_mcu9t5v0": (
        "gf180mcu_fd_sc_mcu9t5v0__fill_64",
        "gf180mcu_fd_sc_mcu9t5v0__fill_32",
        "gf180mcu_fd_sc_mcu9t5v0__fill_16",
        "gf180mcu_fd_sc_mcu9t5v0__fill_8",
        "gf180mcu_fd_sc_mcu9t5v0__fill_4",
        "gf180mcu_fd_sc_mcu9t5v0__fill_2",
        "gf180mcu_fd_sc_mcu9t5v0__fill_1",
    ),
    "gf180mcu_fd_sc_mcu7t5v0": (
        "gf180mcu_fd_sc_mcu7t5v0__fill_64",
        "gf180mcu_fd_sc_mcu7t5v0__fill_32",
        "gf180mcu_fd_sc_mcu7t5v0__fill_16",
        "gf180mcu_fd_sc_mcu7t5v0__fill_8",
        "gf180mcu_fd_sc_mcu7t5v0__fill_4",
        "gf180mcu_fd_sc_mcu7t5v0__fill_2",
        "gf180mcu_fd_sc_mcu7t5v0__fill_1",
    ),
    "sg13g2_stdcell": (
        "sg13g2_fill_1",
        "sg13g2_fill_2",
    ),
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
#:
#: ``sg13g2_stdcell`` (issue #1784) -> ``"sg13g2"`` -- the IHP SG13G2 deck
#: family already registered in ``klayout_tools.decks.__init__`` (used
#: today by ``klt extract``/``klt drc``/``klt lvs`` for this same PDK); no
#: new deck family is introduced here, this table just bridges to it.
_EXTRACT_DECK_FOR_CELL_LIBRARY: dict[str, str] = {
    "sky130_fd_sc_hd": "sky130",
    "gf180mcu_fd_sc_mcu9t5v0": "gf180mcu",
    "sg13g2_stdcell": "sg13g2",
}

#: Fixed internal `global_placement -density` target -- not an exposed
#: request field (the contract's `floorplan` block sizes the die/core, not
#: placement density); verified live at this value against the worked
#: example (docstring above).
_GLOBAL_PLACEMENT_DENSITY = 0.6

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

#: Same marker convention as the setup/hold pair above, isolating the
#: post-route corner sweep's own design-rule-check reports (issue #1709's
#: "Two smaller things found alongside" item 1, folded into that issue's own
#: Builder scope by its 2026-09-15 revision) so :func:`_count_violations` can
#: isolate the max-transition block from the max-capacitance block within the
#: sweep invocation's own combined stdout -- see
#: :func:`_design_rule_check_lines`.
_MAX_TRANSITION_VIOLATIONS_BEGIN = "===KLT_MAX_TRANSITION_VIOLATIONS_BEGIN==="
_MAX_TRANSITION_VIOLATIONS_END = "===KLT_MAX_TRANSITION_VIOLATIONS_END==="
_MAX_CAPACITANCE_VIOLATIONS_BEGIN = "===KLT_MAX_CAPACITANCE_VIOLATIONS_BEGIN==="
_MAX_CAPACITANCE_VIOLATIONS_END = "===KLT_MAX_CAPACITANCE_VIOLATIONS_END==="

#: Same marker convention as the setup/hold pair above, isolating
#: `check_antennas`'s own stdout (run post-`repair_antennas`, `"route"`
#: stage only) so :func:`_count_antenna_violations` can parse its summary
#: count line without picking up unrelated output.
_ANTENNA_VIOLATIONS_BEGIN = "===KLT_ANTENNA_VIOLATIONS_BEGIN==="
_ANTENNA_VIOLATIONS_END = "===KLT_ANTENNA_VIOLATIONS_END==="

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
    # Issue #1826: the single, nominal-corner hold WNS -- `null` before the
    # `"place"` stage, matching `hold_violation_count`'s own gating just
    # above. Distinct from `worst_hold_slack_ns` below (the corner-swept,
    # `"route"`-stage-only aggregate); this one exists specifically so a
    # pre-route caller can get a hold slack *number*, not just a violation
    # count.
    "nominal_hold_slack_ns",
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
    # Additive (issue #1092): per-corner setup/hold slack breakdown --
    # names which swept corner produced `worst_setup_slack_ns`/
    # `worst_hold_slack_ns` above, the same "restated at top level" rule
    # every other `stages[]` metric field already follows. `null` (never
    # `[]`) on any stage before `"route"`, matching `worst_setup_slack_ns`'s
    # own convention.
    "corners",
    # Additive (issue #1709): the design-rule-check verdict measured at the
    # same swept corners as `worst_setup_slack_ns`/`worst_hold_slack_ns`
    # above, and `null` on the same pre-`"route"` stages for the same reason
    # -- the sweep invocation is the only session that loads every swept
    # deck's own max-transition/max-capacitance limits.
    "max_transition_violation_count",
    "max_capacitance_violation_count",
    # Additive (issue #1865): `"constrained"` / `"unconstrained"` / `null` --
    # whether the slack fields above are measurements at all, or OpenSTA's
    # own unconstrained-design sentinel (`1e+39`) restated. See
    # `_extract_stage_metrics` and `_timing_status`.
    "timing_status",
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
    # Issue #1973: fail here, naming the construct and its line, rather than
    # letting a `signed` qualifier surface as OpenSTA's raw `STA-0171` syntax
    # error at the floorplan stage or a bare `x` constant as TritonRoute's
    # `DRT-0305` a full route attempt later. See this module's docstring
    # "Netlist pre-flight" section.
    _reject_unsupported_netlist_constructs(netlist_path)

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
    # `request.pdk.interconnect_corner` (issue #1100): the tech LEF's own
    # parasitic-extraction corner -- open_pdks stages exactly three
    # (`"min"`/`"nom"`/`"max"`, `lef_files`'s own docstring in `pdk.py`), a
    # fixed convention unlike the dynamic, per-installed-file liberty
    # corner set `_resolve_liberty` resolves below, so this is a plain
    # enum-check rather than file-existence-driven resolution. `None`
    # (the default, field omitted) preserves this module's original
    # behaviour byte-for-byte: `_resolve_lef` resolves the same `"nom"`
    # tech LEF it always has.
    interconnect_corner = pdk_spec.get("interconnect_corner", "nom")
    if interconnect_corner not in _TECH_LEF_CORNERS:
        raise PlaceAndRouteError(
            "request.pdk.interconnect_corner must be one of: "
            + ", ".join(sorted(_TECH_LEF_CORNERS))
        )
    # Issue #1092: scopes the post-route multi-corner sweep to a named
    # subset of the shipped corners -- shape-validated here, membership-
    # validated once `cell_library`'s corner set is resolved below (the
    # "route" stage branch).
    sweep_corners = _validate_sweep_corners(pdk_spec.get("sweep_corners"))

    floorplan = _validate_floorplan(request["floorplan"])
    io_spec = _validate_io(request.get("io"))
    macros = _validate_macros(request.get("macros"), request_dir, netlist_path)
    power = _validate_power(request.get("power"))
    (
        clock_port,
        clock_period_ns,
        max_transition_ns,
        max_capacitance_pf,
        max_fanout,
        input_delay_ns,
        output_delay_ns,
    ) = _validate_constraints(request.get("constraints"))
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
    tech_lef, cell_lef = _resolve_lef(cell_library, pdk_info, interconnect_corner)

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
    # `request.power` (issue #1091): its own PDN/tapcell Tcl always runs at
    # the end of the "floorplan" stage (the first stage every run reaches),
    # so these table lookups are unconditional on `stage_index` -- unlike
    # the CTS/routing-layer/antenna-diode checks above, which only apply
    # once a run actually reaches the stage that needs them.
    if power is not None and cell_library not in _POWER_PIN_PATTERNS:
        raise PlaceAndRouteError(
            f"no power pin-pattern table known for standard-cell library "
            f"'{cell_library}' (supported: {', '.join(sorted(_POWER_PIN_PATTERNS))}) "
            "-- cannot honor request.power"
        )
    if power is not None and cell_library not in _TAPCELL_CELLS:
        raise PlaceAndRouteError(
            f"no tapcell master known for standard-cell library "
            f"'{cell_library}' (supported: {', '.join(sorted(_TAPCELL_CELLS))}) "
            "-- cannot honor request.power"
        )
    if (
        power is not None
        and stage_index >= STAGE_ORDER.index("route")
        and cell_library not in _FILLER_CELLS
    ):
        raise PlaceAndRouteError(
            f"no filler-cell masters known for standard-cell library "
            f"'{cell_library}' (supported: {', '.join(sorted(_FILLER_CELLS))}) "
            f"-- cannot honor request.power at target_stage '{target_stage}'"
        )
    # Row-rail fallback (issue #1442): once `stage_index` reaches `"route"`
    # on a `cell_library` with a verified `_ROW_RAIL_STRAP` entry (`power`
    # omitted), the fallback also drives `filler_placement` -- see
    # `_stage_script_lines`'s own `"route"` branch. A defensive check, not a
    # reachable one today: every `_ROW_RAIL_STRAP` entry currently has a
    # matching `_FILLER_CELLS` entry, but this guards a future
    # `_ROW_RAIL_STRAP` addition that doesn't, with a clear error instead of
    # a `KeyError`.
    if (
        power is None
        and stage_index >= STAGE_ORDER.index("route")
        and cell_library in _ROW_RAIL_STRAP
        and cell_library not in _FILLER_CELLS
    ):
        raise PlaceAndRouteError(
            f"no filler-cell masters known for standard-cell library "
            f"'{cell_library}' -- cannot honor the row-rail fallback "
            f"(issue #1442) at target_stage '{target_stage}'"
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
            power=power,
            clock_port=clock_port,
            clock_period_ns=clock_period_ns,
            max_transition_ns=max_transition_ns,
            max_capacitance_pf=max_capacitance_pf,
            max_fanout=max_fanout,
            input_delay_ns=input_delay_ns,
            output_delay_ns=output_delay_ns,
            cell_library=cell_library,
            seed=seed,
            output_dir=output_dir,
            route_critical_nets_percentage=route_critical_nets_percentage,
            max_antenna_repair_iterations=max_antenna_repair_iterations,
        )
        _write_script(script_path, lines)

        completed = _run_openroad(
            script_path, metrics_path, error_cls=PlaceAndRouteError
        )
        if completed.returncode != 0:
            raise PlaceAndRouteError(
                _engine_error_message(stage, completed, pdk_info=pdk_info)
            )

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
        corner_breakdown: list[dict[str, Any]] | None = None
        max_transition_violation_count: int | None = None
        max_capacitance_violation_count: int | None = None
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
            write_route_metrics(
                metrics_path, metrics, route_drc_count, error_cls=PlaceAndRouteError
            )

            # Issue #949: sweep every corner `cell_library` ships for
            # setup/hold slack, as a second OpenROAD invocation over the
            # checkpoint this stage's own script just wrote -- see
            # `_run_corner_sweep`/`_corner_sweep_script_lines` for why this
            # is a separate session rather than more Tcl appended above.
            assert io_spec is not None  # required to reach "route" (validated above)
            corners = list_lib_corners(cell_library, pdk_info)
            # Issue #1092: scope the sweep to `request.pdk.sweep_corners`
            # when given -- every requested name must actually be one of
            # `cell_library`'s own shipped corners, or this is a clear
            # request error (matching this module's existing unresolvable-
            # request-field convention) rather than a silently-dropped name.
            if sweep_corners is not None:
                available_names = {corner["name"] for corner in corners}
                unknown_names = sorted(set(sweep_corners) - available_names)
                if unknown_names:
                    raise PlaceAndRouteError(
                        "request.pdk.sweep_corners names unknown corner(s) "
                        f"{', '.join(unknown_names)} for cell_library "
                        f"'{cell_library}' -- available corners: "
                        + (
                            ", ".join(sorted(available_names))
                            if available_names
                            else "(none shipped)"
                        )
                    )
                corners = [
                    corner for corner in corners if corner["name"] in sweep_corners
                ]
            (
                worst_setup_slack_ns,
                worst_hold_slack_ns,
                corner_breakdown,
                # Issue #1709: the design-rule-check verdict measured in that
                # same sweep invocation -- no extra OpenROAD launch of its own.
                max_transition_violation_count,
                max_capacitance_violation_count,
            ) = _run_corner_sweep(
                checkpoint_in=next_checkpoint,
                corners=corners,
                io_spec=io_spec,
                clock_port=clock_port,
                clock_period_ns=clock_period_ns,
                max_transition_ns=max_transition_ns,
                max_capacitance_pf=max_capacitance_pf,
                max_fanout=max_fanout,
                input_delay_ns=input_delay_ns,
                output_delay_ns=output_delay_ns,
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
                corners=corner_breakdown,
                max_transition_violation_count=max_transition_violation_count,
                max_capacitance_violation_count=max_capacitance_violation_count,
            )
        )
        checkpoint_path = next_checkpoint

    gds_path: str | None = None
    verilog_path: str | None = None
    spef_sta: dict[str, Any] | None = None
    layer_map_info: dict[str, Any] | None = None
    def_net_names_info: dict[str, Any] | None = None
    if target_stage == "route":
        def_path = os.path.join(output_dir, f"{hdl_toplevel}.def")
        gds_path = os.path.join(output_dir, f"{hdl_toplevel}.gds")
        # Same deterministic path `_stage_script_lines`'s own `"route"`
        # branch just handed `write_verilog` (issue #996) -- recomputed here
        # rather than threaded back out, exactly as `def_path` above and the
        # `-output_drc` report path earlier already are.
        verilog_path = os.path.join(output_dir, f"{hdl_toplevel}.v")
        merge_info = _merge_def_to_gds(
            def_path=def_path,
            tech_lef=tech_lef,
            cell_lef=cell_lef,
            pdk_info=pdk_info,
            cell_library=cell_library,
            hdl_toplevel=hdl_toplevel,
            macros=macros,
            out_path=gds_path,
        )
        layer_map_info = {
            "path": merge_info["path"],
            "resolution": merge_info["resolution"],
        }
        def_net_names_info = merge_info["def_net_names"]
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
                max_transition_ns=max_transition_ns,
                max_capacitance_pf=max_capacitance_pf,
                max_fanout=max_fanout,
                input_delay_ns=input_delay_ns,
                output_delay_ns=output_delay_ns,
                checkpoint_in=checkpoint_path,
                # Issue #1002: `write_sdf` inside that same session, right
                # after its `read_spef` -- see `_validate_post_route_sdf`.
                write_sdf=post_route_sdf,
            )
    else:
        def_path = None

    # Additive field (issue #1826): a pre-route DEF, populated only when
    # `target_stage` itself is `"place"` or `"cts"` -- the same "only when
    # this is the actual, final target" convention `def_path` above follows
    # (never populated "along the way" to a later stage a caller didn't
    # actually ask for). Points at the deterministic `write_def` path each
    # of those two stages' own Tcl already writes unconditionally (`"place"`
    # since issue #785; `"cts"` newly added by this issue) -- see
    # `_stage_script_lines`'s own comments for why those paths are safe to
    # recompute here rather than threaded back out, exactly like `def_path`/
    # the DRC report path above already do. `null` at `"floorplan"` (no DEF
    # exists yet) and at `"route"` (the routed `def_path` above is the
    # right artifact there; this field never doubles up with it). This is
    # gap 1's "smaller" resolution from issue #1826's own discussion --
    # reusing the DEF `_stage_script_lines` already produces internally,
    # rather than giving `klt sta` a from-scratch netlist-input mode (the
    # alternative shape #1825 proposes for the same underlying gap).
    unrouted_def_path = (
        os.path.join(output_dir, f"{hdl_toplevel}.{target_stage}.def")
        if target_stage in ("place", "cts")
        else None
    )

    # Additive field (issue #1091): reports what `request.power` actually
    # drove, so a caller can tell a signal-only "route" result from a
    # power-complete one without parsing the DEF for a missing
    # `SPECIALNETS` section -- see this module's own docstring "Power
    # delivery" section. `pdn`/`global_connect` are `True` once
    # `request.power` is present (both run unconditionally together, at the
    # end of the `"floorplan"` stage -- every run reaches at least that
    # stage). `tapcell_master`/`endcap_master`/`filler_masters` name the
    # per-library masters :func:`_power_delivery_lines`/the `"route"` stage's
    # own `filler_placement` call actually used -- **not** a live placed-
    # instance count (OpenROAD reports that only via a
    # `report_design_area`/`get_cells`-style query this module does not yet
    # thread through its per-stage `-metrics` mechanism; see the module
    # docstring's own "Scope deliberately excluded" note). `filler_masters`
    # is `[]` unless this run actually reached the `"route"` stage (the
    # `"floorplan"`-stage `tapcell`/PDN Tcl always runs first, but
    # `filler_placement` is a `"route"`-stage-only call). `straps`/`connects`
    # (issue #1133) echo exactly what `_power_delivery_lines` actually put on
    # each `add_pdn_stripe`/`add_pdn_connect` call -- so a caller citing a
    # real platform PDN config (e.g. gf180's own
    # `pdn_grid_strategy_9t_6M.cfg`) can tell whether its request reproduced
    # that config's `-spacing`/`-max_columns`/`-ongrid`/`-split_cuts` or
    # silently fell back to this module's plain defaults, without re-deriving
    # it from the request document itself. `connects` always lists one entry
    # per consecutive strap pair (built via :func:`_pdn_connects_applied`),
    # whether or not the caller supplied `power.connects[]` tuning for it.
    #
    # `row_rail` (issue #1442): reports the separate, `request.power`-
    # independent fallback :func:`_row_rail_lines` may have emitted at the
    # start of the `"route"` stage -- see that function's own and
    # :data:`_ROW_RAIL_STRAP`'s own docstrings for why this exists and why
    # it is never active when `power is not None` (a `request.power`-bearing
    # run's real PDN already covers this, if its own `straps[]` include the
    # row-rail layer). `emitted` is `True` only once a run both omitted
    # `request.power` *and* actually reached the `"route"` stage on a
    # `cell_library` with a verified :data:`_ROW_RAIL_STRAP` entry.
    # `filler_masters` mirrors `power.filler_masters`'s own shape: the
    # row-rail fallback also drives this same `"route"`-stage-only
    # `filler_placement` call (see `_stage_script_lines`), safe only because
    # the row-rail obstruction above already precedes `global_route`.
    row_rail_emitted = (
        power is None and target_stage == "route" and cell_library in _ROW_RAIL_STRAP
    )
    if row_rail_emitted:
        rail_layer, _w, _p, _o, rail_power_net, rail_ground_net = _ROW_RAIL_STRAP[
            cell_library
        ]
        row_rail_info: dict[str, Any] = {
            "emitted": True,
            "layer": rail_layer,
            "power_net": rail_power_net,
            "ground_net": rail_ground_net,
            "filler_masters": list(_FILLER_CELLS[cell_library]),
        }
    else:
        row_rail_info = {
            "emitted": False,
            "layer": None,
            "power_net": None,
            "ground_net": None,
            "filler_masters": [],
        }
    if power is None:
        power_info: dict[str, Any] = {
            "pdn": False,
            "global_connect": False,
            "power_net": None,
            "ground_net": None,
            "tapcell_master": None,
            "endcap_master": None,
            "filler_masters": [],
            "straps": [],
            "connects": [],
            "row_rail": row_rail_info,
        }
    else:
        tap_master, endcap_master, _distance_um = _TAPCELL_CELLS[cell_library]
        power_info = {
            "pdn": True,
            "global_connect": True,
            "power_net": power["power_net"],
            "ground_net": power["ground_net"],
            "tapcell_master": tap_master,
            "endcap_master": endcap_master,
            "filler_masters": (
                list(_FILLER_CELLS[cell_library]) if target_stage == "route" else []
            ),
            "straps": [
                {"layer": strap["layer"], "spacing_um": strap["spacing_um"]}
                for strap in power["straps"]
            ],
            "connects": _pdn_connects_applied(power),
            "row_rail": row_rail_info,
        }

    last_stage = stages[-1]
    top_metrics = {key: last_stage.get(key) for key in _TOP_LEVEL_METRIC_KEYS}

    engine_version = _openroad_version()
    # `deck` names the resolved LEF/liberty/GDS-view platform set (contract
    # spike section 5's own "e.g. sky130hd" example) -- `<cell_library>__
    # <corner>` matching the STA/timing (liberty/device) corner actually
    # requested, the same naming `klt synthesize`'s own `deck.name` uses.
    # The tech LEF's own "nom"/"min"/"max" parasitic (interconnect) corner
    # *is* independently caller-selectable (issue #1100,
    # `request.pdk.interconnect_corner`) -- it is deliberately kept out of
    # `deck_name` (an artifact-naming convention other stages/tests key off
    # of) and reported instead as the sibling top-level `interconnect_corner`
    # response field below, the same additive-sibling-field shape
    # `spef_sta` (issue #948) used rather than folding into existing naming.
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
        # Additive field (issue #1100): the tech-LEF parasitic-extraction
        # corner actually resolved (`"min"`/`"nom"`/`"max"`,
        # `request.pdk.interconnect_corner`, default `"nom"`) -- distinct
        # from the *liberty* (device) corner, which `deck_name` above
        # already encodes (e.g. `sky130_fd_sc_hd__ss_125C_3v00`). A caller
        # requesting `corner: "ss_125C_3v00"` and `interconnect_corner:
        # "max"` in the same request sees both values independently here,
        # never conflated.
        "interconnect_corner": interconnect_corner,
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
        # Additive field (issue #1826): see this function's own comment
        # above (right before `unrouted_def_path` is computed) for the
        # "when is this populated" contract.
        "unrouted_def_path": unrouted_def_path,
        "gds_path": gds_path,
        # Additive field (issue #1029): whether the DEF->GDS merge above
        # actually applied a KLayout LEF/DEF layer-map file, and how it was
        # resolved -- `_resolve_layer_map` silently degrades to no map at
        # all (`resolution: "none"`) when neither a variant-named nor a
        # family-level map file exists, which previously left no trace in
        # this response for a caller to notice. `null` unless
        # `stage_reached` is `"route"`, mirroring `gds_path`/`verilog_path`.
        "layer_map": layer_map_info,
        # Additive field (issue #1488): what the DEF->GDS merge's own
        # single-pin net-name marker pass did, so a caller can tell an
        # unrouted tie-cell-style net that *did* get its real DEF name back
        # from one that silently kept extraction's synthesized `$<id>`.
        # `single_pin_markers` counts the marker shapes synthesized;
        # `unresolved_single_pin_nets` names every single-pin net the pass
        # could not resolve pin geometry for (no layer map, an undeclared
        # LEF macro/pin, a pin centre not covered by drawn conductor) --
        # those keep the pre-#1488 fallback rather than failing the merge.
        # `null` unless `stage_reached` is `"route"`, mirroring `layer_map`.
        "def_net_names": def_net_names_info,
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
        # Additive field (issue #1091) -- see the construction comment above.
        "power": power_info,
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


def _validate_power(power: Any) -> dict[str, Any] | None:
    """Validate the optional ``request.power`` block (issue #1091) -- power
    delivery net names and PDN strap geometry, driving ``tapcell``/
    ``add_global_connection``/``global_connect``/``pdngen``/
    ``filler_placement`` -- see this module's own docstring "Power delivery"
    section. ``None`` (the default, field omitted) preserves this module's
    original v1 behavior byte-for-byte: no power-delivery Tcl is ever
    emitted, unchanged from before this field existed."""
    if power is None:
        return None
    if not isinstance(power, dict):
        raise PlaceAndRouteError("request.power must be a JSON object")

    power_net = power.get("power_net", "VDD")
    if not isinstance(power_net, str) or not power_net:
        raise PlaceAndRouteError(
            "request.power.power_net must be a non-empty string when given"
        )
    ground_net = power.get("ground_net", "VSS")
    if not isinstance(ground_net, str) or not ground_net:
        raise PlaceAndRouteError(
            "request.power.ground_net must be a non-empty string when given"
        )
    if power_net == ground_net:
        raise PlaceAndRouteError(
            "request.power.power_net and request.power.ground_net must differ"
        )

    straps = power.get("straps")
    if not isinstance(straps, list) or not straps:
        raise PlaceAndRouteError(
            "request.power.straps is required and must be a non-empty list"
        )

    validated_straps: list[dict[str, Any]] = []
    for i, strap in enumerate(straps):
        if not isinstance(strap, dict):
            raise PlaceAndRouteError(f"request.power.straps[{i}] must be an object")

        layer = strap.get("layer")
        if not isinstance(layer, str) or not layer:
            raise PlaceAndRouteError(
                f"request.power.straps[{i}].layer is required and must be a "
                "non-empty string"
            )

        width_um = strap.get("width_um")
        if (
            not isinstance(width_um, (int, float))
            or isinstance(width_um, bool)
            or width_um <= 0
        ):
            raise PlaceAndRouteError(
                f"request.power.straps[{i}].width_um is required and must be a "
                "positive number"
            )

        pitch_um = strap.get("pitch_um")
        if (
            not isinstance(pitch_um, (int, float))
            or isinstance(pitch_um, bool)
            or pitch_um <= 0
        ):
            raise PlaceAndRouteError(
                f"request.power.straps[{i}].pitch_um is required and must be a "
                "positive number"
            )

        offset_um = strap.get("offset_um", 0.0)
        if not isinstance(offset_um, (int, float)) or isinstance(offset_um, bool):
            raise PlaceAndRouteError(
                f"request.power.straps[{i}].offset_um must be a number when given"
            )

        # `spacing_um` (issue #1133) -> `add_pdn_stripe -spacing` -- the
        # paired power/ground stripe spacing on this strap's own layer, used
        # when a grid draws power and ground as an adjacent pair on a single
        # layer rather than on a single pitch (e.g. gf180's own
        # `pdn_grid_strategy_9t_6M.cfg`: `add_pdn_stripe -layer {Metal4}
        # -width {4.480} -spacing {0.56} -pitch {44.8} -offset {22.4}`).
        # Optional, `None` by default -- omitted entirely from the emitted
        # `add_pdn_stripe` call rather than defaulted, preserving this
        # module's prior Tcl byte-for-byte when not given. Deliberately not
        # required to have >= 2 straps: it describes this stripe's own
        # power/ground pairing, independent of any other strap.
        spacing_um = strap.get("spacing_um")
        if spacing_um is not None and (
            not isinstance(spacing_um, (int, float))
            or isinstance(spacing_um, bool)
            or spacing_um <= 0
        ):
            raise PlaceAndRouteError(
                f"request.power.straps[{i}].spacing_um must be a positive "
                "number when given"
            )

        followpins = strap.get("followpins", False)
        if not isinstance(followpins, bool):
            raise PlaceAndRouteError(
                f"request.power.straps[{i}].followpins must be a boolean when given"
            )

        validated_straps.append(
            {
                "layer": layer,
                "width_um": float(width_um),
                "pitch_um": float(pitch_um),
                "offset_um": float(offset_um),
                "spacing_um": float(spacing_um) if spacing_um is not None else None,
                "followpins": followpins,
            }
        )

    # `connects[]` (issue #1133) -> per-pair `add_pdn_connect` via-stack
    # tuning (`-max_columns`/`-ongrid`/`-split_cuts`) -- the platform's own
    # answer to a DRC question (cut splitting, on-grid landing, column
    # count) for the via stack between two strap layers. Optional; omitted
    # (the default, `[]`) preserves this module's prior bare
    # `add_pdn_connect -grid {grid} -layers {lower upper}` call for every
    # consecutive strap pair, byte-for-byte. Each entry's `layers` must name
    # one of `straps[]`'s own consecutive pairs, in that pair's own order --
    # this rejects a typo'd/nonexistent layer pair at validation time rather
    # than silently emitting a call this module never intended.
    connects = power.get("connects")
    validated_connects: list[dict[str, Any]] = []
    if connects is not None:
        if not isinstance(connects, list):
            raise PlaceAndRouteError("request.power.connects must be a list when given")

        strap_pairs = {
            (lower["layer"], upper["layer"])
            for lower, upper in zip(
                validated_straps, validated_straps[1:], strict=False
            )
        }
        seen_pairs: set[tuple[str, str]] = set()

        for i, connect in enumerate(connects):
            if not isinstance(connect, dict):
                raise PlaceAndRouteError(
                    f"request.power.connects[{i}] must be an object"
                )

            layers = connect.get("layers")
            if (
                not isinstance(layers, list)
                or len(layers) != 2
                or not all(isinstance(entry, str) and entry for entry in layers)
            ):
                raise PlaceAndRouteError(
                    f"request.power.connects[{i}].layers is required and must "
                    "be a 2-element list of non-empty strings"
                )
            pair = (layers[0], layers[1])
            if pair not in strap_pairs:
                raise PlaceAndRouteError(
                    f"request.power.connects[{i}].layers {list(pair)} does not "
                    "match any consecutive pair in request.power.straps"
                )
            if pair in seen_pairs:
                raise PlaceAndRouteError(
                    f"request.power.connects[{i}].layers {list(pair)} "
                    "duplicates an earlier request.power.connects entry"
                )
            seen_pairs.add(pair)

            max_columns = connect.get("max_columns")
            if max_columns is not None and (
                not isinstance(max_columns, int)
                or isinstance(max_columns, bool)
                or max_columns <= 0
            ):
                raise PlaceAndRouteError(
                    f"request.power.connects[{i}].max_columns must be a "
                    "positive integer when given"
                )

            ongrid = connect.get("ongrid")
            if ongrid is not None and (
                not isinstance(ongrid, list)
                or not ongrid
                or not all(isinstance(entry, str) and entry for entry in ongrid)
            ):
                raise PlaceAndRouteError(
                    f"request.power.connects[{i}].ongrid must be a non-empty "
                    "list of non-empty strings when given"
                )

            split_cuts = connect.get("split_cuts")
            if split_cuts is not None:
                if not isinstance(split_cuts, dict):
                    raise PlaceAndRouteError(
                        f"request.power.connects[{i}].split_cuts must be an "
                        "object when given"
                    )
                sc_layer = split_cuts.get("layer")
                if not isinstance(sc_layer, str) or not sc_layer:
                    raise PlaceAndRouteError(
                        f"request.power.connects[{i}].split_cuts.layer is "
                        "required and must be a non-empty string"
                    )
                sc_width_um = split_cuts.get("width_um")
                if (
                    not isinstance(sc_width_um, (int, float))
                    or isinstance(sc_width_um, bool)
                    or sc_width_um <= 0
                ):
                    raise PlaceAndRouteError(
                        f"request.power.connects[{i}].split_cuts.width_um is "
                        "required and must be a positive number"
                    )
                split_cuts = {"layer": sc_layer, "width_um": float(sc_width_um)}

            validated_connects.append(
                {
                    "layers": [pair[0], pair[1]],
                    "max_columns": max_columns,
                    "ongrid": list(ongrid) if ongrid is not None else None,
                    "split_cuts": split_cuts,
                }
            )

    return {
        "power_net": power_net,
        "ground_net": ground_net,
        "straps": validated_straps,
        "connects": validated_connects,
    }


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


def _blank_verilog_comments(text: str) -> list[str]:
    """``text``'s lines with every comment blanked out but **line numbering
    preserved** -- one output entry per input line, comment characters
    replaced by spaces rather than deleted.

    :func:`_strip_verilog_comments` deletes comments outright, which
    collapses the line structure a multi-line ``/* ... */`` spans and makes
    the result useless for reporting a line number.
    :func:`_reject_unsupported_netlist_constructs` needs to name the exact
    1-based netlist line its diagnostic points at (issue #1973 -- the whole
    point is to beat OpenSTA's own ``line N, syntax error`` to the punch),
    so it needs this shape instead.
    """
    out: list[str] = []
    in_block = False
    for line in text.splitlines():
        chars = list(line)
        i = 0
        while i < len(chars):
            if in_block:
                if line.startswith("*/", i):
                    chars[i] = chars[i + 1] = " "
                    i += 2
                    in_block = False
                    continue
                chars[i] = " "
                i += 1
            elif line.startswith("/*", i):
                chars[i] = chars[i + 1] = " "
                i += 2
                in_block = True
            elif line.startswith("//", i):
                for j in range(i, len(chars)):
                    chars[j] = " "
                break
            else:
                i += 1
        out.append("".join(chars))
    return out


#: A ``signed`` qualifier on a port/wire declaration in a structural-Verilog
#: netlist -- ``output signed [15:0] sample;``, ``wire signed [7:0] mid;``,
#: ``input wire signed [3:0] a;`` (issue #1973). Anchored to a preceding
#: declaration keyword on purpose: an expression-level ``$signed(...)`` cast
#: is accepted by every downstream reader (and is the RTL-side replacement
#: this repo's own ``docs/guides/digital-review/rtl-style-guide.md``
#: recommends for a ``signed`` port), so it must never be flagged -- and
#: ``$signed(`` is never immediately preceded by one of these keywords plus
#: whitespace.
_NETLIST_SIGNED_DECL_RE = re.compile(
    r"\b(?:input|output|inout|wire|reg|logic)\s+signed\b"
)

#: A bare ``x``-valued Verilog constant literal -- ``5'hxx``, ``2'bxx``,
#: ``1'bx``, ``8'shX0`` (issue #1973). Requires at least one ``x``/``X``
#: digit, so an ordinary resolved constant (``1'b0``, ``16'd100``,
#: ``8'hbe``) never matches -- only the unresolved bits OpenSTA turns into
#: an unroutable ``GROUND``-typed net.
_NETLIST_X_CONSTANT_RE = re.compile(
    r"\d+'[sS]?[bBoOdDhH][0-9a-fA-F_]*[xX][0-9a-fA-FxX_]*"
)


def _reject_unsupported_netlist_constructs(netlist_path: str) -> None:
    """Raise :class:`PlaceAndRouteError` naming the construct, the 1-based
    line number, and the offending line when ``netlist_path`` carries a
    ``signed`` port/wire qualifier or a bare ``x``-valued constant -- the
    two constructs that deterministically fail a real OpenROAD run several
    stages in, with a diagnostic pointing at a generated file the caller
    never wrote (issue #1973; see this module's docstring "Netlist
    pre-flight" section for the full rationale).

    Reports the **first** offending line in file order, whichever construct
    it is: both are already fatal, so there is nothing to gain from
    enumerating every occurrence, and the first one is the one a reader will
    go fix. A netlist this cannot read is not an error here --
    :func:`_resolve_netlist` has already established the file exists and is
    openable, and OpenROAD's own reader remains the authority on whether its
    contents parse at all.
    """
    try:
        with open(netlist_path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return

    for lineno, line in enumerate(_blank_verilog_comments(text), start=1):
        signed_match = _NETLIST_SIGNED_DECL_RE.search(line)
        if signed_match is not None:
            raise PlaceAndRouteError(
                f"netlist '{netlist_path}' line {lineno} declares a 'signed' "
                f"port/wire ('{signed_match.group(0)}'): {line.strip()} -- "
                "OpenSTA's Verilog reader (used by the 'floorplan' stage) "
                "rejects the 'signed' keyword outright with a syntax error. "
                "Re-synthesize with `klt synthesize`, which strips these "
                "declarations, or remove the qualifier from the netlist and "
                "use a `$signed(...)` cast in the RTL instead."
            )

        x_match = _NETLIST_X_CONSTANT_RE.search(line)
        if x_match is not None:
            raise PlaceAndRouteError(
                f"netlist '{netlist_path}' line {lineno} drives a bare "
                f"x-valued constant ('{x_match.group(0)}'): {line.strip()} -- "
                "OpenSTA reads it as a constant and builds an empty "
                "GROUND-typed net from it, which TritonRoute then refuses "
                "with DRT-0305 (after floorplan, placement and CTS have all "
                "already succeeded). Re-synthesize with `klt synthesize`, "
                "which resolves x bits to concrete tie-cell-driven constants "
                "(`setundef -zero` ahead of `hilomap`); a Verilog `function` "
                "with a dangling argument is the usual source in RTL."
            )


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


def _validate_constraints(
    constraints: Any,
) -> tuple[
    str | None,
    float | None,
    float | None,
    float | None,
    float | None,
    float | None,
    float | None,
]:
    """Validate ``request.constraints``.

    Returns ``(clock_port, clock_period_ns, max_transition_ns,
    max_capacitance_pf, max_fanout, input_delay_ns, output_delay_ns)``. The
    first two are the pre-existing clock fields -- required together,
    `None`/`None` when both are omitted (unchanged behavior). The three
    design-rule-constraint fields (issue #1709) are each independently
    optional regardless of clock presence: a caller may set e.g.
    `max_fanout` alone, or alongside the clock fields, or not at all --
    `None` per field when omitted, matching this function's own pre-existing
    "omitted preserves prior behavior exactly" convention.

    The last two (issue #1865) are the **I/O timing** constraints:
    `input_delay_ns` is the arrival time of every non-clock input port
    relative to the clock, `output_delay_ns` the required time at every
    output port. Each is independently optional -- but, unlike the
    design-rule fields, each *requires* `clock_port`/`clock_period_ns`,
    since `set_input_delay`/`set_output_delay` are both defined relative to
    a named clock and have no meaning without one.
    """
    if constraints is None:
        return None, None, None, None, None, None, None
    if not isinstance(constraints, dict):
        raise PlaceAndRouteError("request.constraints must be a JSON object")

    clock_port = constraints.get("clock_port")
    clock_period_ns = constraints.get("clock_period_ns")
    if clock_port is not None or clock_period_ns is not None:
        if not (isinstance(clock_port, str) and clock_port):
            raise PlaceAndRouteError(
                "request.constraints.clock_port must be a non-empty string"
            )
        if not (isinstance(clock_period_ns, (int, float)) and clock_period_ns > 0):
            raise PlaceAndRouteError(
                "request.constraints.clock_period_ns must be a positive number"
            )
        clock_period_ns = float(clock_period_ns)
    else:
        clock_port, clock_period_ns = None, None

    max_transition_ns = constraints.get("max_transition_ns")
    if max_transition_ns is not None:
        if not (
            isinstance(max_transition_ns, (int, float))
            and not isinstance(max_transition_ns, bool)
            and max_transition_ns > 0
        ):
            raise PlaceAndRouteError(
                "request.constraints.max_transition_ns must be a positive number"
            )
        max_transition_ns = float(max_transition_ns)

    max_capacitance_pf = constraints.get("max_capacitance_pf")
    if max_capacitance_pf is not None:
        if not (
            isinstance(max_capacitance_pf, (int, float))
            and not isinstance(max_capacitance_pf, bool)
            and max_capacitance_pf > 0
        ):
            raise PlaceAndRouteError(
                "request.constraints.max_capacitance_pf must be a positive number"
            )
        max_capacitance_pf = float(max_capacitance_pf)

    max_fanout = constraints.get("max_fanout")
    if max_fanout is not None:
        if not (
            isinstance(max_fanout, (int, float))
            and not isinstance(max_fanout, bool)
            and max_fanout > 0
        ):
            raise PlaceAndRouteError(
                "request.constraints.max_fanout must be a positive number"
            )
        max_fanout = float(max_fanout)

    # Issue #1865: I/O timing constraints. Both are plain non-negative
    # numbers (0 is a meaningful, commonly-written value -- "the port is
    # valid exactly at the clock edge" -- so unlike the design-rule fields
    # above these are `>= 0`, not `> 0`), and both are relative to
    # `clock_port`, so neither can be honoured without one.
    input_delay_ns = _validate_io_delay(constraints.get("input_delay_ns"), "input")
    output_delay_ns = _validate_io_delay(constraints.get("output_delay_ns"), "output")
    if (input_delay_ns is not None or output_delay_ns is not None) and (
        clock_port is None
    ):
        raise PlaceAndRouteError(
            "request.constraints.input_delay_ns/output_delay_ns require "
            "request.constraints.clock_port/clock_period_ns -- set_input_delay/"
            "set_output_delay are defined relative to a named clock"
        )

    return (
        clock_port,
        clock_period_ns,
        max_transition_ns,
        max_capacitance_pf,
        max_fanout,
        input_delay_ns,
        output_delay_ns,
    )


def _validate_io_delay(value: Any, which: str) -> float | None:
    """One of ``request.constraints.input_delay_ns``/``.output_delay_ns``
    (issue #1865): optional, and a non-negative number when given."""
    if value is None:
        return None
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or value < 0
        or value != value  # NaN
        or value in (float("inf"), float("-inf"))
    ):
        raise PlaceAndRouteError(
            f"request.constraints.{which}_delay_ns must be a non-negative number"
        )
    return float(value)


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
    Omitted/``None`` defaults to ``1``; ``0`` disables repair/reroute passes.
    See this module's own docstring, "Timing-driven
    global routing + bounded antenna-repair iteration"."""
    if value is None:
        return 1
    if isinstance(value, bool) or not isinstance(value, int):
        raise PlaceAndRouteError(
            "request.max_antenna_repair_iterations must be an integer"
        )
    if not (0 <= value <= _MAX_ANTENNA_REPAIR_ITERATIONS_CAP):
        raise PlaceAndRouteError(
            "request.max_antenna_repair_iterations must be between 0 and "
            f"{_MAX_ANTENNA_REPAIR_ITERATIONS_CAP}"
        )
    return value


def _validate_sweep_corners(value: Any) -> list[str] | None:
    """Optional ``request.pdk.sweep_corners`` (issue #1092) -- scopes the
    post-route multi-corner sweep (issue #949, :func:`_run_corner_sweep`) to
    a caller-named subset of the ``.lib`` corners ``request.pdk.cell_library``
    ships, instead of always sweeping every shipped corner
    (:func:`klayout_tools.pdk.list_lib_corners`'s own unfiltered return).

    A design has exactly one operating supply -- for a cell library shipping
    several (e.g. 15 ``.lib`` files spanning three nominal-supply families),
    the unscoped sweep is dominated by decks the design never runs at (this
    issue's own motivating gap). ``None`` (omitted, the default) reproduces
    today's "sweep everything the cell library ships" behaviour exactly --
    the backward-compatible default this field's own acceptance criteria
    require. An explicit ``[]`` is honoured literally (sweep zero corners --
    ``worst_setup_slack_ns``/``worst_hold_slack_ns``/``corners`` all degrade
    the same way a pre-``"route"`` run's do, via :func:`_run_corner_sweep`'s
    own empty-``corners``-list short-circuit), distinct from omitting the
    field entirely.

    Each requested name is validated against the *unfiltered* shipped-corner
    set once ``cell_library``/``pdk_info`` are resolved (see the ``"route"``
    stage branch in :func:`run_place_and_route`) -- an unknown name raises
    :class:`PlaceAndRouteError` naming every corner that *is* valid, matching
    this module's existing unresolvable-request-field convention (e.g.
    :func:`_validate_route_critical_nets_percentage`'s sibling validators).
    This function only validates *shape* (a list of non-empty strings);
    membership validation needs the resolved corner set and so cannot happen
    here.
    """
    if value is None:
        return None
    if not isinstance(value, list) or not all(
        isinstance(item, str) and item for item in value
    ):
        raise PlaceAndRouteError(
            "request.pdk.sweep_corners must be a list of non-empty strings"
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
    """Resolve ``(liberty_path, corner, pdk_info)`` for ``cell_library``.
    ``variant``/``root`` (the CLI's ``--pdk``/``--pdk-root`` flags, threaded
    through from :func:`run_place_and_route`) select a specific installed PDK
    variant/root exactly as :func:`klayout_tools.pdk.find_pdk` does; ``None``
    for either leaves that resolver's own default search order in effect.
    Raises :class:`PlaceAndRouteError` (never
    :class:`~klayout_tools.pdk.PdkNotFoundError`).

    Thin wrapper around :func:`klayout_tools.pdk.resolve_liberty_for_cell_library`
    (issue #1652) -- that shared implementation (including the IHP
    single-underscore liberty-filename fallback, issue #1790) is what
    ``synthesize.py`` and ``post_route_sta.py``'s own ``_resolve_liberty``
    wrappers call too, so all three modules stay in sync by construction;
    only the exception type raised on each failure differs per module.
    """
    return resolve_liberty_for_cell_library(
        cell_library,
        requested_corner,
        PlaceAndRouteError,
        variant=variant,
        root=root,
    )


def _resolve_lef(
    cell_library: str,
    pdk_info: dict[str, Any],
    interconnect_corner: str = "nom",
) -> tuple[str, str]:
    """Resolve the tech + merged-cell LEF pair for ``cell_library``, pinned
    to the *same* PDK install/variant :func:`_resolve_liberty` already
    resolved (never re-searches). ``interconnect_corner`` (issue #1100,
    ``request.pdk.interconnect_corner``, one of ``"min"``/``"nom"``/
    ``"max"``) selects which tech-LEF parasitic corner
    :func:`klayout_tools.pdk.lef_files` resolves; the default ``"nom"``
    preserves this function's original byte-identical behaviour. Raises
    :class:`PlaceAndRouteError` when either file is missing -- including
    when a caller-selected corner is not staged by the resolved PDK install
    (``lef_files``'s own ``None``-means-absent convention), never a silent
    fallback to nominal."""
    lefs = lef_files(
        cell_library,
        variant=pdk_info["variant"],
        root=pdk_info["root"],
        corner=interconnect_corner,
    )
    tech_lef = lefs["tech_lef"]
    cell_lef = lefs["cell_lef"]
    if tech_lef is None or cell_lef is None:
        missing = [
            name
            for name, path in (("tech_lef", tech_lef), ("cell_lef", cell_lef))
            if path is None
        ]
        detail = (
            f" (requested interconnect_corner '{interconnect_corner}')"
            if tech_lef is None
            else ""
        )
        raise PlaceAndRouteError(
            f"LEF not found for deck: standard-cell library '{cell_library}' "
            f"under resolved PDK install '{pdk_info['variant']}' at "
            f"'{pdk_info['root']}' is missing: {', '.join(missing)}{detail}"
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


def _design_rule_constraint_lines(
    max_transition_ns: float | None,
    max_capacitance_pf: float | None,
    max_fanout: float | None,
) -> list[str]:
    """``set_max_transition``/``set_max_capacitance``/``set_max_fanout`` on
    ``[current_design]`` -- issue #1709's ``request.constraints.
    max_transition_ns``/``.max_capacitance_pf``/``.max_fanout``, aiming the
    ``repair_design``/``repair_timing`` optimiser already present in the
    generated flow (see :func:`_stage_script_lines`'s ``"place"`` branch) at
    a caller-given design-rule target instead of only whatever limit the
    single ``read_liberty`` deck happens to declare.

    Mirrors :func:`_clock_lines`'s shape exactly: threaded through the same
    call sites, at the same point (immediately after ``_clock_lines``,
    before ``repair_design``/``repair_timing``), and each field emits its
    own line only when given -- a field omitted from
    ``request.constraints`` emits no corresponding line, so a request with
    none of the three set produces Tcl byte-identical to before this
    function existed.
    """
    lines = []
    if max_transition_ns is not None:
        lines.append(f"set_max_transition {max_transition_ns} [current_design]")
    if max_capacitance_pf is not None:
        lines.append(f"set_max_capacitance {max_capacitance_pf} [current_design]")
    if max_fanout is not None:
        lines.append(f"set_max_fanout {max_fanout} [current_design]")
    return lines


def _io_delay_lines(
    clock_port: str | None,
    input_delay_ns: float | None,
    output_delay_ns: float | None,
) -> list[str]:
    """``set_input_delay``/``set_output_delay`` -- issue #1865's
    ``request.constraints.input_delay_ns``/``.output_delay_ns``.

    Without these, a design whose only timing paths run *input port ->
    register* and *register -> output port* (a pipeline stage, a registered
    interface adapter, an IO/boundary block, the first slice of any design
    built bottom-up) has no constrained startpoint or endpoint at all: every
    slack field in this command's response degrades to OpenSTA's own
    unconstrained sentinel (``1e+39``), which is a *positive* number and so
    reads to a naive pass/fail gate as "timing closed with enormous margin"
    on a design that was never timed. ``create_clock`` alone -- the only
    constraint surface this command had before #1865 -- is enough only for a
    design whose paths are all register-to-register.

    A scalar applies to *every* port on that side of the design, which is
    the overwhelmingly common case for a boundary block (per-port maps are
    deliberately out of scope here -- see the issue). The non-clock input
    set is computed with the same plain-Tcl ``lsearch`` idiom
    OpenROAD-flow-scripts' own ``constraint.sdc`` templates use, rather than
    a ``remove_from_collection`` this repo has not verified against the
    OpenSTA builds it targets: ``all_inputs`` includes the clock port
    itself, and an arrival time on the clock port is not what a caller
    asking for "input delay" means.

    Mirrors :func:`_clock_lines`/:func:`_design_rule_constraint_lines`'s
    shape exactly: threaded through the same call sites, emitted
    immediately after them, and each field emits its own line(s) only when
    given -- a request with neither field set produces Tcl byte-identical
    to before this function existed. ``clock_port`` is never ``None`` when
    either delay is set (:func:`_validate_constraints` rejects that
    combination); the guard is defensive only.
    """
    if clock_port is None:
        return []
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
        # Issue #1826: the hold-side counterpart of `-setup` above, mirroring
        # the pair `_corner_sweep_script_lines` (`place_and_route_sta.py`)
        # already runs post-route -- captured into the same `-metrics` dump
        # as `timing__hold__ws`, alongside `-setup`'s own `timing__setup__ws`.
        # Extracted by `_extract_stage_metrics` into `nominal_hold_slack_ns`,
        # gated the same "absent before place" way `hold_violation_count`
        # already is; a distinct name from the existing, route-stage-only,
        # corner-swept `worst_hold_slack_ns` aggregate (issue #949) -- this
        # one is the single nominal-corner value, mirroring `worst_slack_ns`.
        "report_worst_slack_metric -hold",
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


def _design_rule_check_lines() -> list[str]:
    """Post-route **design-rule check** reports run inside the multi-corner
    sweep session
    (:func:`~klayout_tools.place_and_route_sta._corner_sweep_script_lines`)
    -- issue #1709's "Two smaller things found alongside" item 1, folded into
    that issue's own Builder scope by its 2026-09-15 revision: the run already
    re-times the routed design at the ``request.pdk.sweep_corners`` decks, so
    the max-transition / max-capacitance verdict at those same decks belongs in
    the report the run already writes rather than in a separate downstream
    tool three steps later.

    Two separately-delimited ``report_check_types ... -violators`` blocks
    (rather than one combined call) so :func:`_count_violations` can attribute
    a violation to the limit it actually broke -- the same marker convention
    :func:`_violation_count_lines` already uses to split its own
    ``-max_delay`` block from its ``-min_delay`` one, and counted by the same
    ``"(VIOLATED)"`` scrape for the same reason (OpenROAD ships no
    ``*_metric`` proc for a design-rule violation *count*, only the scalar
    slack metrics).

    ``-max_slew`` is OpenSTA's own name for the check
    ``set_max_transition``/the liberty's ``max_transition`` constrains -- the
    exact pair (``-max_slew`` + ``-max_capacitance``)
    OpenROAD-flow-scripts' own post-route ``report_metrics`` reporting uses.

    Deliberately **no** fanout report here. ``request.constraints.max_fanout``
    is still emitted as a *constraint* (:func:`_design_rule_constraint_lines`),
    but ``sta::max_fanout_violation_count`` takes OpenROAD down with a SIGSEGV
    inside ``sta::CheckFanouts::check`` on a library that declares no fanout
    limit at all (reproduced at every corner on ``26Q3-1510-g6cb3f2b704``,
    issue #1709) -- a class of library this repo explicitly supports. Fanout
    reporting, if it is ever wanted here, has to come from the topology walk
    (``get_pins -of_objects`` per net, counting inputs) that issue names as
    the safe-but-slower alternative, not from this call.
    """
    return [
        f'puts "{_MAX_TRANSITION_VIOLATIONS_BEGIN}"',
        "report_check_types -max_slew -violators",
        f'puts "{_MAX_TRANSITION_VIOLATIONS_END}"',
        f'puts "{_MAX_CAPACITANCE_VIOLATIONS_BEGIN}"',
        "report_check_types -max_capacitance -violators",
        f'puts "{_MAX_CAPACITANCE_VIOLATIONS_END}"',
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


def _pdn_connect_spec(
    power: dict[str, Any], lower_layer: str, upper_layer: str
) -> dict[str, Any]:
    """Returns the via-stack tuning actually applied to the
    ``add_pdn_connect`` between ``lower_layer`` and ``upper_layer`` (issue
    #1133): the caller's own ``power["connects"]`` entry for that pair when
    one was given (already validated by :func:`_validate_power`), or the
    default "no tuning" shape otherwise. Both :func:`_power_delivery_lines`
    (Tcl generation) and the response's own ``power.connects`` echo (see
    :func:`run_place_and_route`) build from this single lookup, so the two
    can never drift apart."""
    for connect in power.get("connects", []):
        if tuple(connect["layers"]) == (lower_layer, upper_layer):
            return connect
    return {
        "layers": [lower_layer, upper_layer],
        "max_columns": None,
        "ongrid": None,
        "split_cuts": None,
    }


def _pdn_connects_applied(power: dict[str, Any]) -> list[dict[str, Any]]:
    """The ``add_pdn_connect`` tuning actually applied for every consecutive
    strap pair (issue #1133) -- one entry per pair, in the same order
    :func:`_power_delivery_lines` emits them. Used to build the response's
    ``power.connects`` echo."""
    straps = power["straps"]
    return [
        _pdn_connect_spec(power, lower["layer"], upper["layer"])
        for lower, upper in zip(straps, straps[1:], strict=False)
    ]


def _power_delivery_lines(power: dict[str, Any], cell_library: str) -> list[str]:
    """Tcl for the optional ``request.power`` PDN stage (issue #1091):
    ``tapcell`` well/substrate ties, ``add_global_connection``/
    ``global_connect`` power-pin wiring, and ``pdngen``'s strap grid -- all
    real, verified OpenROAD Tcl commands (see this module's own docstring
    "Power delivery" section, and :data:`_TAPCELL_CELLS`/
    :data:`_POWER_PIN_PATTERNS`'s own docstrings for the exact source
    citations). Called once, at the end of the ``"floorplan"`` stage
    (immediately after ``place_macro``/``make_tracks``, before that stage's
    own ``write_db``) -- see :func:`_stage_script_lines`'s own ``"floorplan"``
    branch.

    Builds a single flat standard-cell PDN grid from ``power["straps"]``
    (already validated/normalized by :func:`_validate_power`): one
    ``add_pdn_stripe`` per strap (with ``-spacing`` when ``strap["spacing_um"]``
    was given -- issue #1133), ``add_pdn_connect`` between each consecutive
    pair (in the order the request lists them -- matching every real ORFS
    platform's own pdn config, whose stripes are always listed bottom-to-top
    with each pair connected to its immediate neighbor only; via-stack
    tuning -- ``-max_columns``/``-ongrid``/``-split_cuts`` -- comes from
    ``power["connects"]`` via :func:`_pdn_connect_spec` when the caller gave
    a matching entry, issue #1133), and a ``define_pdn_grid -pins`` naming
    the *last* (topmost) strap's own layer -- the layer a block-level
    caller's own P/G pins land on. Macro-specific PDN grids
    (``define_pdn_grid -macro``) are deliberately out of scope for this v1
    -- see the module docstring.
    """
    power_net = power["power_net"]
    ground_net = power["ground_net"]

    tap_master, endcap_master, distance_um = _TAPCELL_CELLS[cell_library]
    tapcell_call = f"tapcell -distance {distance_um} -tapcell_master {tap_master}"
    if endcap_master is not None:
        tapcell_call += f" -endcap_master {endcap_master}"
    lines = [tapcell_call]

    for net_role, pin_pattern, is_primary in _POWER_PIN_PATTERNS[cell_library]:
        net = power_net if net_role == "power" else ground_net
        flag = ""
        if is_primary:
            flag = " -power" if net_role == "power" else " -ground"
        lines.append(
            f"add_global_connection -net {{{net}}} -inst_pattern {{.*}} "
            f"-pin_pattern {{{pin_pattern}}}{flag}"
        )
    lines.append("global_connect")
    lines.append(
        f"set_voltage_domain -name {{CORE}} -power {{{power_net}}} "
        f"-ground {{{ground_net}}}"
    )

    straps = power["straps"]
    top_layer = straps[-1]["layer"]
    lines.append(
        f"define_pdn_grid -name {{grid}} -voltage_domains {{CORE}} "
        f"-pins {{{top_layer}}}"
    )
    for strap in straps:
        # Flag order (`-width` -> `-spacing` -> `-pitch` -> `-offset`)
        # matches real platform PDN configs verbatim -- e.g. gf180's own
        # `pdn_grid_strategy_9t_6M.cfg`: `add_pdn_stripe -layer {Metal4}
        # -width {4.480} -spacing {0.56} -pitch {44.8} -offset {22.4}`
        # (issue #1133).
        stripe_call = (
            f"add_pdn_stripe -grid {{grid}} -layer {{{strap['layer']}}} "
            f"-width {{{strap['width_um']}}}"
        )
        if strap["spacing_um"] is not None:
            stripe_call += f" -spacing {{{strap['spacing_um']}}}"
        stripe_call += (
            f" -pitch {{{strap['pitch_um']}}} -offset {{{strap['offset_um']}}}"
        )
        if strap["followpins"]:
            stripe_call += " -followpins"
        lines.append(stripe_call)
    for lower, upper in zip(straps, straps[1:], strict=False):
        connect_spec = _pdn_connect_spec(power, lower["layer"], upper["layer"])
        connect_call = (
            f"add_pdn_connect -grid {{grid}} "
            f"-layers {{{lower['layer']} {upper['layer']}}}"
        )
        if connect_spec["max_columns"] is not None:
            connect_call += f" -max_columns {{{connect_spec['max_columns']}}}"
        if connect_spec["ongrid"]:
            connect_call += f" -ongrid {{{' '.join(connect_spec['ongrid'])}}}"
        if connect_spec["split_cuts"] is not None:
            split_cuts = connect_spec["split_cuts"]
            connect_call += (
                f" -split_cuts {{{split_cuts['layer']} {split_cuts['width_um']}}}"
            )
        lines.append(connect_call)
    lines.append("pdngen")
    return lines


def _row_rail_lines(cell_library: str) -> list[str]:
    """Tcl for the ``request.power``-independent row-rail fallback (issue
    #1442) -- see :data:`_ROW_RAIL_STRAP`'s own docstring for the full root-
    cause analysis and source citation. Called once, at the very start of
    the ``"route"`` stage (before ``set_routing_layers``/``global_route``),
    but **only** when ``request.power`` was not given -- see
    :func:`_stage_script_lines`'s own ``"route"`` branch for the call site.

    Deliberately a small subset of :func:`_power_delivery_lines`: a single
    ``add_global_connection``/``global_connect`` pair (binding the library's
    own literal ``VPWR``/``VGND`` pins to like-named nets -- no caller-given
    ``power_net``/``ground_net`` exists here to alias to), a
    ``set_voltage_domain``, and one ``define_pdn_grid``/``add_pdn_stripe
    -followpins``/``pdngen`` -- no ``tapcell``, no vertical straps, no
    ``-macro`` grid. This is intentionally *not* a full PDN: it exists only
    to give the router a real row-rail obstruction, not to make a
    ``request.power``-less run electrically complete (a caller that wants
    real power delivery should still set ``request.power``). Once this rail
    exists, :func:`_stage_script_lines`'s own ``"route"`` branch also runs
    that same stage's ``filler_placement``/``global_connect`` pair (issue
    #1091's existing call, previously gated on ``request.power`` alone) --
    safe *because* the row rail this function draws already obstructs the
    router before `filler_placement`'s own instances ever land, live-
    verified (issue #1442's own PR description) to close every row gap
    with zero `merged_net_labels[]` power/signal shorts, unlike the naive
    "just run `filler_placement` unconditionally, with no row-rail
    obstruction" fix this issue exists to document as unsafe.

    ``define_pdn_grid`` deliberately omits ``-pins`` (unlike
    :func:`_power_delivery_lines`'s own call, which names its topmost strap
    layer): ``-pins`` marks that layer's grid boundary as a real block-level
    P/G interface, which ``pdngen`` then promotes into the routed DEF's own
    top-level ``PINS`` section -- confirmed live (issue #1442: an
    `openroad/orfs:latest` run with ``-pins {met1}`` grew the DEF's
    promoted-pin count from 52 to 54, adding `VPWR`/`VGND` as if they were
    real design ports; the identical run with ``-pins`` simply omitted still
    draws the same real ``SPECIALNETS`` row-rail shapes -- confirmed via the
    same DEF's own ``SPECIALNETS`` section -- with the DEF pin count
    unchanged at 52). This fallback exists only to obstruct the router, not
    to advertise a new top-level interface a caller never asked for.

    ``pdngen`` itself also takes a ``-dont_add_pins`` flag, passed here for
    the same reason and live-verified to matter independently of
    ``define_pdn_grid``'s own ``-pins`` above: even with ``-pins`` already
    omitted, a plain ``pdngen`` call still promotes ``VPWR``/``VGND`` into
    the design's own top-level Verilog port list -- confirmed live (issue
    #1442) by isolating the two calls against the same post-CTS checkpoint:
    `add_global_connection`/`global_connect` alone leaves `write_verilog`'s
    module port list untouched (`module gcd (clk, ..., result);`); adding
    `set_voltage_domain`/`define_pdn_grid`/`add_pdn_stripe`/`pdngen` (still
    with no `-pins`) grows it to `module gcd (clk, ..., result, VPWR,
    VGND);` -- a real regression this issue's own equivalence-check test
    suite (`tests/test_equiv.py`) caught (`yosys equivalence check failed:
    ERROR: Can't match gate port 'VGND_gate' to a gold port` -- the "gold"
    side is `klt synthesize`'s netlist, which never declares VPWR/VGND
    ports). `-dont_add_pins` suppresses exactly that Verilog-port
    promotion while leaving the physical `SPECIALNETS` row-rail shapes
    (confirmed identical DEF `SPECIALNETS` section either way) and the
    router obstruction they provide completely unaffected -- this fallback
    only ever needed the physical shapes, never a new logical port.
    """
    layer, width_um, pitch_um, offset_um, power_pin, ground_pin = _ROW_RAIL_STRAP[
        cell_library
    ]
    return [
        f"add_global_connection -net {{{power_pin}}} -inst_pattern {{.*}} "
        f"-pin_pattern {{{power_pin}}} -power",
        f"add_global_connection -net {{{ground_pin}}} -inst_pattern {{.*}} "
        f"-pin_pattern {{{ground_pin}}} -ground",
        "global_connect",
        f"set_voltage_domain -name {{CORE}} -power {{{power_pin}}} "
        f"-ground {{{ground_pin}}}",
        "define_pdn_grid -name {row_rail} -voltage_domains {CORE}",
        f"add_pdn_stripe -grid {{row_rail}} -layer {{{layer}}} "
        f"-width {{{width_um}}} -pitch {{{pitch_um}}} -offset {{{offset_um}}} "
        "-followpins",
        "pdngen -dont_add_pins",
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
    power: dict[str, Any] | None,
    clock_port: str | None,
    clock_period_ns: float | None,
    max_transition_ns: float | None,
    max_capacitance_pf: float | None,
    max_fanout: float | None,
    input_delay_ns: float | None,
    output_delay_ns: float | None,
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
        lines += _design_rule_constraint_lines(
            max_transition_ns, max_capacitance_pf, max_fanout
        )
        lines += _io_delay_lines(clock_port, input_delay_ns, output_delay_ns)
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
        # `request.power` (issue #1091): tapcell + PDN Tcl, right after
        # `place_macro`/`make_tracks` and before this stage's own
        # `write_db` -- the same insertion point OpenROAD-flow-scripts
        # itself uses (see this module's own docstring "Power delivery"
        # section for the live-verified citation).
        if power is not None:
            lines += _power_delivery_lines(power, cell_library)
        lines += _metrics_report_lines(include_fmax=False, include_power=False)
        lines += [f"write_db {checkpoint_out}"]
        return lines

    assert checkpoint_in is not None  # every non-floorplan stage has a checkpoint
    # `read_db` already restores a linked network (built by the floorplan
    # stage's own `link_design`), so `create_clock` may safely follow
    # `read_liberty` directly here -- unlike the floorplan stage above.
    lines = [f"read_db {checkpoint_in}", f"read_liberty {liberty_path}"]
    lines += _clock_lines(clock_port, clock_period_ns)
    lines += _design_rule_constraint_lines(
        max_transition_ns, max_capacitance_pf, max_fanout
    )
    lines += _io_delay_lines(clock_port, input_delay_ns, output_delay_ns)

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
        # Guaranteed non-None here: the request validator (`run_place_and_
        # route`) already rejects `target_stage in {"place", "cts",
        # "route"}` with `clock_port is None` before this generator ever
        # runs -- see its own `stage_index >= STAGE_ORDER.index("place")`
        # check.
        assert clock_port is not None
        buf_cell = _CTS_BUFFER_CELLS[cell_library]
        lines += [
            f"set_wire_rc -layer {io_spec['layer_v']}",
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
            # no-op and continue -- matching this module's own existing
            # precedent of leaning on OpenROAD/OpenSTA as the authority
            # rather than re-deriving what it already knows (see this
            # module's own docstring, "Macro-pin routability cross-check").
            f"set _klt_cts_seq_sinks [llength [all_registers -clock "
            f"[get_clocks {{{clock_port}}}]]]",
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
            f'puts "klt place-and-route: clock {clock_port} has no '
            "sequential (registered) fanout -- skipping "
            'clock_tree_synthesis (see issue #1506)"',
            "}",
            # `report_clock_skew_metric -setup` below (unconditional,
            # `_metrics_report_lines(include_clock_skew=True)`) is safe to
            # run either way -- live-verified: it does not error when no
            # clock tree was built in the `else` branch above, it simply
            # reports on the (unbuffered) ideal clock same as it would if
            # this stage never ran at all.
            "detailed_placement",
        ]
        # A post-CTS, still-unrouted DEF (issue #1826, reversing #785's own
        # "internal artifact only" decision for the `"place"`-stage DEF
        # below -- this one is surfaced in the response from the start,
        # never internal-only). Written unconditionally, exactly like the
        # `"place"`-stage `place_def_path` write below -- `run_place_and_
        # route` only threads it into `unrouted_def_path` when
        # `target_stage` is `"cts"` itself (mirroring `def_path`'s own
        # "only when this is the actual target" convention), but the file
        # always exists on disk once this stage runs, the same "no dead
        # branch" reasoning `place_def_path` already established.
        cts_def_path = os.path.join(output_dir, f"{hdl_toplevel}.cts.def")
        lines += [f"write_def {cts_def_path}"]
    else:  # stage == "route"
        routing_range = _ROUTING_LAYER_RANGE[cell_library]
        diode_cell = _ANTENNA_DIODE_CELLS[cell_library][0]
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
        # Row-rail fallback (issue #1442): only when `request.power` was
        # *not* given -- a `request.power`-bearing run already got a real
        # PDN (including, when the caller's own `straps[]` name a met1
        # layer, a real row rail) at the end of the `"floorplan"` stage,
        # before placement/CTS/routing ever ran, so there is no obstruction
        # gap left to close here. Emitted before `set_routing_layers`/
        # `global_route` -- the router must see this as a pre-existing
        # obstruction, not something drawn after the fact. See
        # `_row_rail_lines`/`_ROW_RAIL_STRAP`'s own docstrings for why this
        # is unconditional on `request.power` (unlike every other PDN-
        # related call in this module) and why it is deliberately scoped to
        # `cell_library`s with a verified :data:`_ROW_RAIL_STRAP` entry only.
        row_rail_active = power is None and cell_library in _ROW_RAIL_STRAP
        if row_rail_active:
            lines += _row_rail_lines(cell_library)
        lines += [
            f"set_routing_layers -signal {routing_range}",
            global_route_call,
        ]
        lines += detailed_route_lines(
            output_dir, hdl_toplevel, seed, 0, max_antenna_repair_iterations
        )
        # Post-route antenna repair (survey section 2.7/3.3): inserting a
        # diode instance on a violating net changes that net's routing,
        # so -- mirroring ORFS's own `flow/scripts/detail_route.tcl`,
        # which re-runs `detailed_route` immediately after
        # `repair_antennas` to route/legalize each new diode instance --
        # this repeats the `repair_antennas`/`detailed_route` pair
        # `max_antenna_repair_iterations` times (issue #939; default `1`,
        # keeping the original single repair+reroute pass). `repair_antennas`
        # itself is never called with `-iterations`
        # here -- OpenROAD's own `GlobalRouter.cpp` explicitly warns against
        # `-iterations != 1` once `detailed_route` has already run, exactly
        # this stage's own call pattern (see the module docstring for the
        # full citation) -- so the bounded multi-pass behaviour is built at
        # the flow level instead, mirroring ORFS's own opt-in
        # `MAX_REPAIR_ANTENNAS_ITER_DRT` loop shape (unset, and therefore
        # inactive/single-pass, in ORFS's own default flow).
        # `check_antennas` then reports the post-repair violation count
        # unconditionally, exactly as ORFS's own flow does.
        for pass_index in range(1, max_antenna_repair_iterations + 1):
            lines += [f"repair_antennas {diode_cell}"]
            lines += detailed_route_lines(
                output_dir,
                hdl_toplevel,
                seed,
                pass_index,
                max_antenna_repair_iterations,
            )
        lines += _antenna_check_lines()
        # `request.power` (issue #1091): gap-filler cell insertion, right
        # after the antenna-repair loop above (mirroring ORFS's own
        # `flow/scripts/fillcell.tcl`, which likewise runs immediately
        # after detailed routing/antenna repair) and before this stage's
        # own final `write_def` -- closing every row gap `repair_antennas`'s
        # own diode insertions (or ordinary placement) may have left. The
        # `global_connect` re-run right after it wires the new filler
        # instances' own PG pins to the connection rules
        # `_power_delivery_lines` already registered during the
        # `"floorplan"` stage -- mirroring ORFS's own
        # `flow/scripts/final_connect.tcl`, whose own comment states
        # exactly why: "Ensure all OR created (rsz/cts) instances are
        # connected". Fillers carry no signal nets, so this insertion point
        # (before the parasitics re-estimate below) does not affect any
        # signal-net RC.
        #
        # Issue #1442: `row_rail_active` runs this same `filler_placement`/
        # `global_connect` pair too, using the row-rail fallback's own
        # `add_global_connection` rules (`_row_rail_lines`) instead of
        # `request.power`'s -- safe *because* the row-rail obstruction above
        # now precedes `global_route`, unlike the naive "just run
        # `filler_placement` unconditionally" fix this issue's own
        # investigation found shorts signal nets to `VPWR`/`VGND` (no prior
        # obstruction meant the router could freely cross a row gap on
        # `met1`, then a filler cell's own PG strap would land on top of
        # that signal route). Live-verified against the real
        # `openroad/orfs:latest` toolchain (issue #1442's own PR
        # description carries the transcript): row-rail-only leaves
        # `tests/corpus/place_and_route/gcd.gds.gz`'s own
        # `nwell.width.1`/`nwell.space.1` violations open (the row gaps
        # this fallback deliberately does not fill), while row-rail +
        # `filler_placement` closes them (0 `klt drc` violations) with
        # zero `klt extract` `merged_net_labels[]` power/signal shorts
        # either way.
        if power is not None or row_rail_active:
            filler_masters = " ".join(_FILLER_CELLS[cell_library])
            lines += [f"filler_placement {{{filler_masters}}}", "global_connect"]
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
        # - `-remove_cells <cells>`: `null` (omitted) unless this stage
        #   actually inserted a physical-only instance -- either
        #   `request.power`'s own `tapcell`/`filler_placement` calls above,
        #   or (issue #1442) the row-rail fallback's own `filler_placement`
        #   call, active whenever `request.power` was omitted on a
        #   `cell_library` the fallback covers. Either way, this flag exists
        #   because those instances would otherwise widen the divergence
        #   from `klt synthesize`'s own netlist (which never contains
        #   them) -- ORFS's own `flow/scripts/final_outputs.tcl` strips the
        #   equivalent set via `-remove_cells [find_physical_only_masters]`,
        #   but that proc is defined only inside ORFS's own utility scripts,
        #   never sourced by this module (see this module's own docstring:
        #   "nothing in this module shells out to, reads, or requires an
        #   ORFS checkout"). This module instead passes the same master
        #   names its own `_TAPCELL_CELLS`/`_FILLER_CELLS` tables already
        #   name explicitly -- a literal, deterministic list rather than a
        #   dependency on an external proc. Antenna diodes
        #   (`repair_antennas`) are genuine logical instances (not
        #   physical-only) and are always kept, either way.
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
        write_verilog_call = f"write_verilog {verilog_path}"
        # `-remove_cells` (see above): also strips the row-rail fallback's
        # own `filler_placement` instances (issue #1442) -- that path never
        # inserts a tapcell/endcap (the fallback deliberately carries no
        # `tapcell` call), so its own physical-only set is just the filler
        # masters, unlike `request.power`'s tapcell+endcap+filler set below.
        physical_only_masters: list[str] = []
        if power is not None:
            tap_master, endcap_master, _distance_um = _TAPCELL_CELLS[cell_library]
            physical_only_masters.append(tap_master)
            if endcap_master is not None:
                physical_only_masters.append(endcap_master)
            physical_only_masters += list(_FILLER_CELLS[cell_library])
        elif row_rail_active:
            physical_only_masters += list(_FILLER_CELLS[cell_library])
        if physical_only_masters:
            removed = " ".join(physical_only_masters)
            write_verilog_call += f" -remove_cells {{{removed}}}"
        lines += [f"write_def {def_path}", write_verilog_call]
    elif stage == "place":
        # A placement-only DEF, written as a side artifact alongside the
        # `write_db` checkpoint below. Originally added purely so an
        # out-of-band caller -- the FLUTE/RUDY-family congestion pre-check
        # (`klayout_tools.congestion`, issue #785, Epic #700 Phase 1 §3.6)
        # -- could read real post-placement cell/pin geometry via
        # `klayout.db`'s DEF parser (mirroring `_merge_def_to_gds`'s own use
        # of it) without needing the far more expensive `route` stage to
        # have run first; #785 deliberately kept this path internal-only
        # (not part of the public request/response contract). Issue #1826
        # reverses that scoping decision: `run_place_and_route` now surfaces
        # this same deterministic path as `unrouted_def_path` whenever
        # `target_stage` is `"place"` itself -- see that function's own
        # comment for why -- so a caller can get a pre-route, SDC-driven
        # `klt sta` result (via `klt sta`'s own `geometry_source` request
        # field) without a full route. The path itself
        # (`<output_dir>/<hdl_toplevel>.place.def`) is unchanged, so any
        # out-of-band caller that already located it directly (as #785
        # intended) keeps working unmodified.
        place_def_path = os.path.join(output_dir, f"{hdl_toplevel}.place.def")
        lines += [f"write_def {place_def_path}"]

    lines += [f"write_db {checkpoint_out}"]
    return lines


#: Matches the "can't read/open a file" *phrase* in an OpenROAD/Tcl error
#: line, deliberately loose on wording -- issue #1868 has observed both
#: ``cannot read file <path>.`` and ``couldn't open "<path>": ...``, and
#: OpenROAD's own diagnostics interpose extra words (``cannot open LEF file
#: <path>``). Matched against one line at a time; :data:`_ABS_PATH_RE` then
#: pulls the actual path out of a line this matches, rather than trying to
#: encode both the phrase and the path shape in one pattern.
_UNREADABLE_FILE_PHRASE_RE = re.compile(
    r"(?:cannot|can[' ]?t|couldn[' ]?t|could\s+not)\s+(?:read|open)\b",
    re.IGNORECASE,
)

#: The first absolute-path-shaped token on a line -- used only after
#: :data:`_UNREADABLE_FILE_PHRASE_RE` has already matched that line, so a
#: bare ``/`` prefix is a reliable enough signal without also anchoring the
#: surrounding phrase.
_ABS_PATH_RE = re.compile(r"\"?(/[^\s\"]+)")


def _engine_error_message(
    stage: str,
    completed: subprocess.CompletedProcess,
    *,
    pdk_info: dict[str, Any] | None = None,
) -> str:
    """Build an actionable error message from a failed per-stage OpenROAD
    run.

    Prefers a bracketed ``[ERROR ...]`` diagnostic OpenROAD itself printed
    -- on either stream, first occurrence -- over a bare ``Error:`` trailer
    line (just ``<script>.tcl, <line> <code>``, no message text) that
    reliably follows it on a Tcl-level failure at ``-exit`` (issue #1079).
    Only when no bracketed diagnostic is present do we fall back to the last
    bare ``Error:`` line, on either stream (mirrors ``synthesize.py``'s
    ``_synthesis_error_message``).

    ``DRT-0305`` (a constant-tie net reaching TritonRoute) is additionally
    *diagnosed* rather than passed through -- see
    :func:`_constant_tie_diagnosis`.

    ``pdk_info`` -- when the caller has one (``run_place_and_route`` always
    does, once liberty/LEF resolution has happened) -- feeds
    :func:`_mount_namespace_hint`, appended as a further ``--`` clause when
    the failure looks like the container-wrapper mount gap issue #1868
    describes: ``openroad`` couldn't read a file this process just resolved
    and can itself still read. ``pdk_info`` is ``None`` in the existing
    ``DRT-0305``/bracket/trailer unit tests, which keep their pre-#1868
    messages unchanged.
    """
    diagnosis = _constant_tie_diagnosis(completed)
    if diagnosis is not None:
        error_line, hint = diagnosis
        return f"openroad '{stage}' stage failed: {error_line} -- {hint}"

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
        message = f"openroad '{stage}' stage failed: {bracket_lines[0]}"
    elif bare_error_lines:
        message = f"openroad '{stage}' stage failed: {bare_error_lines[-1]}"
    else:
        tail_source = (completed.stderr or completed.stdout or "").strip().splitlines()
        snippet = " ".join(tail_source[-3:]) if tail_source else "no output captured"
        message = (
            f"openroad '{stage}' stage exited with code "
            f"{completed.returncode}: {snippet}"
        )

    hint = _mount_namespace_hint(completed, pdk_info)
    if hint is not None:
        message = f"{message} -- {hint}"
    return message


def _mount_namespace_hint(
    completed: subprocess.CompletedProcess, pdk_info: dict[str, Any] | None
) -> str | None:
    """``None``, or an actionable hint that ``openroad`` and this process do
    not share a filesystem view (issue #1868).

    ``klt place-and-route`` resolves every liberty/LEF path on the **host**
    (via :func:`_resolve_liberty`/:func:`_resolve_lef`, backed by
    :mod:`klayout_tools.pdk`'s ``find_pdk``) and bakes the resulting absolute
    paths into the generated Tcl handed to an ``openroad`` **subprocess** --
    which, per ``docs/cli/place-and-route.md``'s own documented, CI-used
    install path, is very often actually a wrapper script that runs a
    container per invocation (``scripts/install-openroad-docker.sh``). A
    container only sees the host paths its wrapper explicitly bind-mounted,
    so a PDK discovered via a search root (``~/.volare``, ciel, open_pdks)
    rather than an explicitly-set ``$PDK_ROOT`` silently falls outside that
    wrapper's mount set -- see that script's own header comment.

    The result is an OpenROAD/Tcl "can't read/open file <path>" failure that,
    read alone, looks identical to a genuinely missing or unreadable file.
    The cheap, general signal that distinguishes the two: **this** process
    can still read that exact path (it is the one that resolved it in the
    first place). When that holds, the failure is a mount/namespace gap, not
    a missing file -- worth saying so, and worth surfacing the resolved PDK
    root/``resolved_via`` alongside it when the path is a PDK asset, so a
    reader sees ``resolved_via`` next to the unreadable path immediately
    rather than having to separately run ``klt pdk find``.

    This generalizes beyond PDK assets on purpose (issue #1868's own closing
    note): any absolute path outside ``$PWD``/``$PDK_ROOT`` that a request
    can reference -- an out-of-tree netlist, an ``--abstract-cell-lef``, a
    macro LEF given by absolute path -- hits the identical hazard, and
    ``pdk_info`` being ``None`` (or the path not living under its root) just
    means the hint is generic rather than PDK-specific.
    """
    path: str | None = None
    for stream in (completed.stdout or "", completed.stderr or ""):
        for line in stream.splitlines():
            if not _UNREADABLE_FILE_PHRASE_RE.search(line):
                continue
            path_match = _ABS_PATH_RE.search(line)
            if path_match is not None:
                path = path_match.group(1).rstrip(".,;:'\"")
                break
        if path is not None:
            break
    if path is None:
        return None

    try:
        readable = os.path.isfile(path) and os.access(path, os.R_OK)
    except OSError:
        readable = False
    if not readable:
        return None

    pdk_note = ""
    if pdk_info is not None and path.startswith(pdk_info.get("root", "\0")):
        pdk_note = (
            f" (resolved PDK root: '{pdk_info['root']}', "
            f"{pdk_info['resolved_via']}; $PDK_ROOT="
            f"{os.environ.get('PDK_ROOT') or '(unset)'})"
        )

    return (
        f"'openroad' could not read '{path}', but this process can read it"
        f"{pdk_note} -- if 'openroad' is a container/wrapper invocation, its "
        "mounts likely do not cover that path (see docs/cli/place-and-route.md's "
        "'Installing OpenROAD' section)"
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

    **Coverage correction (issue #1973).** Until #1973, the hint below said
    ``hilomap`` alone covered this -- which was only true for concrete
    ``1'b0``/``1'b1`` literals. ``hilomap`` does not recognise an ``x`` bit
    as a constant to map at all, so an ``x``-valued literal (a Verilog
    ``function``'s dangling argument wire is the usual source) survived
    synthesis and produced this exact ``DRT-0305`` anyway. ``klt synthesize``
    now runs ``setundef -zero`` **before** ``hilomap``, resolving every
    ``x`` bit to a concrete ``0`` that ``hilomap`` then tie-cell-maps
    identically to a literal constant -- so the hint's claim now holds for
    ``x`` too, for any ``cell_library`` in the tie-cell table. A
    ``cell_library`` *not* in that table still gets neither pass, which is
    why the hint keeps its "for every standard-cell library in its tie-cell
    table" qualifier and its hand-edit fallback.

    Reaching this diagnosis at all now implies the netlist did **not** come
    from a tie-cell-table ``klt synthesize`` run: a bare constant of either
    kind in a caller-supplied netlist is rejected up front by
    :func:`_reject_unsupported_netlist_constructs` (the ``x`` case) or was
    already mapped (the ``0``/``1`` case). This stays as the last line of
    defense for the cases neither covers -- an unmapped ``1'b0``/``1'b1``
    literal, or a constant OpenROAD materialises from something other than a
    literal in the Verilog text.
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
        f"via yosys's `setundef -zero` + `hilomap` passes (which together "
        f"cover x-valued bits as well as 1'b0/1'b1 literals) for every "
        f"standard-cell library in its tie-cell table -- or hand-edit the "
        f"netlist to instantiate tie-high/tie-low cells before "
        f"place-and-route.",
    )


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


def _read_metrics(metrics_path: str, stage: str) -> dict[str, Any]:
    if not os.path.isfile(metrics_path):
        raise PlaceAndRouteError(
            f"openroad exited successfully but did not produce the expected "
            f"'{stage}' stage metrics file '{metrics_path}'"
        )
    try:
        with open(metrics_path, encoding="utf-8") as handle:
            data = json.load(
                handle,
                object_pairs_hook=route_metrics_object if stage == "route" else dict,
            )
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
    corners: list[dict[str, Any]] | None = None,
    max_transition_violation_count: int | None = None,
    max_capacitance_violation_count: int | None = None,
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
        # Issue #1826: the single, nominal-corner hold WNS, populated from
        # the same `"place"` stage onward as `hold_violation_count` above --
        # a real slack-in-ns margin, not just the pass/fail count that field
        # already provides. Named `nominal_hold_slack_ns` (not
        # `worst_hold_slack_ns`) to avoid colliding with that existing,
        # `"route"`-stage-only, corner-swept aggregate (issue #949) a few
        # lines below -- this field never replaces it, the same way
        # `worst_slack_ns` above is untouched by `worst_setup_slack_ns`.
        nominal_hold_slack = metrics.get("timing__hold__ws")
        if nominal_hold_slack is not None:
            entry["nominal_hold_slack_ns"] = round(nominal_hold_slack, 5)
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
        # Issue #1092: the per-corner setup/hold slack breakdown backing the
        # two aggregates above -- `is not None` (not truthiness) so an
        # explicit `request.pdk.sweep_corners: []` (sweep zero corners)
        # reports `corners: []`, distinct from the `None`/absent value every
        # pre-`"route"` stage reports.
        if corners is not None:
            entry["corners"] = corners
        # Issue #1709: the design-rule-check verdict at those same swept
        # corners -- absent (never present-but-null) on every stage but
        # `"route"`, exactly like the two slack aggregates above, since the
        # corner-sweep invocation is the only session that loads the swept
        # decks' own max-transition/max-capacitance limits. `is not None`
        # (not truthiness) so a genuinely clean run reports an explicit `0`
        # rather than dropping the field, the same way
        # `route_drc_violation_count` already does.
        if max_transition_violation_count is not None:
            entry["max_transition_violation_count"] = max_transition_violation_count
        if max_capacitance_violation_count is not None:
            entry["max_capacitance_violation_count"] = max_capacitance_violation_count

    # Issue #1865: whether the slack fields above are measurements at all.
    # OpenSTA reports the worst slack of a design with no constrained
    # startpoint/endpoint as `1e+39` -- a *positive* number, which a naive
    # `worst_slack_ns >= 0` gate reads as "timing closed with enormous
    # margin" on a design that was never timed. This field states the
    # difference mechanically, so no consumer has to special-case `1e+39` by
    # value. Computed over every slack field this stage actually reported
    # (TNS is excluded deliberately: an unconstrained run reports `0` there,
    # which is indistinguishable by value from a genuinely clean one).
    entry["timing_status"] = _timing_status(
        (
            entry.get("worst_slack_ns"),
            entry.get("nominal_hold_slack_ns"),
            entry.get("worst_setup_slack_ns"),
            entry.get("worst_hold_slack_ns"),
        )
    )

    return entry


__all__ = [
    "SCHEMA_VERSION",
    "SUPPORTED_ENGINES",
    "STAGE_ORDER",
    "PlaceAndRouteError",
    "load_request",
    "run_place_and_route",
    "def_pin_names",
]

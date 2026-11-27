"""Cell-library reference tables used by placement, routing and request validation."""

from __future__ import annotations

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
#: exactly the guess this table's docstring convention exists to avoid. It
#: is instead listed in :data:`_NO_TAPCELL_LIBRARIES` below (issue #2441),
#: which records "this library verifiably has no tap/endcap masters" as its
#: own explicit, cited fact -- so `request.power` skips *only* the `tapcell`
#: Tcl line for it and runs the rest of the power-delivery sequence
#: normally, rather than raising. A plain absence from *both* tables still
#: means "no tapcell masters have been verified for this library yet" and
#: still raises. A `klt place-and-route` run that never sets
#: `request.power`, or that reaches only the
#: `"floorplan"`/`"place"`/`"cts"`/`"route"` stages without it, is
#: unaffected -- none of those paths consult this table.
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


#: Standard-cell libraries that verifiably ship **no** tap/endcap cell
#: masters at all (issue #2441). A `request.power` run against one of these
#: emits no `tapcell` Tcl line -- and nothing else changes: the
#: `add_global_connection`/`global_connect` wiring, the
#: `define_pdn_grid`/`add_pdn_stripe`/`add_pdn_connect`/`pdngen` grid, and
#: the `"route"`-stage `filler_placement` call all run exactly as they do
#: for a library with tapcells (nothing in those steps depends on `tapcell`
#: having run first).
#:
#: This is deliberately an explicit allowlist and **not** a "missing from
#: :data:`_TAPCELL_CELLS` => skip tapcell" fallback. The two states look
#: identical in that table (no entry) but mean opposite things:
#:
#: - *in neither table* -- nobody has verified this library's tapcell
#:   masters yet. `request.power` keeps raising the existing "no tapcell
#:   master known" error, so a library added to the sibling tables before
#:   its tapcell masters are known fails loudly instead of silently
#:   skipping the well-tie step a caller asked for.
#: - *listed here* -- the library's own PDK states it has no such cells, so
#:   skipping `tapcell` is the *correct* Tcl for it, not a degraded
#:   fallback.
#:
#: `sg13g2_stdcell` (issues #1784, #2441) -- IHP-Open-PDK v0.3.0's own
#: LibreLane platform config says so outright; see
#: :data:`_TAPCELL_CELLS`'s own docstring above for the two citations
#: (`libs.tech/librelane/sg13g2_stdcell/config.tcl`'s `"There are no endcap
#: and welltie cells in ihp-sg13g2"` comment, and the sibling
#: `libs.tech/librelane/config.tcl`'s `FP_TAPCELL_DIST 0`). What makes
#: *skipping* the right answer rather than *refusing* is the rest of that
#: same sibling config, read 2026-09-23 from a fetched IHP-Open-PDK v0.3.0
#: install: it configures a full PDN grid for this library unconditionally,
#: independent of the no-tapcell posture --
#: `PDN_RAIL_LAYER Metal1`/`PDN_RAIL_OFFSET 0`,
#: `PDN_VERTICAL_LAYER TopMetal1`/`PDN_HORIZONTAL_LAYER TopMetal2`,
#: `PDN_{V,H}WIDTH 2.2`/`PDN_{V,H}SPACING 4.0`/`PDN_{V,H}PITCH 75.6`/
#: `PDN_{V,H}OFFSET 13.6` (plus `PDN_CORE_RING_*` defaults, which
#: `request.power` has no field for -- core rings are a documented v1
#: exclusion, see :func:`_power_delivery_lines`). IHP's own reference flow
#: therefore builds a real multi-layer PDN for this library *without*
#: `tapcell`/well-ties -- not no PDN at all.
#:
#: Verification status, stated plainly: the same bar as the entries in
#: :data:`_TAPCELL_CELLS`/:data:`_FILLER_CELLS`. This path was driven
#: through a real `openroad 26Q3-1278-g4421880472` (`openroad/orfs:latest`)
#: against a real fetched IHP-Open-PDK v0.3.0 install on 2026-09-24 --
#: `request.power` with the `Metal1` followpin rail plus the
#: `TopMetal1`/`TopMetal2` strap pair above completed floorplan through a
#: full detailed route with `route_drc_violation_count: 0`,
#: `power.pdn: true`, `tapcell_master`/`endcap_master` both `null`, 2343
#: filler instances and **0** tapcell/endcap instances placed, and real
#: `VDD`/`VSS` `SPECIALNETS` spanning `Metal1`..`TopMetal2`. See
#: `docs/cli/place-and-route.md`'s "Live verification" section for that
#: run's own numbers, including what a `klt drc --deck sg13g2` pass over
#: its GDS does and does not report.
_NO_TAPCELL_LIBRARIES: frozenset[str] = frozenset({"sg13g2_stdcell"})


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
    "sg13g2_stdcell": ("sg13g2_fill_1", "sg13g2_fill_2"),
}

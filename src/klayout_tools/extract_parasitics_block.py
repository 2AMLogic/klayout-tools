"""``klt extract --parasitics`` block (issue #5196, stage 4).

Moved verbatim out of ``run_extract``: the lumped-RC post-processing that runs
when parasitics were extracted (``--mom-net`` cross-check,
``--substrate-spreading``, ``--mom-rlc-net`` override, ``_inject_parasitics``
and the additive ``parasitics_report[...]`` fields). Returns the
``parasitics_report`` (``None`` when parasitics were not requested).

``warnings`` is mutated in place, exactly as the inline block did. The helpers
it calls that still live in ``extract.py`` are imported inside the function
body: ``extract.py`` imports this module at its top level, so a module-level
``from .extract import ...`` would be a circular import.
"""

from __future__ import annotations

from typing import Any


def build_parasitics_report(
    *,
    circuit: Any,
    critical_nets: Any,
    critical_nets_set: Any,
    deck: Any,
    deck_name: Any,
    distributed_rc: Any,
    emit_selected_resistance: Any,
    mom_crosscheck: Any,
    mom_net: Any,
    mom_rlc_capacitance_ff: Any,
    mom_rlc_inductance_nh: Any,
    mom_rlc_net: Any,
    mom_rlc_resistance_ohm: Any,
    net_selectors: Any,
    parasitic_nets: Any,
    parasitics_deck: Any,
    parasitics_nets: Any,
    parasitics_nets_set: Any,
    parasitics_top_cell_only: Any,
    resistance_path_max_nodes: Any,
    resistance_path_max_resistors: Any,
    resistance_paths: Any,
    subcircuit_cell: Any,
    substrate_spreading: Any,
    substrate_tie_requested: Any,
    variants: Any,
    warnings: Any,
) -> dict[str, Any] | None:
    import klayout.db as kdb

    from .extract import (
        PARASITIC_MODEL_SCOPE,
        _attach_resistance_paths,
        _mom_ground_entry_for_crosscheck,
        _report_net_selectors,
        _selects_critical,
        substrate_tie_mode,
        substrate_tie_written_ohms,
    )
    from .extract_parasitics import (
        PARASITICS_MODEL_REVISION,
        _inject_parasitics,
        spice_safe_net_name,
    )
    from .extract_report import (
        _describe_parasitics_metal_gaps,
        _describe_parasitics_overlap_gaps,
    )
    from .extract_spef import ExtractError
    from .parasitic_integrity import empty_integrity_summary

    mom_crosscheck_report: dict[str, Any] | None = None
    parasitics_report: dict[str, Any] | None = None
    if parasitic_nets is not None:
        ground_nets, coupled_pairs = parasitic_nets
        # `--mom-net` (issue #798): swap this one net's lumped-RC ground
        # capacitance for `klt mom`'s field-solved value *before*
        # `_inject_parasitics` reads `ground_nets` below, so both the
        # written SPICE `C` card and `parasitics.nets[]` for this net carry
        # the MoM value -- "extracted via klt mom instead of lumped RC" per
        # the epic's own Phase 1b acceptance criterion, not merely reported
        # alongside it. Every other net's entry is untouched. Validated
        # above (an unresolvable net or a solve with no metal-role geometry
        # already raised `ExtractError`), so `mom_crosscheck` here is always
        # a dict with a non-`None` `mom_capacitance_ff` when `mom_net` was
        # given.
        #
        # The entry to swap is resolved from the solve's own `net_id`, not by
        # re-matching the `--mom-net` *name* against `ground_nets` a second
        # time (issue #811) -- see `_mom_ground_entry_for_crosscheck`. A
        # layout label is not a net identity, so two by-name lookups over two
        # differently-ordered net lists are not guaranteed to select the same
        # island; keying on the id the solve already carries removes the
        # question.
        if mom_net is not None:
            assert mom_crosscheck is not None
            matched_entry = _mom_ground_entry_for_crosscheck(
                ground_nets, mom_crosscheck
            )
            if matched_entry is None:
                raise ExtractError(
                    f"--mom-net {mom_net!r} matches no net with "
                    "ground-eligible parasitics geometry in this layout"
                )
            lumped_rc_capacitance_ff = matched_entry["capacitance_ff"]
            mom_capacitance_ff = mom_crosscheck["mom_capacitance_ff"]
            matched_entry["capacitance_ff"] = mom_capacitance_ff
            delta_ff = mom_capacitance_ff - lumped_rc_capacitance_ff
            mom_crosscheck_report = {
                "net": mom_net,
                "net_id": mom_crosscheck["net_id"],
                "lumped_rc_capacitance_ff": round(lumped_rc_capacitance_ff, 6),
                "mom_capacitance_ff": round(mom_capacitance_ff, 6),
                "delta_ff": round(delta_ff, 6),
                "delta_pct": (
                    round(100.0 * delta_ff / lumped_rc_capacitance_ff, 3)
                    if lumped_rc_capacitance_ff
                    else None
                ),
                "background_permittivity": mom_crosscheck["background_permittivity"],
                "panel_size_um": mom_crosscheck["panel_size_um"],
                "panel_count": mom_crosscheck["panel_count"],
                "ground_pad_factor": mom_crosscheck["ground_pad_factor"],
                "method": (
                    "klt mom (Method of Moments) two-conductor solve between "
                    "this net's own metal-role geometry and a synthesized "
                    "ground plate, derived from the deck's own lumped-RC "
                    "coefficient table -- see docs/cli/extract.md's "
                    "'--mom-net' section for the full derivation and "
                    "reproduction steps."
                ),
                "warnings": list(mom_crosscheck["warnings"]),
            }
            warnings.extend(mom_crosscheck["warnings"])
        # `--mom-rlc-net` (issue #988, Epic #709 Phase 3a): substitute a
        # caller-supplied R/L/C for one named net's Phase 1/2 lumped-RC
        # model *before* `_inject_parasitics` reads `ground_nets` below --
        # see `run_extract`'s own `mom_rlc_net` docstring paragraph. Unlike
        # `--mom-net` above, the entry to swap is resolved by *name*
        # (matching `--critical-net`'s own convention, not `--mom-net`'s
        # `net_id`-keyed one): there is no single solved net object here to
        # key on, since the R/L/C values are opaque caller input, not the
        # output of a solve this function itself ran against one specific
        # net object. Every distinct net object sharing this net name (e.g.
        # several un-strapped islands with the same layout label) is
        # substituted the same way.
        mom_rlc_override_report: dict[str, Any] | None = None
        if mom_rlc_net is not None:
            matched_entries = [e for e in ground_nets if e["net"] == mom_rlc_net]
            if not matched_entries:
                raise ExtractError(
                    f"--mom-rlc-net {mom_rlc_net!r} matches no net with "
                    "ground-eligible parasitics geometry in this layout"
                )
            previous_resistance_ohm = sum(e["resistance_ohm"] for e in matched_entries)
            previous_capacitance_ff = sum(e["capacitance_ff"] for e in matched_entries)
            for matched_entry in matched_entries:
                if mom_rlc_resistance_ohm is not None:
                    matched_entry["resistance_ohm"] = mom_rlc_resistance_ohm
                    matched_entry["mom_rlc_r_override"] = True
                if mom_rlc_capacitance_ff is not None:
                    matched_entry["capacitance_ff"] = mom_rlc_capacitance_ff
            mom_rlc_override_report = {
                "net": mom_rlc_net,
                "matched_net_count": len(matched_entries),
                "previous_resistance_ohm": round(previous_resistance_ohm, 4),
                "previous_capacitance_ff": round(previous_capacitance_ff, 6),
                "resistance_ohm": mom_rlc_resistance_ohm,
                "capacitance_ff": mom_rlc_capacitance_ff,
                "inductance_nh": mom_rlc_inductance_nh,
                "method": (
                    "caller-supplied value (e.g. a separate `klt mom` "
                    "Method-of-Moments solve against this net's real "
                    "geometry, Epic #701) substituted verbatim for this "
                    "net's Phase 1/2 lumped-RC ground model -- see "
                    "docs/cli/extract.md's '--mom-rlc-net' section."
                ),
            }
        if circuit is not None and (ground_nets or coupled_pairs):
            ground_net = deck.substrate_net
            parasitics_report = _inject_parasitics(
                kdb,
                circuit,
                ground_nets,
                coupled_pairs,
                ground_net,
                distributed_rc_nets=(critical_nets_set if distributed_rc else None),
                substrate_tie_ohms=substrate_tie_requested,
                mom_rlc_inductor=(
                    (mom_rlc_net, mom_rlc_inductance_nh)
                    if mom_rlc_net is not None and mom_rlc_inductance_nh is not None
                    else None
                ),
                # `--subcircuit` (issue #2245) only: tag each injected node
                # with the original net it descends from, so the slice below
                # can attribute every parasitic element to one electrical net.
                # `False` (every other run) writes no property at all.
                record_slice_topology=subcircuit_cell is not None,
                variants=variants,
                emit_selected_resistance=emit_selected_resistance,
            )
        else:
            parasitics_report = {
                "r_count": 0,
                "c_count": 0,
                "cc_count": 0,
                "l_count": 0,
                "total_resistance_ohm": 0.0,
                "total_capacitance_ff": 0.0,
                "total_coupling_capacitance_ff": 0.0,
                "total_inductance_nh": 0.0,
                "nets": [],
                # Additive field (issue #1263). This branch injected nothing
                # into the circuit at all, so there is no substrate node for
                # a DC tie to anchor either -- the block is still present
                # (schema stability), just empty.
                "substrate_dc_tie": {
                    "resistance_ohm": substrate_tie_written_ohms(
                        substrate_tie_requested
                    ),
                    "mode": substrate_tie_mode(substrate_tie_requested),
                    "nets": [],
                },
                # Additive field (issue #2764): no nets injected, so the
                # integrity summary reports an all-zero check -- same shape
                # as the checked branch's summary, per the schema-stability
                # convention above.
                "integrity": empty_integrity_summary(),
            }
        # `parasitics_deck` is only non-None when `--parasitics` was given
        # (see above), which is exactly when `parasitic_nets is not None`.
        assert parasitics_deck is not None
        metal_gaps = _describe_parasitics_metal_gaps(deck, parasitics_deck)
        parasitics_report["metals_without_coefficient"] = metal_gaps
        if metal_gaps:
            levels = ", ".join(f"Metal{gap['metal_index'] + 1}" for gap in metal_gaps)
            warnings.append(
                f"'{deck_name}' deck's PARASITICS.metals has no R/C "
                f"coefficient for {levels} -- --parasitics reports zero "
                "resistance and capacitance for that metal level on every "
                "net, understating the true value. See docs/cli/extract.md's "
                "'Parasitic (RC) extraction' section."
            )
        # Additive field (issue #760): the `metals_without_coefficient`-style
        # gap report for the vertical-overlap coupling coefficient family --
        # see `_describe_parasitics_overlap_gaps`'s docstring.
        overlap_gaps = _describe_parasitics_overlap_gaps(deck, parasitics_deck)
        parasitics_report["overlap_pairs_without_coefficient"] = overlap_gaps
        if overlap_gaps:
            pairs = ", ".join(
                f"Metal{gap['lower_metal_index'] + 1}/"
                f"Metal{gap['upper_metal_index'] + 1}"
                for gap in overlap_gaps
            )
            warnings.append(
                f"'{deck_name}' deck's PARASITICS.metal_overlaps has no "
                f"vertical-overlap coupling coefficient for {pairs} -- "
                "--parasitics reports zero net-to-net coupling capacitance "
                "for that adjacent metal-level pair, understating the true "
                "value (the corresponding area still charges to ground, "
                "unlike a pair with a curated coefficient). See "
                "docs/cli/extract.md's 'Parasitic (RC) extraction' section."
            )
        # Additive field (issue #976): echoes the `--critical-net` request
        # back verbatim (as given, not sorted/deduplicated) -- `[]` when the
        # flag was never given. A name matching no net with ground-eligible
        # parasitics geometry is not an error (a caller may name several
        # candidate nets across several blocks/runs) -- flagged in
        # `warnings` instead, mirroring `--pins`' "declared name matched no
        # promoted net" convention.
        parasitics_report["critical_nets"] = (
            sorted(critical_nets_set or ())
            if _selects_critical(net_selectors)
            else (list(critical_nets) if critical_nets else [])
        )
        # Additive field (issue #1700): echoes the `--parasitics-net` request
        # back verbatim (as given, not sorted/deduplicated) -- `[]` when the
        # flag was never given. Without it a scoped run's deliberately-short
        # `parasitics.nets[]` would be indistinguishable from a layout whose
        # other nets genuinely had no ground-eligible geometry.
        parasitics_report["parasitics_nets"] = (
            sorted(parasitics_nets_set or ())
            if net_selectors is not None and net_selectors.parasitics_patterns
            else (list(parasitics_nets) if parasitics_nets else [])
        )
        # Additive fields (issue #2974): the requested selectors, present
        # only when one was given, plus their zero-match warnings.
        _report_net_selectors(net_selectors, parasitics_report, warnings)
        if variants is not None:
            # Additive field (issue #3056): present only when
            # `--parasitic-variant` was given, so every other report is
            # byte-identical to before the flag existed.
            parasitics_report["parasitic_variant"] = list(variants.raw)
            matched_variant_nets = {
                spice_safe_net_name(entry["net"]) for entry in ground_nets
            }
            unmatched_variant = sorted(variants.named_nets() - matched_variant_nets)
            if unmatched_variant:
                warnings.append(
                    "--parasitic-variant net name(s) "
                    f"{', '.join(repr(name) for name in unmatched_variant)} "
                    "match no net with ground-eligible parasitics geometry "
                    "in this layout -- nothing was dropped for them. See "
                    "docs/cli/extract.md's '--parasitic-variant' section."
                )
            distributed_variant = sorted(
                {
                    n["net"]
                    for n in parasitics_report.get("nets", [])
                    if n.get("rc_model") == "distributed"
                    and (
                        variants.drops_r(n["net"]) or variants.drops_c_ground(n["net"])
                    )
                }
            )
            if distributed_variant:
                warnings.append(
                    "--parasitic-variant drop-r/drop-c-ground does not apply "
                    "to --distributed-rc net(s) "
                    f"{', '.join(repr(name) for name in distributed_variant)} "
                    "-- their ladder is left unchanged."
                )
        if parasitics_nets_set:
            # Same escaped-spelling comparison `critical_nets` makes below
            # (issue #1162): `ground_nets[].net` is the unescaped identity
            # spelling, the caller-supplied set is the escaped one.
            scoped_matched_names = {
                spice_safe_net_name(entry["net"]) for entry in ground_nets
            }
            unscoped = sorted(parasitics_nets_set - scoped_matched_names)
            if unscoped:
                warnings.append(
                    "--parasitics-net name(s) "
                    f"{', '.join(repr(name) for name in unscoped)} match no "
                    "net with ground-eligible parasitics geometry in this "
                    "layout -- no R/C was computed for them. See "
                    "docs/cli/extract.md's '--parasitics-net' section."
                )
        if critical_nets_set:
            # `ground_nets[].net` is the unescaped identity spelling (issue
            # #1162, see `_net_identity_name`'s docstring); `critical_nets_set`
            # is caller-supplied and named using the escaped spelling this
            # module reports everywhere else, so the comparison set below
            # re-escapes each entry.
            matched_net_names = {
                spice_safe_net_name(entry["net"]) for entry in ground_nets
            }
            # `--parasitics-net` (issue #1700) scoped the ground pass that
            # `matched_net_names` is derived from, so a critical net left out
            # of that scope is absent for a reason the generic "matches no
            # net in this layout" wording below would misattribute to the
            # layout. Reported separately, and excluded from that warning, so
            # the caller is pointed at the flag combination actually
            # responsible. Empty (and therefore inert) unless the new flag
            # was given.
            scoped_out_critical = (
                sorted(critical_nets_set - parasitics_nets_set)
                if parasitics_nets_set
                else []
            )
            if scoped_out_critical:
                warnings.append(
                    "--critical-net name(s) "
                    f"{', '.join(repr(name) for name in scoped_out_critical)} "
                    "were not also named --parasitics-net -- that flag scopes "
                    "the ground R/C pass lateral coupling is measured from, "
                    "so no lateral coupling was computed for them. See "
                    "docs/cli/extract.md's '--parasitics-net' section."
                )
            unmatched = sorted(
                critical_nets_set - matched_net_names - set(scoped_out_critical)
            )
            if unmatched:
                warnings.append(
                    "--critical-net name(s) "
                    f"{', '.join(repr(name) for name in unmatched)} match no "
                    "net with ground-eligible parasitics geometry in this "
                    "layout -- lateral coupling was not computed for "
                    "them. See docs/cli/extract.md's '--critical-net' "
                    "section."
                )
            if not any(parasitics_deck.metal_sidewalls):
                warnings.append(
                    f"'{deck_name}' deck's PARASITICS.metal_sidewalls curates "
                    "no lateral (same-layer) coupling coefficient for any "
                    "metal level -- --critical-net reports zero lateral "
                    "coupling capacitance for every requested net. See "
                    "docs/cli/extract.md's '--critical-net' section."
                )
        # Additive field (issue #977): `True` only when `--distributed-rc`
        # was given (always `False` otherwise, `--critical-net`-only runs
        # included) -- distinguishes "lateral coupling only" (Phase 2a) runs
        # from "lateral coupling plus distributed RC" (Phase 2b) runs
        # without a caller having to inspect individual `nets[].rc_model`
        # entries.
        parasitics_report["distributed_rc"] = bool(distributed_rc)
        if distributed_rc:
            assert critical_nets_set is not None  # validated above
            distributed_net_names = {
                entry["net"]
                for entry in parasitics_report["nets"]
                if entry.get("rc_model") == "distributed"
            }
            fell_back = sorted(
                (critical_nets_set & matched_net_names) - distributed_net_names
            )
            if fell_back:
                warnings.append(
                    "--distributed-rc name(s) "
                    f"{', '.join(repr(name) for name in fell_back)} matched "
                    "a net with fewer than 2 device terminals -- kept the "
                    "star/Gamma-shunt model for them instead of a "
                    "distributed ladder (nothing to chain). See "
                    "docs/cli/extract.md's '--distributed-rc' section."
                )
        # Additive field (issue #2765): present only when --resistance-path
        # was given; a no-op otherwise.
        _attach_resistance_paths(
            parasitics_report,
            resistance_paths,
            resistance_path_max_nodes,
            resistance_path_max_resistors,
        )
        # Additive field (issue #1704): `True` only when
        # `--parasitics-top-cell-only` was given -- distinguishes a run whose
        # `nets[].resistance_ohm_top_cell`/`.capacitance_ff_top_cell` are
        # real computed splits from one where they are `None` because the
        # flag was never asked for.
        parasitics_report["top_cell_only"] = bool(parasitics_top_cell_only)
        # Additive field (issue #728, updated by #760, #976): declares the
        # parasitic model's own scope machine-readably (net-to-ground
        # capacitance plus vertical-overlap net-to-net coupling, plus
        # `--critical-net`-scoped lateral coupling; single lumped series
        # resistance per net; quasi-static) -- present on every
        # `parasitics` block regardless of whether any net actually carried
        # non-zero parasitics, since the model's scope does not depend on
        # what was found. See `PARASITIC_MODEL_SCOPE`'s docstring.
        parasitics_report["model"] = dict(PARASITIC_MODEL_SCOPE)
        # Additive field (issue #2957): the extraction model's own revision,
        # so a consumer can tell a model change from a mere tool bump.
        parasitics_report["model_revision"] = PARASITICS_MODEL_REVISION
        # Additive field (issue #798): `None` unless `--mom-net` was given,
        # in which case it is the swap-and-measure report built above -- see
        # `run_extract`'s `mom_net` docstring paragraph and
        # `docs/cli/extract.md`'s `--mom-net` section.
        parasitics_report["mom_crosscheck"] = mom_crosscheck_report
        # Additive field (issue #2561): `None` unless `--substrate-spreading`
        # was given, in which case it is the two-terminal closed-form
        # estimate between that net's substrate-tap contacts. Deliberately a
        # sibling of `substrate_dc_tie` rather than a replacement for it --
        # the DC tie is still what the written SPICE carries; this block is
        # the reported *magnitude* of the impedance that model leaves out.
        # See `docs/cli/extract.md`'s `--substrate-spreading` section.
        parasitics_report["substrate_spreading"] = substrate_spreading
        # Additive field (issue #988, Epic #709 Phase 3a): `None` unless
        # `--mom-rlc-net` was given, in which case it is the substitution
        # report built above -- see `run_extract`'s `mom_rlc_net` docstring
        # paragraph and `docs/cli/extract.md`'s `--mom-rlc-net` section.
        parasitics_report["mom_rlc_override"] = mom_rlc_override_report

    return parasitics_report

"""Final report assembly for :func:`klayout_tools.lvs.run_lvs` (issue #5162)."""

from __future__ import annotations

from typing import Any


def assemble_lvs_report(
    *,
    mismatches: Any,
    status: Any,
    engine: Any,
    engine_version: Any,
    netgen_binary: Any,
    layout_deck_name: Any,
    layout_hash_source: Any,
    layout_input_role: Any,
    layout_circuit: Any,
    layout_abstracted_cells: Any,
    reference_circuit: Any,
    reference_echo: Any,
    reference_form: Any,
    reference_spec: Any,
    reference_pdk_info: Any,
    reference_pin_order_sources: Any,
    reference_pin_order_files: Any,
    reference_netlist_path: Any,
    extracted_netlist_path: Any,
    equivalent_pins_applied: Any,
    constant_bindings: Any,
    power_connectivity: Any,
    body_verification: Any,
    device_parameter_coverage: Any,
    counts: Any,
    device_classes: Any,
    net_correspondence: Any,
    portable_paths: Any,
    request_dir: Any,
    opts: Any,
) -> dict[str, Any]:
    """Final stage of :func:`run_lvs` (issue #5162): tally ``mismatches[]``
    per category, build provenance, assemble the report and apply the
    ``portable_paths`` rewrite. Pure extraction -- no logic of its own."""
    # Deferred: these helpers live in ``lvs`` (which imports this module).
    from . import env_provenance
    from ._provenance import build_provenance, sha256_file
    from .decks import deck_source_path
    from .lvs import (
        SCHEMA_VERSION,
        _compare_timeout_replay_options,
        _constant_nets_echo,
        _finish_response,
        _label_layers_echo,
        _layout_abstraction_echo,
        _reference_conversion_echo,
    )
    from .verilog_module_names import reference_module_names

    category_counts: dict[str, int] = {}
    # Issue #1132: `category_counts` alone collapses `error`/`warning`
    # entries of the same category into one number (e.g. `"topology": 8`
    # could be six warnings and two errors), so a caller cannot gate on
    # "any error present" without re-reading every `mismatches[]` entry.
    # `category_error_counts` mirrors `category_counts`'s shape (same keys,
    # sorted, additive) but counts only `severity: "error"` entries per
    # category -- a category with zero errors (e.g. an all-`warning`
    # `topology.flattened`) is simply absent, matching `category_counts`'s
    # own "no entries of this category at all" convention of omitting the
    # key rather than reporting 0.
    category_error_counts: dict[str, int] = {}
    for mismatch in mismatches:
        category_counts[mismatch["category"]] = (
            category_counts.get(mismatch["category"], 0) + 1
        )
        if mismatch["severity"] == "error":
            category_error_counts[mismatch["category"]] = (
                category_error_counts.get(mismatch["category"], 0) + 1
            )

    provenance = build_provenance(
        deck_name=layout_deck_name,
        deck_path=(deck_source_path(layout_deck_name) if layout_deck_name else None),
        # Issue #1901: populated only for a `gate-level-verilog`
        # reference whose `reference.pdk`/`reference.pdk_root` resolved
        # a PDK (see `reference_pdk_info` above) -- `None` for a plain
        # SPICE-vs-SPICE reference, which genuinely involves no PDK.
        pdk=reference_pdk_info,
        # Issue #1969: pin the layout side under `provenance.input`, the
        # shared block's own field, for *both* engines (this call is
        # reached after the `klayout`/`netgen` branch converges, and
        # `layout_hash_source` is resolved before it). This deliberately
        # reverses issue #331's original call: `environment.layout_sha256`
        # above records the same digest of the same file (both go through
        # `sha256_file`), but only under an LVS-specific key no generic
        # consumer reads. `klt signoff --manifest`'s T1 item-4 staleness
        # gate reads `provenance.input.content_hash` generically across
        # every kind, so leaving this `null` made *every* content-hash-
        # pinned "LVS clean" citation render `stale_evidence` -- a pinned
        # hash can never match `None`. `environment.layout_sha256` is a
        # bare hex digest and stays exactly as it was (a report-shape
        # contract of its own); `provenance.input.content_hash` is the
        # `sha256:`-prefixed form, so the two are redundant in content
        # but not interchangeable in shape.
        input_path=layout_hash_source,
        # Issue #2027: and say *what* that hash is of -- `"layout"` for
        # the `layout.file` shape (the original stream, the same bytes
        # `klt drc` hashes), `"netlist"` for the pre-extracted
        # `layout.netlist` shape. Without this discriminator `klt
        # signoff` compared a pre-extracted run's SPICE digest against a
        # DRC report's layout digest and refused to aggregate a
        # perfectly consistent pair (the repo's own `examples/signoff/`
        # pair reproduced it).
        input_role=layout_input_role,
        # Issue #600: echo the resolved `layout.deck_options` mapping
        # under `provenance.deck.options`, matching `klt extract`'s
        # shape exactly (`_deck_block` omits the key entirely when
        # `deck_options` is `None`/empty).
        deck_options=opts.deck_options,
        # Issue #2394: ...and, identically to `klt extract`, record the
        # *fully resolved* option set (the deck's own defaults for every
        # key `layout.deck_options` did not pin) plus `options_explicit`/
        # `options_hash`. `layout_deck_name` is `None` for the
        # pre-extracted `layout.netlist` shape, where `_deck_block`
        # returns `None` and this has no effect.
        resolve_deck_options=True,
        include_klayout_version_mismatch=True,
    )
    if reference_pin_order_sources:
        # Issue #2693: *which* pin-order sources this compare's cell pin
        # orders came from -- one record per file read, in read order. A
        # macro-backed `gate-level-verilog` reference resolves several
        # (`sky130_fd_sc_hd`'s aggregate library file plus one file per
        # instantiated SRAM macro), so `provenance.pdk` alone cannot answer
        # "where did this cell's pin order come from". Omitted entirely for
        # every other `reference.form`, which reads no pin-order source at
        # all -- never a spuriously present empty list.
        provenance["pin_order_sources"] = reference_pin_order_sources

    report = {
        "schema_version": SCHEMA_VERSION,
        "engine": engine,
        # Issue #2659: the committable `{path, scope}` shape, built from the
        # *resolved* layout path (`layout_hash_source` -- the file
        # `provenance.input.content_hash` below actually hashes), never the
        # request's own cwd-dependent echo. See `env_provenance.repo_relative_path`.
        # Issue #4984: `reference` takes the same shape, built from the
        # resolved path `environment.reference_sha256` hashes.
        "layout": env_provenance.repo_relative_path(
            layout_hash_source,
            repo_root=env_provenance.repo_root_for(layout_hash_source),
        ),
        # Issue #2686: the applied `layout.label_layers` override (which GDS
        # purpose inline extraction read net/pin names from, per role) --
        # see `_label_layers_echo`. Absent when no override was applied, so
        # those reports stay byte-identical to before.
        **_label_layers_echo(opts.label_layers),
        # Issue #4347: the applied `layout.def_net_names`/`def_pins` DEF-name
        # options. Absent when not requested (byte-identical reports).
        **({"def_net_names": True} if opts.def_net_names else {}),
        **({"def_pins": opts.def_pins_echo} if opts.def_pins_echo is not None else {}),
        # Issue #2853: the applied `layout.abstract_cells`/
        # `abstract_cell_lef` black-box abstraction and its per-cell report
        # -- see `_layout_abstraction_echo`. Absent when no abstraction was
        # requested, so those reports stay byte-identical to before.
        **_layout_abstraction_echo(opts.layout_abstraction, layout_abstracted_cells),
        "reference": reference_echo,
        "top": layout_circuit.name,
        # Issue #1205: the *reference* side's resolved top circuit name,
        # alongside the layout side's `top` above. The two are equal for the
        # ordinary compare, but differ by construction for an LVS negative
        # control (a deliberately-broken `<cell>_shorted` layout compared
        # against the intact `<cell>`'s reference netlist). Recording only
        # one of them made such a report unreconstructable by `--check
        # --rerun`, which applied the single `top` to both sides and failed
        # with "top cell/subcircuit not found in reference netlist" -- see
        # `_reconstruct_lvs_request`.
        "reference_top": reference_circuit.name,
        # Issue #2673: the reference-conversion context `--rerun` needs to
        # replay a `reference.form: "subckt-call"` reference -- see
        # `_reference_conversion_echo`. Absent for every other form, so those
        # reports stay byte-identical to before.
        **_reference_conversion_echo(
            reference_form, reference_spec, reference_pin_order_files
        ),
        # Issue #589: the effective `options.parameter_tolerance`, echoed so a
        # consumer reading only the response can tell whether a `"match"` was
        # reached under a caller-supplied design tolerance at all. `null` when
        # the option was omitted (today's exact-compare behaviour).
        "parameter_tolerance": opts.parameter_tolerance,
        # Issue #1205: every request option that shapes *what was compared*,
        # echoed as resolved, so a committed report round-trips back into the
        # request it came from (`_reconstruct_lvs_request`) instead of being
        # re-run as a differently-shaped compare whose difference is then
        # reported as drift. Keys are always present with their effective
        # values (never omitted), matching `parameter_tolerance`'s own
        # always-present discipline -- `parameter_tolerance` is repeated here
        # so this block is a complete request-side view, while the top-level
        # field above stays exactly where it has always been.
        #
        # Deliberately *not* echoed here: `options.keep_extracted` (an
        # output-side flag -- it only controls whether the intermediate
        # netlist is also written to disk, and is already visible as
        # `environment.extracted_netlist`) and `options.netgen_timeout_s` (a
        # runtime guard, not a compare input). Neither can change a verdict.
        #
        # `combine_devices` echoes the *resolved* request value, so it is a
        # boolean for the bool shape and the (normalised) list of device-class
        # names for issue #1370's list shape -- the same round-trip discipline
        # every other key here follows.
        "options": {
            "combine_devices": opts.combine_devices,
            # Issue #1552: `null` when the option was omitted, else the
            # resolved `{<circuit-name-glob>: <bool>}` mapping -- the same
            # always-present-but-nullable convention `netgen_setup` already
            # follows, since (unlike `combine_devices`) there is no
            # meaningful default value to echo instead of the option being
            # absent.
            "combine_devices_per_circuit": opts.combine_devices_per_circuit,
            # Issue #2682: the resolved retry budget (the parser's default
            # when omitted), echoed even when combining is off -- it
            # describes the configured budget, not retries consumed.
            "combine_devices_max_attempts": opts.combine_devices_max_attempts,
            "flatten_layout": opts.flatten_layout,
            "flatten_reference": opts.flatten_reference,
            # Issue #4165: present only when the caller set it, so a request
            # without the option produces a byte-identical report.
            **_compare_timeout_replay_options(
                {"compare_timeout_s": opts.compare_timeout_s}
            ),
            "netgen_setup": opts.netgen_setup_echo,
            "parameter_tolerance": opts.parameter_tolerance,
            # Issue #1928: `null` when the option was omitted, else the
            # resolved `{<device-class>: [<parameter>, ...]}` mapping -- the
            # same always-present-but-nullable convention
            # `combine_devices_per_circuit`/`netgen_setup` already follow.
            "compare_parameters": opts.compare_parameters,
            # Issue #1952: `null` when the option was omitted (the check
            # still runs -- omitting it is the default-on case), else the
            # caller's own `true`/`false`/`{"expected_nets": {...}}` value
            # verbatim. Same always-present-but-nullable convention
            # `combine_devices_per_circuit`/`netgen_setup` follow; echoed
            # unresolved so a committed report round-trips back into the
            # request document it came from.
            "power_connectivity": opts.power_connectivity_echo,
            # Issue #2692: `null` when the option was omitted (the
            # form-dependent default applies -- on for
            # `reference.form: "gate-level-verilog"`, off elsewhere), else the
            # caller's own boolean verbatim. Same always-present-but-nullable
            # convention `power_connectivity` above follows, echoed unresolved
            # so a committed report round-trips back into the request document
            # it came from; what this run actually *did* is reported as the
            # `topology.top_level_pins_anchored` disclosure in `mismatches[]`.
            "anchor_top_level_pins": opts.anchor_top_level_pins_echo,
            # Issue #2845: the effective boolean (`false` when omitted).
            "strict_top_level_pin_count": opts.strict_top_level_pin_count,
            # Issue #3650: the effective boolean (`false` when omitted).
            "compare_mos_flavour": opts.compare_mos_flavour,
            "ignore_mos_bulk": opts.ignore_mos_bulk,
            "supply_nets": opts.supply_nets,
            # Issue #2813: `null` when omitted, else the resolved
            # `{"nmos": <net>, "pmos": <net>}` bulk-tie policy (opt-in; the
            # applied ties are disclosed in `mismatches[]`).
            "finfet_bulk": opts.finfet_bulk,
        },
        # Issue #1998: every `hints.equivalent_pins` grouping actually passed
        # to `NetlistComparer.equivalent_pins()` for this run, keyed by
        # (reference-side) subcircuit name -- unlike `hints.same_nets`,
        # which the comparer can refuse (surfaced as a `hints.rejected`
        # mismatch entry, see `_build_mismatches`), a swappable-pin group has
        # no rejection outcome to report, so without this field a caller
        # reading only the response has no way to tell whether an
        # `equivalent_pins` hint changed the verdict at all. `null` when
        # `request.hints.equivalent_pins` was omitted (or resolved to no
        # groups), matching `options.compare_parameters`'
        # always-present-but-nullable convention for an optional dict-shaped
        # echo -- never a spuriously present empty `{}`.
        "hints_applied": (equivalent_pins_applied if equivalent_pins_applied else None),
        # Issue #3014: the `hints.constant_nets` bindings applied
        # (`{constant: layout net}`), `null` when none -- the echo `--rerun`
        # replays, like `hints_applied` above.
        "constant_nets_applied": _constant_nets_echo(constant_bindings),
        "status": status,
        # Issue #1952: the power/ground half of a `reference.form:
        # "gate-level-verilog"` compare, reported as its own verdict beside
        # `status` rather than folded into it. `status` is, and stays,
        # exactly `NetlistComparer.compare()`'s own signal-connectivity
        # result (see this module's docstring) -- so a caller that wants
        # full LVS on a digital block gates on
        # `status == "match" and power_connectivity["status"] == "match"`.
        # See `_power_connectivity_report` and
        # `docs/design/pg-connectivity-check-decision.md`.
        "power_connectivity": power_connectivity,
        # Issue #1983: whether this layout's MOS body terminals were resolved
        # from real drawn/labelled geometry or from a deck-synthesized net --
        # the gradeable form of the `device.body_unverified` warning, which
        # was previously only discoverable by string-matching a `category`
        # inside `mismatches[]`. Reported beside `status`, never folded into
        # it: `status` is, and stays, exactly the comparer's own result, so a
        # layout with unverified bodies still reports `status: "match"` when
        # the compare matched. See `_body_verification_report`.
        "body_verification": body_verification,
        # Issue #2461: per participating resistor/capacitor device class,
        # which of its declared parameters this compare actually covered and
        # why each of the others did not -- the gradeable form of the
        # `device.geometry_not_compared` warning (and of the
        # `device.placeholder_value`/`device.parameter_excluded` warnings,
        # whose exclusions are labelled with their own `reason`). Reported
        # beside `status`, never folded into it: a class whose geometry was
        # never compared still reports `status: "match"` when the compare
        # matched -- that is precisely the disclosure gap this answers. `null`
        # for `"engine": "netgen"`. See `_device_parameter_coverage`.
        "device_parameter_coverage": device_parameter_coverage,
        "mismatch_count": len(mismatches),
        "error_count": sum(category_error_counts.values()),
        "category_counts": dict(sorted(category_counts.items())),
        "category_error_counts": dict(sorted(category_error_counts.items())),
        "counts": counts,
        "device_classes": device_classes,
        "environment": {
            "engine": engine,
            "engine_version": engine_version,
            # Issue #2373: which netgen executable actually produced this
            # verdict -- the absolute path `_resolve_netgen_binary` settled
            # on, so a committed report distinguishes a from-source `netgen`
            # from Debian/Ubuntu's `netgen-lvs` (and from an explicitly
            # named build). Always `null` for `"engine": "klayout"`, which
            # launches no subprocess.
            "netgen_binary": netgen_binary,
            "layout_sha256": sha256_file(layout_hash_source),
            "reference_sha256": sha256_file(reference_netlist_path),
            "extracted_netlist": extracted_netlist_path,
            **reference_module_names(reference_netlist_path, reference_form),
        },
        # Issue #2693: built above the return (per-form `pin_order_sources`).
        "provenance": provenance,
        "mismatches": mismatches,
        "net_correspondence": net_correspondence,
    }
    return _finish_response(
        report,
        portable=portable_paths,
        request_dir=request_dir,
        pdk_root=(reference_pdk_info or {}).get("root"),
    )

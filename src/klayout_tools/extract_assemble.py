"""Final ``klt extract`` report assembly (issue #5196).

Moved verbatim out of the tail of ``run_extract``: given every already-computed
stage output, build the response dict (schema version, provenance, additive
optional fields, ``metrics``). It does no geometry work.

The helpers it calls that still live in ``extract.py`` are imported inside the
function body: ``extract.py`` imports this module at its top level, so a
module-level ``from .extract import ...`` would be a circular import.
"""

from __future__ import annotations

from typing import Any

from . import env_provenance
from ._provenance import build_provenance
from .decks import deck_source_path


def assemble_extract_report(
    *,
    abstracted_cells: Any,
    black_box_regions: Any,
    dbu_um: Any,
    dead_metal: Any,
    deck: Any,
    deck_name: Any,
    deck_options: Any,
    declared_pins: Any,
    def_pin_promotion: Any,
    def_pins: Any,
    device_counts: Any,
    device_models: Any,
    device_recognition_only_layers: Any,
    devices: Any,
    dummy_devices_dropped: Any,
    hierarchy_report: Any,
    ignored_layers: Any,
    label_layers: Any,
    matched_device_groups_report: Any,
    merged_net_labels: Any,
    missing_flavour_markers: Any,
    netlist_sha256: Any,
    nets: Any,
    parasitics_report: Any,
    path: Any,
    pdk_info: Any,
    pin_renames: Any,
    pin_ties: Any,
    probe_results: Any,
    rename_declared_pins: Any,
    reported_netlist_path: Any,
    single_terminal_nets: Any,
    spef_path: Any,
    split_net_labels: Any,
    subcircuit_report: Any,
    tie_declared_pins: Any,
    top_cell_name: Any,
    unbiased_pmos_body_nets: Any,
    unmodelled_poly: Any,
    unsupported_devices: Any,
    voltage_domain_warnings: Any,
    warnings: Any,
) -> dict[str, Any]:
    """Build the ``run_extract`` result dict (byte-identical to the inline code)."""
    from .extract import (
        SCHEMA_VERSION,
        _build_metrics,
        _describe_device_models,
        _describe_label_layers,
        _hierarchy_fields,
        _normalized_netlist_sha256,
        _pin_ties_field,
        _staged,
    )

    # Issue #2659: `file` -- the input layout `provenance.input.content_hash`
    # below covers -- is normalised against the repo root resolved from the
    # layout's own location, the same "walk up from the input" anchor
    # `klt pex` picks for its own `layout`. `netlist_path` below is an
    # *output* path and stays a plain string (see `SCHEMA_VERSION`).
    repo_root = env_provenance.repo_root_for(path)
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "file": env_provenance.repo_relative_path(path, repo_root=repo_root),
        "deck": deck_name,
        "top": top_cell_name,
        "dbu_um": dbu_um,
        "netlist_path": reported_netlist_path,
        "netlist_sha256": netlist_sha256,
        # Additive field (issue #3407): `null` exactly when `netlist_sha256`
        # is (`--no-netlist`); otherwise a numbering-independent digest of
        # the flat `devices[]`/`nets[]` connectivity -- see
        # `_normalized_netlist_sha256`.
        "netlist_normalized_sha256": (
            None
            if netlist_sha256 is None
            else _staged(
                "netlist_normalized_hash", _normalized_netlist_sha256, devices, nets
            )
        ),
        "status": "extracted",
        "device_count": len(devices),
        "net_count": len(nets),
        "pin_count": sum(1 for net in nets if net["pin"]),
        "device_counts": dict(sorted(device_counts.items())),
        "dummy_devices_dropped": dummy_devices_dropped,
        "ignored_layers": ignored_layers,
        # Additive field (issue #619): always a list, empty when no layer is
        # read for device recognition only -- see run_extract's docstring and
        # `ExtractionDeck.device_recognition_only_layers` for the field's
        # full meaning.
        "device_recognition_only_layers": device_recognition_only_layers,
        "device_classes": list(deck.device_classes),
        "devices": devices,
        "nets": nets,
        "warnings": warnings,
        # Additive field (issue #293): always a list, empty when the layout
        # draws no reserved-annotation-layer geometry -- see run_extract's
        # docstring for the field's full meaning.
        "black_box_regions": black_box_regions,
        # Additive field (issue #620): always a list, empty unless
        # `abstract_cell_patterns` (--abstract-cells) matched at least one
        # instantiated cell -- see run_extract's docstring for the field's
        # full meaning.
        "abstracted_cells": abstracted_cells,
        # Additive field (issue #324): always a list, empty when `warnings`
        # carries no unmodelled-device entry -- see run_extract's docstring
        # and `_detect_unmodelled_poly_bodies` for the field's full meaning.
        "unmodelled_poly": unmodelled_poly,
        # Additive field (issue #470): always a list, empty when no net
        # carries more than one KLayout-assigned label -- see
        # `_detect_merged_net_labels` and the comment above where it is
        # computed for the field's full meaning.
        "merged_net_labels": merged_net_labels,
        # Additive field (issue #3920): always a list, empty when no drawn
        # label text names more than one net -- see
        # `_detect_split_net_labels`.
        "split_net_labels": split_net_labels,
        # Additive field (issue #552): always a list, empty when this deck
        # registers no voltage-domain marker or none of it overlaps
        # extracted MOS geometry -- see run_extract's docstring and
        # `_detect_voltage_domain_overlap` for the field's full meaning.
        "voltage_domain_warnings": voltage_domain_warnings,
        # Additive field (issue #2417): always a list, empty unless a deck
        # declares `mos_flavours` whose marker layer has zero shapes in the
        # whole layout -- see `_detect_missing_flavour_markers`.
        "missing_flavour_markers": missing_flavour_markers,
        # Additive field (issue #2919): always a list, empty unless the
        # layout draws shapes on a deck `unsupported_device_markers` layer
        # (a device class the deck cannot extract yet, e.g. gf180mcu MOS
        # capacitors) -- see `_detect_unsupported_devices`.
        "unsupported_devices": unsupported_devices,
        # Additive field (issue #555): always a list, empty when no PMOS
        # device's body net is the anonymous, KLayout-synthesized kind -- see
        # run_extract's docstring and `_detect_unbiased_pmos_body_nets` for
        # the field's full meaning.
        "unbiased_pmos_body_nets": unbiased_pmos_body_nets,
        # Additive field (issue #596): always a list, empty when every net
        # either has zero or 2+ device terminals, or is a declared pin -- see
        # run_extract's docstring and `_detect_single_terminal_nets` for the
        # field's full meaning.
        "single_terminal_nets": single_terminal_nets,
        # Additive field (issue #676): always a list, empty when every
        # routing-stack (metals/vias) shape joins an extracted net -- see
        # run_extract's docstring and `_detect_dead_metal` for the field's
        # full meaning. Computed inside `_extract_netlist` (it needs the live
        # `LayoutToNetlist` shape database), which also appends its aggregate
        # `warnings[]` entry.
        "dead_metal": dead_metal,
        # Additive field (issue #1018): always a list, empty unless
        # `matched_device_groups` (--matched-group) was given -- see
        # run_extract's docstring and `_describe_matched_device_groups` for
        # the field's full meaning.
        "matched_device_groups": matched_device_groups_report,
        # Additive field (issue #2473): `null` unless `def_pins`
        # (--def-pins) was given, the declared-vs-promoted DEF `PINS`
        # reconciliation summary otherwise -- see run_extract's docstring and
        # `_unpromoted_def_pin_geometry` for the field's full meaning.
        "def_pin_promotion": def_pin_promotion,
        # Additive field (issue #2854): `null` unless `rename_declared_pins`
        # (--rename-declared-pins) was given, the declared-name rename report
        # otherwise -- every rename performed (joined name -> declared name),
        # plus the `declared_pin_short` / `declared_pin_rename_collision`
        # findings for the nets the pass deliberately did *not* touch. See
        # run_extract's docstring and `_rename_declared_pin_nets` for the
        # field's full meaning.
        "pin_renames": pin_renames,
        # Additive field (issue #2854): the effective `--rename-declared-pins`
        # value, always present (`false` when the flag was not given) so
        # `--check --rerun` can replay the rename alongside the declared
        # sets `pin_renames.declared_pins`/`pin_renames.def_pins` echo --
        # see `rerun_extract_report`.
        "rename_declared_pins": rename_declared_pins,
        # Additive fields (issue #3061): the `--pins` / `--def-pins` sets as
        # given -- a sorted list, or `null` when the flag was not given (`[]`
        # stays distinct from `null`: the demotion passes treat an empty set
        # differently from an absent one). Always present, independent of
        # `--rename-declared-pins`, so `--check --rerun` can replay them; see
        # `rerun_extract_report`.
        "declared_pins": None if declared_pins is None else sorted(declared_pins),
        "def_pins": None if def_pins is None else sorted(def_pins),
        # Additive field (issue #2656): `null` unless `label_layers`
        # (--label-layer) was given, otherwise the caller's own role ->
        # [layer, datatype] (or `null`) mapping echoed verbatim -- the record
        # of *which GDS purpose this run read net/pin names from*, which the
        # deck name alone no longer answers once it can be overridden. Read
        # back by `rerun_extract_report` so a `--check --rerun` of such a
        # report replays the same purposes instead of drifting on every net
        # name at once. See run_extract's `label_layers` docstring paragraph.
        "label_layers": _describe_label_layers(label_layers),
        # Additive field (issue #4234): `null` unless `device_models`
        # (--device-model) was given, otherwise the caller's own
        # `{class: subckt}` binding for a device class with no curated PDK
        # model (e.g. gf180mcu's `bjt`), echoed so a committed report records
        # which model the bipolar card was written against.
        "device_models": _describe_device_models(device_models),
        # Additive field (issue #2986): one entry per `--probe`, in request
        # order; always present, `[]` when none was given. `net`/`cluster_id`
        # are the final `nets[].name`/`nets[].net_id` of the net under the
        # point, both `null` when `found` is false. See `_evaluate_probes`.
        "probes": probe_results,
    }
    # Additive field (issue #4383): only present under `--tie-declared-pins`;
    # each net's canonical port plus the declared names tied to it by a
    # `Rtie_*` resistor in the written `.SUBCKT`.
    result.update(_pin_ties_field(tie_declared_pins, pin_ties))
    if pdk_info is not None:
        result["pdk"] = {
            "variant": pdk_info["variant"],
            # {path, scope} (issue #1376), not the raw `--pdk-root` argument
            # -- a PDK install is external to the invoking repo by
            # definition, so the old raw-path shape baked a host-specific
            # absolute path (possibly a username) into any committed
            # `--format json` evidence report. `provenance.pdk` below
            # already carries this PDK's reproducible identity
            # (name/source/version) without a path.
            "root": env_provenance.repo_relative_path(pdk_info["root"]),
            "version": pdk_info["version"],
        }
    else:
        result["pdk"] = None

    result["provenance"] = build_provenance(
        deck_name=deck_name,
        deck_path=deck_source_path(deck_name),
        pdk=pdk_info,
        input_path=path,
        deck_options=deck_options,
        # Issue #2394: record the *resolved* option set (every key this deck
        # declares, defaults filled in) plus which of those the caller
        # pinned, not only the caller's own overrides -- a run that silently
        # took gf180mcu's `poly_res='1k'` default was otherwise
        # indistinguishable, in its own record, from a deck with no
        # selectable options at all.
        resolve_deck_options=True,
    )

    # Additive, independently-optional field (issue #216 addendum): `null`
    # unless `--parasitics` was given, a `parasitics` summary block otherwise.
    result["parasitics"] = parasitics_report

    # Additive field (issue #948): `null` unless `--spef` was given, the
    # resolved SPEF path otherwise -- see `run_extract`'s `spef_output`
    # docstring paragraph and `docs/cli/extract.md`'s "SPEF export" section.
    result["spef_path"] = spef_path

    # Additive, independently-optional field (issue #2245): `null` unless
    # `--subcircuit` was given, the sliced sub-circuit's own report otherwise
    # -- see `run_extract`'s `subcircuit_cell` docstring paragraph and
    # `docs/cli/extract.md`'s "Sub-circuit isolation" section.
    result["subcircuit"] = subcircuit_report

    # Additive field (issue #2722): present *only* when
    # `--hierarchical-cells` was given, so a flat run's JSON is unchanged.
    result.update(_hierarchy_fields(hierarchy_report))

    # Additive `metrics` block (issue #1848, adopting the declared metric
    # namespace registry from #247 beyond its `layout-metrics`/`klt drc`
    # (#1847) adopters) -- see `run_extract`'s docstring "metrics" paragraph
    # for the full contract. Never replaces `device_count`/`net_count`/
    # `pin_count`/`parasitics.*` above, which stay exactly as documented.
    result["metrics"] = _build_metrics(result, parasitics_report)

    return result
